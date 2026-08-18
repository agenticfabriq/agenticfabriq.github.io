"""Authorization code grant with PKCE, written out at the HTTP level.

Runs against any authorization server that publishes RFC 8414 or OpenID
Connect discovery metadata. Written against Keycloak's documented behaviour;
start one in dev mode with:

    docker run -p 127.0.0.1:8080:8080 \\
      -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \\
      -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \\
      quay.io/keycloak/keycloak:26.7.1 start-dev

Then create a realm, a confidential client with the redirect URI below, and
a test user.

Environment:
    OAUTH_ISSUER         e.g. http://localhost:8080/realms/agents
    OAUTH_CLIENT_ID      client id belonging to this agent and nothing else
    OAUTH_CLIENT_SECRET  client secret; unset for a public client
    OAUTH_SCOPE          space-delimited scopes, default "openid profile"

Writes the token response to ./agent_grant.json for token_exchange.py.

Standard library only. Python 3.10+.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser

from oauth_http import DiscoveryError, OAuthError, discover, post_form

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
GRANT_FILE = "agent_grant.json"


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for code_challenge_method=S256.

    RFC 7636 section 4.1 defines code_verifier as 43 to 128 characters from
    the unreserved set, and recommends a 32-octet random sequence, which
    base64url-encodes to exactly 43 characters.

    RFC 7636 section 4.2 defines the S256 transformation as
    BASE64URL-ENCODE(SHA256(ASCII(code_verifier))).
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def require_s256(metadata: dict) -> None:
    """Refuse to run unless S256 is on the table.

    `plain` sets code_challenge equal to code_verifier, so an attacker who
    can read the authorization request learns the verifier and PKCE stops
    protecting anything (RFC 9700 section 2.1.1).
    """
    methods = metadata.get("code_challenge_methods_supported")
    if methods is None:
        # RFC 8414 section 2 marks this member OPTIONAL, so its absence is not
        # evidence that PKCE is unsupported. RFC 9700 section 2.1.1 says
        # authorization servers MUST support PKCE, so send S256 and let the
        # token request fail loudly rather than downgrading to plain.
        print("note: server publishes no code_challenge_methods_supported; sending S256 anyway")
        return
    if "S256" not in methods:
        raise DiscoveryError(f"server advertises {methods!r}; refusing to downgrade to plain")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Loopback receiver for the redirect (RFC 8252 section 7.3)."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {key: values[0] for key, values in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authorization response received. You can close this tab.\n")

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log; it would print the code."""


def wait_for_redirect(timeout_seconds: float = 300.0) -> dict[str, str]:
    """Serve loopback requests until the authorization response arrives.

    Serving exactly one request would be wrong: browsers speculatively fetch
    /favicon.ico, and any stray GET would be consumed by the 404 branch, end
    the wait, and make a perfectly healthy run report a timeout it never had.
    Keep serving until the callback actually lands or the deadline passes.
    """
    deadline = time.monotonic() + timeout_seconds
    server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
    server.timeout = 1.0
    try:
        while not _CallbackHandler.result:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"no authorization response on {REDIRECT_URI} within {timeout_seconds:.0f} seconds"
                )
            # handle_request() returns after server.timeout with no request,
            # which is what lets the deadline above actually be checked.
            server.handle_request()
    finally:
        server.server_close()
    return _CallbackHandler.result


def build_authorization_url(metadata: dict, client_id: str, scope: str, challenge: str, state: str) -> str:
    """Assemble the RFC 6749 section 4.1.1 request plus RFC 7636 section 4.3."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{metadata['authorization_endpoint']}?{urllib.parse.urlencode(params)}"


def exchange_code(
    metadata: dict,
    client_id: str,
    client_secret: str | None,
    code: str,
    verifier: str,
) -> dict:
    """Redeem the authorization code (RFC 6749 section 4.1.3 + RFC 7636 section 4.5)."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    return post_form(metadata["token_endpoint"], form, client_id, client_secret)


def granted_scopes(token: dict, requested: str) -> set[str]:
    """What the user actually granted.

    RFC 6749 section 5.1 makes `scope` OPTIONAL in the response only when it
    is identical to what the client requested. An absent `scope` therefore
    means "you got what you asked for" -- but treating absence as a silent
    success is how over-scoped agents ship, so the caller is told either way.
    """
    if "scope" in token:
        return set(token["scope"].split())
    return set(requested.split())


def main() -> int:
    issuer = os.environ["OAUTH_ISSUER"]
    client_id = os.environ["OAUTH_CLIENT_ID"]
    client_secret = os.environ.get("OAUTH_CLIENT_SECRET")
    scope = os.environ.get("OAUTH_SCOPE", "openid profile")

    metadata = discover(issuer)
    require_s256(metadata)

    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(32)
    url = build_authorization_url(metadata, client_id, scope, challenge, state)

    print("Open this URL and consent:\n", url, "\n")
    webbrowser.open(url)
    response = wait_for_redirect()

    if "error" in response:
        # RFC 6749 section 4.1.2.1: the authorization endpoint reports failure
        # by redirecting with an `error` parameter, not by refusing to redirect.
        print(f"authorization failed: {response['error']} {response.get('error_description', '')}")
        return 1

    if not secrets.compare_digest(response.get("state", ""), state):
        # RFC 6749 section 10.12: a mismatched or missing `state` means this
        # redirect does not belong to the request this process started.
        print("state mismatch: discarding the authorization code")
        return 1

    try:
        token = exchange_code(metadata, client_id, client_secret, response["code"], verifier)
    except OAuthError as exc:
        # RFC 7636 section 4.6: a code_verifier that does not match the stored
        # code_challenge is reported as invalid_grant, the same code used for
        # an expired or already-redeemed authorization code.
        print(f"token request failed: {exc}")
        return 1

    granted = granted_scopes(token, scope)
    withheld = sorted(set(scope.split()) - granted)
    if withheld:
        print(f"user withheld: {', '.join(withheld)}")

    with open(GRANT_FILE, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "issuer": metadata["issuer"],
                "requested_scope": scope,
                "granted_scope": sorted(granted),
                "token_type": token.get("token_type"),
                "expires_in": token.get("expires_in"),
                "access_token": token["access_token"],
                "refresh_token": token.get("refresh_token"),
            },
            handle,
            indent=2,
        )
    print(f"granted: {' '.join(sorted(granted))}")
    print(f"access token expires in {token.get('expires_in', 'unspecified')}s; wrote {GRANT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
