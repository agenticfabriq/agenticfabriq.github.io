"""RFC 8693 token exchange: trade a user's token for a narrower downstream one.

Reads ./agent_grant.json written by pkce_authorization_code.py, exchanges the
access token in it for a token scoped to one downstream audience, and refuses
to hand back a token that gained authority instead of losing it.

Environment:
    OAUTH_ISSUER          same issuer used for the authorization code flow
    EXCHANGE_CLIENT_ID    confidential client that performs the exchange
    EXCHANGE_CLIENT_SECRET
    DOWNSTREAM_AUDIENCE   logical name of the target service (RFC 8693 2.1)
    DOWNSTREAM_SCOPE      space-delimited scopes the downstream call needs
    DOWNSTREAM_RESOURCE   optional target URI; sent as `resource` if set

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


def issued_scopes(response: dict, requested_scope: str | None) -> set[str]:
    """Scopes on the issued token.

    RFC 8693 section 2.2.1 makes `scope` OPTIONAL only when it is identical to
    the scope the client requested; otherwise it is REQUIRED. So an absent
    `scope` means the request was honoured exactly.
    """
    if "scope" in response:
        return set(response["scope"].split())
    if requested_scope:
        return set(requested_scope.split())
    return set()


def assert_narrower(response: dict, subject_scopes: set[str], requested_scope: str | None) -> set[str]:
    """Fail if the exchange widened the grant instead of narrowing it.

    Nothing in RFC 8693 makes a token exchange a downscope. The spec is a
    general security token service, and an authorization server is free to
    issue a token with rights the subject token never carried. Keycloak's own
    documentation says so plainly and offers the `downscope-assertion-grant-
    enforcer` client policy executor to forbid it. Assume nothing: check.
    """
    issued = issued_scopes(response, requested_scope)
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


def main() -> int:
    issuer = os.environ["OAUTH_ISSUER"]
    client_id = os.environ["EXCHANGE_CLIENT_ID"]
    client_secret = os.environ["EXCHANGE_CLIENT_SECRET"]
    audience = os.environ.get("DOWNSTREAM_AUDIENCE")
    resource = os.environ.get("DOWNSTREAM_RESOURCE")
    scope = os.environ.get("DOWNSTREAM_SCOPE")

    if not audience and not resource:
        print("set DOWNSTREAM_AUDIENCE or DOWNSTREAM_RESOURCE: an unaudienced token is the bug")
        return 1

    with open(GRANT_FILE, encoding="utf-8") as handle:
        grant = json.load(handle)
    subject_scopes = set(grant["granted_scope"])

    metadata = discover(issuer)
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
    if claims is not None:
        print(f"aud:               {claims.get('aud')}")
        print(f"act:               {claims.get('act')}")
    else:
        print("issued token is opaque; use RFC 7662 introspection to inspect it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
