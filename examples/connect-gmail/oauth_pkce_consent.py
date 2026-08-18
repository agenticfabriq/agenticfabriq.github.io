"""Run the Gmail authorization-code + PKCE consent flow for one user.

Real Google endpoints, no client library. Requires: pip install requests

Environment:
    GOOGLE_CLIENT_ID      OAuth client id (Google Cloud console, "Desktop app")
    GOOGLE_CLIENT_SECRET  the matching client secret

Prints the refresh token on success. Store it encrypted; it is a standing
grant on the user's mailbox until the user revokes it.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser

import requests

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Start read-only. Widening later is one more consent screen; narrowing later
# is a security review you already failed.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/oauth2callback"


def _b64url(raw: bytes) -> str:
    """Base64url with padding stripped, as RFC 7636 requires."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for code_challenge_method=S256.

    64 random bytes encode to 86 characters, inside Google's documented
    43-128 character range for the verifier.
    """
    verifier = _b64url(os.urandom(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single redirect Google sends back to the loopback address."""

    query: dict[str, list[str]] = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth2callback":
            # Browsers ask for /favicon.ico first. Ignore it or it eats the request.
            self.send_response(404)
            self.end_headers()
            return
        type(self).query = urllib.parse.parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<p>Consent recorded. You can close this tab.</p>")

    def log_message(self, *args: object) -> None:
        return  # the default handler logs the authorization code to stderr


def wait_for_callback(timeout_seconds: int = 300) -> dict[str, list[str]]:
    """Serve requests until the callback arrives, or give up at the deadline.

    handle_request() returns after server.timeout with nothing handled, so the
    loop needs its own absolute deadline or the timeout is decorative.
    """
    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.timeout = timeout_seconds
    deadline = time.monotonic() + timeout_seconds
    try:
        while not _CallbackHandler.query:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no OAuth callback within {timeout_seconds}s")
            server.handle_request()
    finally:
        server.server_close()
    return _CallbackHandler.query


def build_authorization_url(client_id: str, challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # access_type=offline is what makes Google return a refresh token.
        "access_type": "offline",
        # prompt=consent forces a fresh refresh token even on re-authorisation.
        "prompt": "consent",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str) -> dict:
    response = requests.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"token exchange failed {response.status_code}: {response.text}")
    return response.json()


def main() -> int:
    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]

    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(32)
    url = build_authorization_url(client_id, challenge, state)

    print("Open this URL and grant consent:\n", url, "\n")
    webbrowser.open(url)

    query = wait_for_callback()

    # Compare with a constant-time check: state is a CSRF defence, not a hint.
    returned_state = query.get("state", [""])[0]
    if not secrets.compare_digest(returned_state, state):
        print("state mismatch; discarding the response", file=sys.stderr)
        return 1
    if "error" in query:
        print(f"consent denied: {query['error'][0]}", file=sys.stderr)
        return 1

    token = exchange_code(client_id, client_secret, query["code"][0], verifier)

    # Users can untick individual restricted scopes. Never assume you got what
    # you asked for; the token response tells you what you actually got.
    granted = set(token.get("scope", "").split())
    missing = sorted(set(SCOPES) - granted)
    if missing:
        print(f"user withheld: {', '.join(missing)}", file=sys.stderr)
        return 1

    if "refresh_token" not in token:
        print(
            "no refresh_token in the response: the user already has a live grant "
            "for this client. Re-request with prompt=consent or reuse the stored token.",
            file=sys.stderr,
        )
        return 1

    print("refresh_token:", token["refresh_token"])
    print("granted scopes:", " ".join(sorted(granted)))
    print("access token expires in:", token.get("expires_in"), "seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
