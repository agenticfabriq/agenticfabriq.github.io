"""Read a Slack channel's recent messages and reply in-thread as the bot.

Raw Web API calls, no Slack SDK, so nothing about scopes, pagination, or the
error surface is hidden. Requires: pip install requests

Environment:
    SLACK_BOT_TOKEN   an xoxb- token from the app's OAuth & Permissions page
    SLACK_CHANNEL     a channel name without the '#', e.g. agent-testing

Bot scopes this file needs: channels:read (find the channel),
channels:history (read it), chat:write (post). The bot must also be a member
of the channel, which is membership, not scope -- see the not_in_channel
branch below.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import requests

API = "https://slack.com/api"
TOKEN = os.environ["SLACK_BOT_TOKEN"]

# ok:false errors that no retry will fix. The install is gone or the token is
# not a token any more; the correct response is to stop and re-install.
DEAD_TOKEN_ERRORS = {
    "invalid_auth",
    "not_authed",
    "account_inactive",
    "token_revoked",
    "token_expired",
}
MAX_ATTEMPTS = 5


class SlackError(RuntimeError):
    """A Slack ok:false response, carrying the machine-readable error code."""

    def __init__(self, method: str, body: dict[str, Any]) -> None:
        self.method = method
        self.code = body.get("error", "unknown")
        self.body = body
        super().__init__(f"{method}: {self.code}")


class InstallRevoked(SlackError):
    """The token is dead. Re-install the app; do not retry."""


def call(method: str, params: dict[str, Any] | None = None, *, json_body: bool = False) -> dict[str, Any]:
    """POST one Web API method and return its body, or raise.

    Slack answers HTTP 200 with {"ok": false, "error": "..."} for application
    errors, so the status code alone tells you almost nothing. The exceptions
    are HTTP 429, which carries Retry-After, and 5xx.
    """
    url = f"{API}/{method}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if json_body:
            response = requests.post(
                url,
                json=params or {},
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=30,
            )
        else:
            response = requests.post(
                url,
                data=params or {},
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                },
                timeout=30,
            )

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == MAX_ATTEMPTS:
                response.raise_for_status()
            # Slack tells you exactly how long to wait. Believe it rather than
            # guessing with a backoff curve of your own.
            delay = int(response.headers.get("Retry-After", "1"))
            print(f"{method}: throttled, sleeping {delay}s", file=sys.stderr)
            time.sleep(delay)
            continue

        response.raise_for_status()
        body = response.json()
        if body.get("ok"):
            return body
        if body.get("error") in DEAD_TOKEN_ERRORS:
            raise InstallRevoked(method, body)
        raise SlackError(method, body)

    raise RuntimeError(f"{method}: exhausted {MAX_ATTEMPTS} attempts")


def find_channel(name: str) -> str:
    """Resolve a channel name to its id. Needs channels:read.

    conversations.list is Tier 2 (20+ per minute) and pages at 100 by default,
    1000 maximum. Resolve once and cache the id; do not scan the workspace on
    every run.
    """
    cursor = ""
    while True:
        page = call(
            "conversations.list",
            {
                "types": "public_channel",
                "exclude_archived": "true",
                "limit": 200,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        for channel in page.get("channels", []):
            if channel.get("name") == name:
                return channel["id"]
        cursor = page.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            raise SystemExit(f"no public channel named {name!r} visible to this token")


def recent_messages(channel_id: str, limit: int = 15) -> list[dict[str, Any]]:
    """Read the newest messages in a channel. Needs channels:history.

    The default limit is 100 and the maximum is 999, but a non-Marketplace app
    created on or after 2025-05-29 is capped at 15 objects per request and one
    request per minute, so 15 is the portable ceiling.
    """
    body = call("conversations.history", {"channel": channel_id, "limit": limit})
    return body.get("messages", [])


def post_reply(channel_id: str, thread_ts: str, text: str) -> str:
    """Reply inside a thread as the bot. Needs chat:write.

    thread_ts is the ts of the parent message. Omit it and the reply lands in
    the channel as a new top-level message, which is the single most common
    way an agent's answer gets separated from its question.
    """
    body = call(
        "chat.postMessage",
        {"channel": channel_id, "thread_ts": thread_ts, "text": text},
        json_body=True,
    )
    return body["ts"]


def main() -> None:
    channel_name = os.environ["SLACK_CHANNEL"]
    channel_id = find_channel(channel_name)

    try:
        messages = recent_messages(channel_id)
    except SlackError as error:
        if error.code == "not_in_channel":
            raise SystemExit(
                f"the bot is not a member of #{channel_name}. Invite it with "
                f"/invite @yourapp, or call conversations.join with channels:join."
            ) from error
        if error.code == "missing_scope":
            # Slack names the gap for you instead of making you guess.
            raise SystemExit(
                f"token needs {error.body.get('needed')}, has {error.body.get('provided')}"
            ) from error
        raise

    for message in messages:
        print(f"{message.get('ts')} {message.get('user', message.get('bot_id'))}: {message.get('text', '')[:120]}")

    if not messages:
        return

    parent = messages[0]
    # Reply under the thread this message belongs to, not under the reply
    # itself: a message inside a thread carries the parent's ts in thread_ts.
    thread_ts = parent.get("thread_ts", parent["ts"])
    post_reply(channel_id, thread_ts, "Acknowledged. A human is picking this up.")


if __name__ == "__main__":
    try:
        main()
    except InstallRevoked as error:
        # Delete the stored token and mark the workspace as needing a
        # re-install. Retrying a revoked token forever is the signature of an
        # integration that ignores revocation.
        raise SystemExit(f"install revoked ({error.code}); re-install required") from error
