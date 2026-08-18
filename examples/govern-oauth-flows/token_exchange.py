"""RFC 8693 token exchange: trade a user's token for a narrower downstream one.

Reads ./agent_grant.json written by pkce_authorization_code.py, exchanges the
access token in it for a token scoped to one downstream audience, and refuses
to hand back a token that gained authority instead of losing it.

Written against RFC 8693 and Keycloak's documented request and response
shapes; not executed here against a live authorization server. Provider
behaviour varies more for this grant than for any other in OAuth, so treat
the deployment-specific notes as the part most likely to differ.

Environment:
    OAUTH_ISSUER          same issuer used for the authorization code flow
    EXCHANGE_CLIENT_ID    confidential client that performs the exchange
    EXCHANGE_CLIENT_SECRET
    DOWNSTREAM_AUDIENCE   logical name of the target service (RFC 8693 2.1)
    DOWNSTREAM_SCOPE      REQUIRED. Space-delimited scopes the downstream call
                          needs. Without it the narrowing check cannot conclude
                          anything, so this script refuses to run.
    DOWNSTREAM_RESOURCE   optional target URI; sent as `resource` if set
    ACTOR_TOKEN           optional. A token identifying the agent itself, sent
                          as `actor_token` to ask for delegation semantics
                          rather than impersonation (RFC 8693 section 1.1).
                          Server support for this is deployment-specific.

Usage:
    python3 token_exchange.py            # exchange, then verify narrowing
    python3 token_exchange.py --refresh  # refresh the stored grant, handling rotation
    python3 token_exchange.py --revoke   # revoke the stored refresh token

Standard library only. Python 3.10+.
"""

from __future__ import annotations

import base64
import json
import os
import sys

from oauth_http import OAuthError, discover, post_form

GRANT_FILE = "agent_grant.json"

# RFC 8693 section 2.1. The grant type is an absolute URI because RFC 6749
# section 4.5 requires extension grants to be identified that way.
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"

# RFC 8693 section 3. These are the exact registered identifiers; a token type
# spelled any other way is a different (and almost certainly unregistered) URI.
TOKEN_TYPE_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
TOKEN_TYPE_REFRESH_TOKEN = "urn:ietf:params:oauth:token-type:refresh_token"
TOKEN_TYPE_ID_TOKEN = "urn:ietf:params:oauth:token-type:id_token"
TOKEN_TYPE_SAML1 = "urn:ietf:params:oauth:token-type:saml1"
TOKEN_TYPE_SAML2 = "urn:ietf:params:oauth:token-type:saml2"
# Defined in RFC 7519 section 9, not RFC 8693, but usable in the same slots.
TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"


class NotNarrower(RuntimeError):
    """The exchange returned a token with more authority than the subject."""


def exchange(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    subject_token: str,
    *,
    audience: str | None = None,
    resource: str | None = None,
    scope: str | None = None,
    actor_token: str | None = None,
) -> dict:
    """Perform an RFC 8693 section 2.1 token exchange.

    Parameter names and values below are verbatim from RFC 8693 section 2.1.
    `audience` and `resource` are both OPTIONAL and may repeat; this helper
    sends at most one of each, which is what section 2.1.1 recommends -- the
    requested rights are the Cartesian product of every scope at every target,
    so each extra target lowers the chance the server will fulfil the request.
    """
    form: list[tuple[str, str]] = [
        ("grant_type", TOKEN_EXCHANGE_GRANT),
        ("subject_token", subject_token),
        ("subject_token_type", TOKEN_TYPE_ACCESS_TOKEN),
        ("requested_token_type", TOKEN_TYPE_ACCESS_TOKEN),
    ]
    if audience:
        form.append(("audience", audience))
    if resource:
        form.append(("resource", resource))
    if scope:
        form.append(("scope", scope))
    if actor_token:
        # RFC 8693 section 2.1: actor_token_type is REQUIRED when actor_token
        # is present and MUST NOT be included otherwise.
        form.append(("actor_token", actor_token))
        form.append(("actor_token_type", TOKEN_TYPE_ACCESS_TOKEN))
    return post_form(token_endpoint, form, client_id, client_secret)


