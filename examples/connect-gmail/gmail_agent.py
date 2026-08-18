"""Read unread Gmail and reply in-thread, refreshing the access token by hand.

Real Gmail REST endpoints, no client library, so nothing about the token
lifecycle is hidden. Requires: pip install requests

Environment:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN   issued by oauth_pkce_consent.py

Listing and reading need gmail.readonly. Sending needs gmail.send as well;
with a readonly-only grant, send_reply raises on a 403 whose message is
"Request had insufficient authentication scopes."
"""

from __future__ import annotations

import base64
import email.message
import os
import random
import sys
import time
from typing import Any

import requests

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
MAX_ATTEMPTS = 6


class GrantRevoked(RuntimeError):
    """The refresh token is dead. No retry recovers this; the user must re-consent."""


def refresh_access_token(refresh_token: str) -> tuple[str, int]:
    """Trade a refresh token for an access token. Returns (token, lifetime_seconds)."""
    response = requests.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code == 400:
        body = response.json()
        if body.get("error") == "invalid_grant":
            # e.g. {"error": "invalid_grant",
            #       "error_description": "Token has been expired or revoked."}
            raise GrantRevoked(body.get("error_description", "invalid_grant"))
    response.raise_for_status()
    body = response.json()
    return body["access_token"], int(body["expires_in"])


class AccessToken:
    """Holds one access token and refreshes it when it is about to expire."""

    def __init__(self, refresh_token: str, skew_seconds: int = 60) -> None:
        self._refresh_token = refresh_token
        self._skew = skew_seconds
        self._value = ""
        self._expires_at = 0.0

    def get(self) -> str:
        if time.monotonic() >= self._expires_at:
            self._value, lifetime = refresh_access_token(self._refresh_token)
            self._expires_at = time.monotonic() + lifetime - self._skew
        return self._value

    def expire(self) -> None:
        """Force the next get() to refresh, after a 401 says the token is dead."""
        self._expires_at = 0.0


def _is_rate_limited(response: requests.Response) -> bool:
    """Gmail signals throttling as 429, and also as 403 with a rate-limit reason.

    A 403 for insufficient scope or domainPolicy must NOT be retried, so read
    the reason rather than retrying every 403.
    """
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        errors = response.json()["error"].get("errors", [])
    except (ValueError, KeyError, AttributeError):
        # ValueError: not JSON. KeyError: no "error". AttributeError: "error"
        # came back as a bare string, which some Google endpoints still do.
        return False
    return bool({error.get("reason") for error in errors} & RATE_LIMIT_REASONS)


def call(token: AccessToken, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """One Gmail REST call, with a token refresh on 401 and backoff on throttling."""
    for attempt in range(MAX_ATTEMPTS):
        response = requests.request(
            method,
            f"{GMAIL}{path}",
            headers={"Authorization": f"Bearer {token.get()}"},
            timeout=30,
            **kwargs,
        )
        if response.status_code == 401 and attempt == 0:
            token.expire()
            continue
        if response.status_code in RETRYABLE_STATUS or _is_rate_limited(response):
            time.sleep(min(2**attempt, 32) + random.uniform(0, 1))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"gmail {method} {path} still failing after {MAX_ATTEMPTS} attempts")


def list_unread(token: AccessToken, max_results: int = 10) -> list[dict[str, str]]:
    """messages.list returns ids only: {'id': ..., 'threadId': ...}, 5 quota units."""
    body = call(
        token,
        "GET",
        "/messages",
        params={"q": "is:unread", "maxResults": max_results},
    )
    return body.get("messages", [])


def get_message(token: AccessToken, message_id: str, fmt: str = "full") -> dict[str, Any]:
    """messages.get costs 20 quota units per call, four times a list page."""
    return call(token, "GET", f"/messages/{message_id}", params={"format": fmt})


def get_thread(token: AccessToken, thread_id: str) -> dict[str, Any]:
    """Every message in the conversation in one call. 40 quota units."""
    return call(token, "GET", f"/threads/{thread_id}", params={"format": "full"})


def header(message: dict[str, Any], name: str) -> str:
    for entry in message.get("payload", {}).get("headers", []):
        if entry["name"].lower() == name.lower():
            return entry["value"]
    return ""


def plain_text(payload: dict[str, Any]) -> str:
    """Depth-first walk to the first text/plain part. Bodies are base64url."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if not data:
            return ""
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    for part in payload.get("parts", []):
        found = plain_text(part)
        if found:
            return found
    return ""


def attachment_names(payload: dict[str, Any]) -> list[str]:
    """Everything the mailbox would hand an agent that it did not ask for."""
    names = []
    if payload.get("filename"):
        names.append(payload["filename"])
    for part in payload.get("parts", []):
        names.extend(attachment_names(part))
    return names


def send_reply(token: AccessToken, message: dict[str, Any], body_text: str) -> dict[str, Any]:
    """Reply so Gmail threads it: matching Subject plus In-Reply-To/References."""
    original_id = header(message, "Message-ID")
    subject = header(message, "Subject")

    reply = email.message.EmailMessage()
    reply["To"] = header(message, "Reply-To") or header(message, "From")
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    reply["In-Reply-To"] = original_id
    reply["References"] = f"{header(message, 'References')} {original_id}".strip()
    reply.set_content(body_text)

    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode("ascii")
    # messages.send costs 100 quota units, twenty times a list call.
    return call(
        token,
        "POST",
        "/messages/send",
        json={"raw": raw, "threadId": message["threadId"]},
    )


def run(token: AccessToken) -> int:
    unread = list_unread(token, max_results=5)
    if not unread:
        print("nothing unread")
        return 0

    for stub in unread:
        message = get_message(token, stub["id"])
        print(f"--- {header(message, 'From')} :: {header(message, 'Subject')}")
        print(f"    snippet: {message.get('snippet', '')[:120]}")
        files = attachment_names(message.get("payload", {}))
        if files:
            print(f"    attachments in context: {', '.join(files)}")

    # The whole conversation around the first unread message, 40 quota units.
    thread = get_thread(token, unread[0]["threadId"])
    print(f"thread {thread['id']} carries {len(thread.get('messages', []))} messages")

    # Uncomment once the grant includes gmail.send. On a readonly-only grant
    # this raises requests.HTTPError with a 403 and
    # "Request had insufficient authentication scopes."
    # send_reply(token, get_message(token, unread[0]["id"]),
    #            "Acknowledged. A human is picking this up.")
    return 0


def main() -> int:
    token = AccessToken(os.environ["GMAIL_REFRESH_TOKEN"])
    try:
        # The handler wraps the entire run, not just the first call.
        # Revocation mid-run is the normal case: revocation invalidates the
        # access token too, so a later call 401s, the refresh behind it returns
        # invalid_grant, and the right move is to abandon the run rather than
        # finish it on partial data.
        return run(token)
    except GrantRevoked as error:
        print(f"grant revoked, stopping run: {error}", file=sys.stderr)
        print("delete the stored token and prompt the user to re-consent", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
