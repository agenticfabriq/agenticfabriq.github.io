"""Narrow a live Google OAuth grant: acquire the smaller grant, THEN revoke.

The order is the whole point. Revoking first leaves the user with a dead agent
until they notice a prompt they were not expecting. This script:

    1. runs the authorization-code + PKCE flow for the NARROW scope set
    2. checks the `scope` field of the token response equals that set exactly
       -- Google warns that when an app requests multiple permissions, "users
       may not grant your app access to all of them", so a partial grant is a
       normal outcome, not an error you can skip
    3. refuses to revoke anything if step 2 did not hold
    4. only then POSTs the OLD refresh token to the revocation endpoint

There is no partial revoke: https://oauth2.googleapis.com/revoke takes a token,
not a scope list. Revocation is all-or-nothing, which is exactly why the new
grant has to exist and be stored before the old one is destroyed.

    python3 narrow_google_scope.py --dry-run
        Print the plan and the authorization URL. No browser, no network,
        no revocation. Start here.

    python3 narrow_google_scope.py --old-refresh-token "$OLD"
        Do it for real, for one user.

Environment:
    GOOGLE_CLIENT_ID      OAuth client id (Google Cloud console, "Desktop app")
    GOOGLE_CLIENT_SECRET  the matching client secret

Requires: pip install requests
"""

from __future__ import annotations

import argparse
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

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# What the agent is being narrowed FROM and TO. drive.readonly is Google's
# "View and download all your Drive files" and is classified restricted;
# drive.file is "Create new Drive files, or modify existing files, that you
# open with an app or that the user shares with an app" and is not.
OLD_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
NEW_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

REDIRECT_PORT = 8766
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/oauth2callback"


class NarrowingAborted(Exception):
    """Raised whenever the new grant is not provably in hand.

    Every raise site below is a point at which revoking the old token would
    leave the user with no working integration and no new one either.
    """


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


def build_authorization_url(client_id: str, challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(NEW_SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        # prompt=consent forces a fresh refresh token for the narrow set.
        "prompt": "consent",
        # include_granted_scopes is deliberately ABSENT. Setting it true means
        # the new token "will also cover any scopes to which the user
        # previously granted the application access", which is the opposite of
        # narrowing -- it is the parameter that makes grants only ever grow.
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


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
        self.wfile.write(b"<p>Re-consent recorded. You can close this tab.</p>")

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
                raise NarrowingAborted(f"no OAuth callback within {timeout_seconds}s")
            server.handle_request()
    finally:
        server.server_close()
    return _CallbackHandler.query


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str) -> dict:
    import requests

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
        raise NarrowingAborted(f"token exchange failed {response.status_code}: {response.text}")
    return response.json()


def assert_exactly_narrow(token: dict) -> set[str]:
    """The grant is only narrowed if the response says so. Verify, do not assume."""
    granted = set(token.get("scope", "").split())
    wanted = set(NEW_SCOPES)

    missing = sorted(wanted - granted)
    if missing:
        # The user unticked something. The old token is still the only working
        # credential, so leave it alone and let them try again.
        raise NarrowingAborted(f"user withheld: {', '.join(missing)}")

    extra = sorted(granted - wanted - {"openid", "email", "profile"})
    if extra:
        # Nothing was narrowed. Revoking the old token here would destroy a
        # working credential and replace it with an equally broad one.
        raise NarrowingAborted(
            "response is not narrower than the old grant; still carries: " + ", ".join(extra)
        )

    if "refresh_token" not in token:
        # Without a refresh token there is no durable new grant to fall back
        # on, so revoking the old one strands this user at the next run.
        raise NarrowingAborted("no refresh_token in the response; nothing durable to keep")

    return granted


def store_new_refresh_token(user: str, refresh_token: str) -> None:
    """Replace with a write to your encrypted credential store.

    It must be durable and readable by the agent BEFORE the revoke below. If
    this write can fail silently, the revoke is a data-loss bug.
    """
    print(f"[store] {user}: new refresh token acquired ({len(refresh_token)} chars)")


def revoke(token: str) -> None:
    """POST the OLD token to the revocation endpoint. 200 means revoked."""
    import requests

    response = requests.post(
        REVOKE_ENDPOINT,
        data={"token": token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code == 200:
        print("[revoke] old grant revoked")
        return
    # A token that is already invalid is a success for our purposes: the goal
    # is that it stops working, and it has.
    if response.status_code == 400 and "invalid_token" in response.text:
        print("[revoke] old token was already invalid; nothing to do")
        return
    raise SystemExit(f"revoke failed {response.status_code}: {response.text}")


def plan() -> None:
    print("narrowing plan")
    print(f"  from: {' '.join(OLD_SCOPES)}")
    print(f"  to:   {' '.join(NEW_SCOPES)}")
    print("  order: consent -> verify scope field -> store -> revoke old")
    print("  a failure at any step before 'store' leaves the old grant untouched")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan and URL only")
    parser.add_argument("--user", default="user@example.com", help="who this grant belongs to")
    parser.add_argument("--old-refresh-token", help="the broad refresh token to revoke at the end")
    args = parser.parse_args()

    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(32)

    plan()
    if args.dry_run:
        # Dry run must work with no credentials configured at all, so the
        # client id falls back to a placeholder rather than raising.
        placeholder = os.environ.get("GOOGLE_CLIENT_ID", "<GOOGLE_CLIENT_ID>")
        print("\nauthorization URL:\n" + build_authorization_url(placeholder, challenge, state))
        print("\ndry run: no browser opened, no token exchanged, nothing revoked")
        return 0

    if not args.old_refresh_token:
        print("--old-refresh-token is required outside --dry-run", file=sys.stderr)
        return 2

    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
    url = build_authorization_url(client_id, challenge, state)

    print("\nOpen this URL and grant the narrower consent:\n", url, "\n")
    webbrowser.open(url)

    try:
        query = wait_for_callback()

        # state is a CSRF defence, so compare it in constant time.
        if not secrets.compare_digest(query.get("state", [""])[0], state):
            raise NarrowingAborted("state mismatch; discarding the response")
        if "error" in query:
            raise NarrowingAborted(f"consent denied: {query['error'][0]}")

        token = exchange_code(client_id, client_secret, query["code"][0], verifier)
        granted = assert_exactly_narrow(token)
        store_new_refresh_token(args.user, token["refresh_token"])
    except NarrowingAborted as error:
        print(f"\nABORTED: {error}", file=sys.stderr)
        print(
            "The old grant was NOT revoked. The agent keeps working at the old\n"
            "width, which is the failure mode you want: too much access beats\n"
            "no access plus a support ticket. Re-run when the user is ready.",
            file=sys.stderr,
        )
        return 1

    print("[verify] granted exactly:", " ".join(sorted(granted)))

    # Only now. The new grant exists, was verified, and has been stored.
    revoke(args.old_refresh_token)

    print(
        "\n"
        + json.dumps(
            {
                "user": args.user,
                "removed": OLD_SCOPES,
                "kept": NEW_SCOPES,
                "verified_from": "token response `scope` field",
                "note": (
                    "Google documents the upward merge and documents revocation, but "
                    "does not state whether a subset re-consent clears the grant record. "
                    "Confirm at https://myaccount.google.com/permissions."
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