def jwt_claims(token: str) -> dict | None:
    """Decode a JWT payload for inspection. Returns None for opaque tokens.

    This does not verify the signature and must never be used to make an
    access control decision. RFC 9068 section 4 puts that duty on the resource
    server; a client decoding its own token is only reading a hint.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None


def issued_scopes(response: dict, requested_scope: str | None) -> set[str] | None:
    """Scopes on the issued token, or None when they cannot be determined.

    RFC 8693 section 2.2.1 makes `scope` OPTIONAL only when it is identical to
    the scope the client requested; otherwise it is REQUIRED. So an absent
    `scope` means the request was honoured exactly -- but that inference only
    holds if a scope was actually requested. Request nothing and there is no
    identity for the response to be identical to, so an absent `scope` carries
    no information, and returning an empty set here would be a lie the caller
    cannot detect.
    """
    if "scope" in response:
        return set(response["scope"].split())
    if requested_scope:
        return set(requested_scope.split())
    return None


def assert_narrower(response: dict, subject_scopes: set[str], requested_scope: str | None) -> set[str]:
    """Fail if the exchange widened the grant instead of narrowing it.

    Nothing in RFC 8693 makes a token exchange a downscope. The spec is a
    general security token service, and an authorization server is free to
    issue a token with rights the subject token never carried. Keycloak's own
    documentation says so plainly and offers the `downscope-assertion-grant-
    enforcer` client policy executor to forbid it. Assume nothing: check.
    """
    issued = issued_scopes(response, requested_scope)
    if issued is None:
        # Fail closed. An exchange whose result cannot be compared is not a
        # verified-narrow exchange, and treating it as an empty set would wave
        # through a token that may still carry the user's entire grant.
        raise NotNarrower(
            "cannot verify narrowing: the response carried no `scope` and none was "
            "requested, so there is nothing to compare. Send an explicit `scope`."
        )
    gained = sorted(issued - subject_scopes)
    if gained:
        raise NotNarrower(f"exchange added scopes absent from the subject token: {', '.join(gained)}")
    return issued


def refresh(token_endpoint: str, client_id: str, client_secret: str, refresh_token: str) -> dict:
    """RFC 6749 section 6. Handles rotation by returning the whole response.

    If the response carries a new `refresh_token`, RFC 6749 section 6 requires
    the client to discard the old one immediately. Keeping the old value around
    "just in case" is what turns rotation into a self-inflicted reuse alarm.
    """
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    return post_form(token_endpoint, form, client_id, client_secret)


def revoke(metadata: dict, client_id: str, client_secret: str, token: str, hint: str) -> None:
    """RFC 7009 section 2.1. `hint` is `access_token` or `refresh_token`."""
    endpoint = metadata.get("revocation_endpoint")
    if not endpoint:
        print("note: server publishes no revocation_endpoint; nothing to call")
        return
    post_form(endpoint, {"token": token, "token_type_hint": hint}, client_id, client_secret)


def load_grant() -> dict:
    with open(GRANT_FILE, encoding="utf-8") as handle:
        return json.load(handle)


def save_grant(grant: dict) -> None:
    with open(GRANT_FILE, "w", encoding="utf-8") as handle:
        json.dump(grant, handle, indent=2)


def run_refresh(metadata: dict, client_id: str, client_secret: str) -> int:
    """Refresh the stored grant, persisting a rotated refresh token atomically."""
    grant = load_grant()
    if not grant.get("refresh_token"):
        print("stored grant carries no refresh_token; nothing to refresh")
        return 1
    try:
        response = refresh(metadata["token_endpoint"], client_id, client_secret, grant["refresh_token"])
    except OAuthError as exc:
        if exc.error == "invalid_grant":
            # Two very different situations share this code. If the stored value
            # is the newest one you were issued, the server rejecting it means
            # someone else already spent it -- that is reuse detection firing,
            # and it is a security event rather than a re-consent trigger.
            print(f"invalid_grant on refresh: {exc.description or 'grant is gone'}")
            print("if this token is the newest one you were issued, treat it as reuse detection")
            return 1
        print(f"refresh failed: {exc}")
        return 1

    grant["access_token"] = response["access_token"]
    grant["expires_in"] = response.get("expires_in")
    rotated = response.get("refresh_token")
    if rotated:
        # RFC 6749 section 6: the client MUST discard the old refresh token.
        # Replace it rather than keeping the previous value as a fallback.
        grant["refresh_token"] = rotated
    save_grant(grant)
    print(f"refreshed; server {'rotated' if rotated else 'did not rotate'} the refresh token")
    return 0


def run_revoke(metadata: dict, client_id: str, client_secret: str) -> int:
    """Revoke the stored refresh token and drop it from disk, in that order."""
    grant = load_grant()
    token = grant.get("refresh_token")
    if not token:
        print("stored grant carries no refresh_token; nothing to revoke")
        return 1
    revoke(metadata, client_id, client_secret, token, "refresh_token")
    grant["refresh_token"] = None
    save_grant(grant)
    print("revoked the refresh token and removed it from the stored grant")
    return 0


def main() -> int:
    issuer = os.environ["OAUTH_ISSUER"]
    client_id = os.environ["EXCHANGE_CLIENT_ID"]
    client_secret = os.environ["EXCHANGE_CLIENT_SECRET"]
    audience = os.environ.get("DOWNSTREAM_AUDIENCE")
    resource = os.environ.get("DOWNSTREAM_RESOURCE")
    scope = os.environ.get("DOWNSTREAM_SCOPE")
    actor_token = os.environ.get("ACTOR_TOKEN")

    metadata = discover(issuer)

    if "--refresh" in sys.argv:
        return run_refresh(metadata, client_id, client_secret)
    if "--revoke" in sys.argv:
        return run_revoke(metadata, client_id, client_secret)

    if not audience and not resource:
        print("set DOWNSTREAM_AUDIENCE or DOWNSTREAM_RESOURCE: an unaudienced token is the bug")
        return 1
    if not scope:
        # Without a requested scope the narrowing check below cannot conclude
        # anything (see issued_scopes). Refusing here is better than running a
        # check that always passes.
        print("set DOWNSTREAM_SCOPE: without it the narrowing check cannot verify anything")
        return 1

    grant = load_grant()
    subject_scopes = set(grant["granted_scope"])
    supported = metadata.get("grant_types_supported")
    if supported is not None and TOKEN_EXCHANGE_GRANT not in supported:
        # RFC 8414 section 2 defines grant_types_supported and defaults it to
        # ["authorization_code", "implicit"] when absent, so absence tells you
        # nothing useful about extension grants. A present list that omits the
        # exchange grant does.
        print(f"{issuer} does not advertise {TOKEN_EXCHANGE_GRANT}")
        return 1

    try:
        response = exchange(
            metadata["token_endpoint"],
            client_id,
            client_secret,
            grant["access_token"],
            audience=audience,
            resource=resource,
            scope=scope,
            actor_token=actor_token,
        )
    except OAuthError as exc:
        # RFC 8693 section 2.2.2: an unusable subject_token or a malformed
        # request is invalid_request; a target the server will not mint for is
        # invalid_target. They mean different things and want different fixes.
        if exc.error == "invalid_target":
            print(f"server will not issue for that target: {exc.description}")
        else:
            print(f"exchange failed: {exc}")
        return 1

    issued = assert_narrower(response, subject_scopes, scope)

    print(f"issued_token_type: {response['issued_token_type']}")
    print(f"token_type:        {response['token_type']}")
    print(f"expires_in:        {response.get('expires_in', 'unspecified')}")
    print(f"subject scopes:    {' '.join(sorted(subject_scopes)) or '(none recorded)'}")
    print(f"issued scopes:     {' '.join(sorted(issued)) or '(none)'}")

    claims = jwt_claims(response["access_token"])
    if claims is None:
        print("issued token is opaque; use RFC 7662 introspection to inspect it")
        return 0

    print(f"aud:               {claims.get('aud')}")
    act = claims.get("act")
    if act is not None:
        print(f"act:               {act}")
    elif actor_token is None:
        # Expected, not a failure: RFC 8693 section 4.1's `act` claim only
        # appears when the server issues a composite token, which it will not
        # do without an actor. Set ACTOR_TOKEN to ask for one.
        print("act:               absent (no ACTOR_TOKEN sent, so no delegation was requested)")
    else:
        # An actor token was sent and the server still issued a plain token.
        # That is impersonation, not delegation, and it is worth knowing.
        print("act:               absent despite ACTOR_TOKEN -- this server did not issue a")
        print("                   composite token, so the result carries no record of the actor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
