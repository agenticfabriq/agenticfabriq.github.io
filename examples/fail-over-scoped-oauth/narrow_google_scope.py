"""Narrow a live Google OAuth grant for ONE user: revoke, then re-consent.

The order is the whole point, and it is the opposite of the intuitive one.

Google's web-server OAuth page prints this beside every revocation sample:

    "Revocation removes all OAuth 2.0 scopes previously granted to a project,
     invalidating any issued access or refresh tokens for all clients
     registered under that project."

The grant is held per project per user, not per token: the same page says the
combined authorization "includes all scopes that the user granted to the API
project even if the grants were requested from different clients". Two things
follow, and both kill the acquire-then-revoke order:

  * Revoking the broad refresh token AFTER acquiring the narrow one destroys
    the narrow one too. Same user, same project, one grant record.
  * A subset re-consent does not narrow the grant record on its own. Only an
    explicit revoke clears it, or a later request can be auto-approved back up
    to the old scope set without showing the user anything.

So: revoke first, consent immediately afterwards, and keep the gap between them
to the few seconds of one user-initiated interaction. That is why this script
is meant to run from inside a first-run interstitial while the user is present,
never from a background job.

Blast radius warning: the revoke below clears this user's grant for EVERY OAuth
client registered under the same Cloud project. Check what else lives in the
project before running it -- if the agent shares a project with your main
product, this signs the user out of the product too.

    python3 narrow_google_scope.py --dry-run
        Print the plan and the authorization URL. No browser, no network,
        nothing revoked. Start here.

    python3 narrow_google_scope.py --old-refresh-token "$OLD"
        Do it for real, for one user, with that user watching.

Environment:
    GOOGLE_CLIENT_ID      OAuth client id (Google Cloud console, "Desktop app")
    GOOGLE_CLIENT_SECRET  the matching client secret

Exit codes: 0 narrowed, 1 revoked but not re-consented (user must be re-prompted),
2 nothing happened.

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
# open with an app or that the user shares with an app while using the Google
# Picker API or the app's file picker", and is non-sensitive.
#
# Note this is a change of access MODEL, not a subset trim: after cutover the
# agent reaches nothing until the user re-picks files through the picker. Plan
# that re-pick into the same interstitial.
OLD_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
NEW_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Google returns sign-in scopes alongside whatever you asked for. Both the bare
# OIDC names and the userinfo.* URLs appear in the wild, so tolerate all of them
# when checking that nothing broader came back.
IDENTITY_SCOPES = {
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
}

REDIRECT_PORT = 8766
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/oauth2callback"


class RevokeFailed(Exception):
    """The old grant could not be cleared, so the narrowing never started.

    Distinct from ConsentFailed: here the user still has their old access and
    nothing has changed, which is exit 2 rather than exit 1.
    """


class ConsentFailed(Exception):
    """The narrow grant was not obtained after the old one was already revoked.

    This is a real state, not an edge case: the user closed the tab. They now
    have no grant at all, so the caller must mark the connection as needing
    consent and put them back into the interstitial next session. It must never
    become a background retry loop.
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
                raise ConsentFailed(f"no OAuth callback within {timeout_seconds}s")
            server.handle_request()
    finally:
        server.server_close()
    return _CallbackHandler.query


