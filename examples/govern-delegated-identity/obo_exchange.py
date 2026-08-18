"""Validate an inbound user token, then exchange it on-behalf-of the user.

This is the Microsoft Entra ID on-behalf-of (OBO) flow written out against the
protocol rather than through MSAL, because the parts that break in production
are the parts a library hides: what the returned `scope` actually is, and which
errors mean "retry" versus "this delegation is over".

Environment:

    AF_TENANT_ID       Entra tenant id or domain
    AF_CLIENT_ID       the agent's own app registration (client) id
    AF_CLIENT_SECRET   the agent's client secret

Dependencies: PyJWT with crypto, for signature validation.

    pip install "pyjwt[crypto]"

References for every literal below are in the guide's Further reading section.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

AUTHORITY = "https://login.microsoftonline.com"
TIMEOUT_SECONDS = 30

# The Entra token endpoint returns these AADSTS codes inside `error_codes`.
# Grouping them by what you should *do* is the whole point of reading them.
REVOKED_CODES = {
    50173,  # grant expired due to revocation, or the user changed their password
    700082,  # refresh token expired due to inactivity
}
CONSENT_CODES = {
    65001,  # DelegationDoesNotExist: user or admin has not consented
}


class DelegationEnded(Exception):
    """The user's grant is gone. Stop the run; do not retry, do not partial-write."""


class ConsentRequired(Exception):
    """The agent needs a consent it does not have. Send the user through consent."""


class ClaimsChallenge(Exception):
    """Conditional Access wants more from the user (MFA, compliant device, CAE).

    Carries the opaque `claims` blob so the middle tier can relay it to the
    client in a 401, per the OBO error-response guidance. The client re-requests
    a token presenting the challenge; the agent must not retry on its own.
    """

    def __init__(self, message: str, claims: str | None) -> None:
        super().__init__(message)
        self.claims = claims


class ScopeWidened(Exception):
    """The authorization server returned more than was asked for. Refuse it."""


@dataclass(frozen=True)
class DelegatedToken:
    """A downstream token minted for one user, for one resource, once."""

    access_token: str
    granted_scopes: frozenset[str]
    expires_in: int


def _post_form(url: str, form: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(form).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            raise RuntimeError(f"{error.code} from {url}: {payload!r}") from error
        _raise_for_oauth_error(parsed, error.code)
        raise


def _raise_for_oauth_error(body: dict, status: int) -> None:
    """Turn a token-endpoint error body into an exception that says what to do.

    Order matters. Branch on the numeric `error_codes` first, because the string
    `error` is coarse: `invalid_grant` covers a revoked grant, an expired
    assertion, and a Conditional Access step-up, and those need three different
    responses from you.
    """
    error = body.get("error", "")
    codes = set(body.get("error_codes") or ())
    description = body.get("error_description", error or f"HTTP {status}")
    claims = body.get("claims")

    if codes & REVOKED_CODES:
        raise DelegationEnded(description)
    if codes & CONSENT_CODES:
        raise ConsentRequired(description)
    if claims is not None or error == "interaction_required":
        raise ClaimsChallenge(description, claims)
    if error == "invalid_grant":
        # invalid_grant with no code we recognise is still terminal for this
        # assertion: the token you were handed cannot be redeemed. Treat it as
        # the end of the delegation rather than looping on it.
        raise DelegationEnded(description)
    raise RuntimeError(f"{status} {error}: {description}")


# Scopes the protocol adds or drops on its own. They carry no resource
# permission, so they must not count as widening.
PROTOCOL_SCOPES = frozenset({"openid", "profile", "email", "offline_access"})


def _bare(scopes: frozenset[str]) -> frozenset[str]:
    """Strip the resource prefix and drop protocol scopes, for subset comparison."""
    return frozenset(
        scope.rsplit("/", 1)[-1] for scope in scopes if scope not in PROTOCOL_SCOPES
    )


def _discover(tenant: str) -> dict:
    """Read the tenant's OpenID configuration instead of hardcoding endpoints."""
    url = f"{AUTHORITY}/{tenant}/v2.0/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def validate_inbound_token(token: str, tenant: str, audience: str) -> dict:
    """Verify the signature, issuer, and audience of the token the caller sent.

    This is not optional and it is not decoration. An agent that decodes the
    payload without checking the signature is an agent whose entire identity
    model is "whatever the caller typed". The audience check is the one people
    skip: a token minted for some other API is not a token for you, and
    forwarding it downstream is the confused-deputy bug the MCP authorization
    spec forbids in as many words.
    """
    try:
        import jwt  # PyJWT
        from jwt import PyJWKClient
    except ImportError as error:  # fail closed, never fall back to decoding
        raise RuntimeError('install "pyjwt[crypto]" to validate inbound tokens') from error

    metadata = _discover(tenant)
    signing_key = PyJWKClient(metadata["jwks_uri"]).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=audience,
        issuer=metadata["issuer"],
        options={"require": ["exp", "aud", "iss"]},
    )


def exchange_on_behalf_of(
    user_assertion: str,
    scopes: frozenset[str] | set[str],
    *,
    tenant: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> DelegatedToken:
    """Trade the user's token for a downstream token that still names the user.

    `user_assertion` must be an access token whose `aud` is this agent's own
    client id. Entra rejects an assertion minted for anything else, which is the
    protocol enforcing the same rule as the audience check above.
    """
    tenant = tenant or os.environ["AF_TENANT_ID"]
    client_id = client_id or os.environ["AF_CLIENT_ID"]
    client_secret = client_secret or os.environ["AF_CLIENT_SECRET"]
    requested = frozenset(scopes)

    body = _post_form(
        f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": client_id,
            "client_secret": client_secret,
            "assertion": user_assertion,
            "scope": " ".join(sorted(requested)),
            "requested_token_use": "on_behalf_of",
        },
    )

    granted = frozenset(body.get("scope", "").split())
    # Read what you got. The response `scope` is authoritative and it is allowed
    # to differ from what you asked for. Narrower is normal and fine: the user
    # consented to less than the agent requested. Wider is not, and it deserves a
    # hard failure rather than a log line, because a downstream call carrying
    # permissions nobody asked for is the exact thing you are preventing.
    #
    # Compare on bare permission names. Entra's own OBO example requests
    # `https://graph.microsoft.com/user.read offline_access` and answers with
    # `"scope": "https://graph.microsoft.com/user.read"`, so the resource prefix
    # round-trips and `offline_access` does not come back; a comparison that
    # ignores neither of those fires on a perfectly correct response.
    if _bare(granted) - _bare(requested):
        raise ScopeWidened(f"asked for {sorted(requested)}, received {sorted(granted)}")

    return DelegatedToken(
        access_token=body["access_token"],
        granted_scopes=granted,
        expires_in=int(body.get("expires_in", 0)),
    )