def revoke(token: str, label: str = "old project-level grant") -> None:
    """POST the OLD token to the revocation endpoint. Runs FIRST, by design.

    Clears the whole project-level grant for this user, which is exactly what
    makes the narrowing real -- and exactly why every other client under the
    project loses this user's grant at the same moment.
    """
    import requests

    response = requests.post(
        REVOKE_ENDPOINT,
        data={"token": token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code == 200:
        print(f"[revoke] {label} cleared")
        return
    # A token that is already invalid is a success for our purposes: the goal
    # is that it stops working, and it has.
    if response.status_code == 400 and "invalid_token" in response.text:
        print(f"[revoke] {label} was already invalid; nothing to do")
        return
    # Do NOT continue to consent on an unknown revoke failure: the old grant may
    # still be live, and consenting now would leave the user holding both.
    # This is exit 2 ("nothing happened"), never exit 1 ("revoked, not
    # re-consented") -- a caller has to be able to tell those two apart.
    raise RevokeFailed(f"revoke failed {response.status_code}: {response.text}")


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
        raise ConsentFailed(f"token exchange failed {response.status_code}: {response.text}")
    return response.json()


def _discard(token: dict, why: str) -> None:
    """Revoke a token we are about to throw away, before throwing it away.

    Google has already issued this grant. Dropping the response on the floor
    leaves a live grant on the user's account that this app can no longer see
    or clear -- standing authority nobody owns, which is the whole subject of
    this guide. Revoke it first, then raise.
    """
    doomed = token.get("refresh_token") or token.get("access_token")
    if not doomed:
        return
    try:
        revoke(doomed, label="discarded narrow grant")
        print(f"[cleanup] discarded grant revoked ({why})", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - cleanup must never mask `why`
        # Report loudly and keep going: the caller still needs the original
        # reason, and a human now has an orphan grant to clear by hand.
        print(
            f"[cleanup] COULD NOT revoke the discarded grant ({error}). "
            "Clear it manually at https://myaccount.google.com/permissions",
            file=sys.stderr,
        )


def assert_exactly_narrow(token: dict) -> set[str]:
    """The grant is only narrowed if the response says so. Verify, do not assume."""
    granted = set(token.get("scope", "").split())
    wanted = set(NEW_SCOPES)

    missing = sorted(wanted - granted)
    if missing:
        _discard(token, "user withheld a requested scope")
        raise ConsentFailed(f"user withheld: {', '.join(missing)}")

    extra = sorted(granted - wanted - IDENTITY_SCOPES)
    if extra:
        _discard(token, "grant came back broader than requested")
        raise ConsentFailed("grant is not narrower; still carries: " + ", ".join(extra))

    if "refresh_token" not in token:
        _discard(token, "no refresh token to store")
        raise ConsentFailed("no refresh_token in the response; nothing durable to store")

    return granted


def store_new_refresh_token(user: str, refresh_token: str) -> None:
    """Replace with a write to your encrypted credential store."""
    print(f"[store] {user}: new refresh token stored ({len(refresh_token)} chars)")


def mark_needs_consent(user: str, reason: str) -> None:
    """The user has no grant at all right now. Make that an explicit state.

    Put them back into the same interstitial on their next session. Do not
    retry in the background -- there is no consent screen to click out there.
    """
    print(f"[state] {user}: connection marked NEEDS_CONSENT ({reason})", file=sys.stderr)


def plan() -> None:
    print("narrowing plan")
    print(f"  from: {' '.join(OLD_SCOPES)}")
    print(f"  to:   {' '.join(NEW_SCOPES)}")
    print("  order: revoke -> consent -> verify scope field -> store")
    print("  run this from a user-facing interstitial, with the user present")
    print("  WARNING: the revoke clears this user's grant for every OAuth")
    print("           client registered under the same Cloud project")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan and URL only")
    parser.add_argument("--user", default="user@example.com", help="who this grant belongs to")
    parser.add_argument("--old-refresh-token", help="the broad refresh token, revoked first")
    args = parser.parse_args()

    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(32)

    plan()
    if args.dry_run:
        # Dry run must work with no credentials configured at all, so the
        # client id falls back to a placeholder rather than raising.
        placeholder = os.environ.get("GOOGLE_CLIENT_ID", "<GOOGLE_CLIENT_ID>")
        print("\nauthorization URL:\n" + build_authorization_url(placeholder, challenge, state))
        print("\ndry run: nothing revoked, no browser opened, no token exchanged")
        return 0

    if not args.old_refresh_token:
        print("--old-refresh-token is required outside --dry-run", file=sys.stderr)
        return 2

    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
    url = build_authorization_url(client_id, challenge, state)

    # Mark BEFORE revoking, not after: the write is idempotent, and a crash
    # between the two calls would otherwise leave a user with no grant and
    # nothing recording that fact.
    mark_needs_consent(args.user, "about to revoke, awaiting narrow consent")

    # STEP ONE. Everything after this point runs with the user holding no grant,
    # so it has to be fast and it has to be in front of them.
    try:
        revoke(args.old_refresh_token)
    except RevokeFailed as error:
        print(f"\nREVOKE FAILED: {error}", file=sys.stderr)
        print(
            "Nothing was changed. The old grant may still be live, so the user\n"
            "still has working access at the old width. Do not send them to the\n"
            "consent screen: they would end up holding both grants.",
            file=sys.stderr,
        )
        return 2

    print("\nOpen this URL now and grant the narrower consent:\n", url, "\n")
    webbrowser.open(url)

    try:
        query = wait_for_callback()

        # state is a CSRF defence, so compare it in constant time.
        if not secrets.compare_digest(query.get("state", [""])[0], state):
            raise ConsentFailed("state mismatch; discarding the response")
        if "error" in query:
            raise ConsentFailed(f"consent denied: {query['error'][0]}")

        token = exchange_code(client_id, client_secret, query["code"][0], verifier)
        granted = assert_exactly_narrow(token)
        store_new_refresh_token(args.user, token["refresh_token"])
    except ConsentFailed as error:
        print(f"\nCONSENT DID NOT COMPLETE: {error}", file=sys.stderr)
        print(
            "The old grant is already revoked, so this user currently has NO\n"
            "access. The connection stays marked NEEDS_CONSENT. Re-prompt from\n"
            "the interstitial on their next session; do not retry in the\n"
            "background, and do not restore the old scope set to paper over it.\n"
            "\n"
            "The revoke cleared this user's grant for EVERY client under the same\n"
            "Cloud project, so any sibling integration they were using also needs\n"
            "its own re-consent. Notify those owners; this state is recoverable,\n"
            "but not by this script alone.",
            file=sys.stderr,
        )
        return 1

    print("[verify] granted exactly:", " ".join(sorted(granted)))
    print(
        "\n"
        + json.dumps(
            {
                "user": args.user,
                "removed": OLD_SCOPES,
                "kept": NEW_SCOPES,
                "order": "revoke -> consent -> verify -> store",
                "verified_from": "token response `scope` field",
                "note": (
                    "Revocation cleared the project-level grant for every client under "
                    "this Cloud project. Confirm at https://myaccount.google.com/permissions."
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
