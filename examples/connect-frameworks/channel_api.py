"""Framework-independent half of the tool: credentials and the HTTP call.

The three framework files in this directory all import from here, so the only
thing that differs between them is how the framework hands the tool a user
identity at call time. Nothing in this module reads a credential at import
time, which is the whole point.

Requires: pip install httpx
"""

from __future__ import annotations

import os

import httpx

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# Slack returns HTTP 200 with {"ok": false, "error": "<code>"} for application
# errors, so a raise_for_status() alone will never see them.
# https://docs.slack.dev/reference/methods/chat.postMessage/
RETRYABLE_CODES = frozenset({"ratelimited", "rate_limited"})

# Codes whose text is safe to hand to a model: they name a state of the
# workspace, not a state of your infrastructure. Anything outside this set is
# reported to the model as a bare category and logged in full on your side.
MODEL_SAFE_CODES = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "channel_archived",
        "is_archived",
        "msg_too_long",
        "no_text",
        "restricted_action",
    }
)


class ChannelError(Exception):
    """A chat.postMessage call that came back with ok: false."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


class CredentialMissing(Exception):
    """No usable token is stored for this user."""


def resolve_token(user_id: str) -> str:
    """Return the chat token to act as `user_id`, fetched at call time.

    Replace the body with a read from your secret manager. Two properties
    matter and neither depends on which store you pick: the lookup is keyed by
    the user the agent is acting for, and it happens inside the call rather
    than at import time. Cache the result for less than its lifetime if the
    lookup is expensive; never cache it in a module-level global.
    """
    token = os.environ.get(f"CHANNEL_TOKEN_{user_id.upper()}")
    if not token:
        raise CredentialMissing(user_id)
    return token


def post_message(token: str, channel: str, text: str) -> str:
    """Post `text` to `channel` as the owner of `token`. Returns the message ts.

    Raises ChannelError on an application-level failure and httpx.HTTPError on
    a transport-level one. Both carry detail that must not reach a model
    verbatim; see model_safe_text below.
    """
    response = httpx.post(
        POST_MESSAGE_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": channel, "text": text},
        timeout=10.0,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise ChannelError(body.get("error", "unknown_error"))
    return body["ts"]


async def post_message_async(token: str, channel: str, text: str) -> str:
    """Async twin of post_message, for frameworks that run tools concurrently."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            POST_MESSAGE_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
        )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise ChannelError(body.get("error", "unknown_error"))
    return body["ts"]


def model_safe_text(exc: Exception) -> str:
    """Render an exception as something it is safe to put in a prompt.

    Every branch returns a string this file chose. None of them interpolate
    str(exc) for an unknown error, because that string is written by an
    upstream service and by your own stack, and it lands in the model's
    context window where it is indistinguishable from an instruction.
    """
    if isinstance(exc, ChannelError):
        if exc.code in MODEL_SAFE_CODES:
            return f"post_message failed: {exc.code}"
        return "post_message failed: the workspace rejected the call"
    if isinstance(exc, CredentialMissing):
        return "post_message failed: this user has not connected their chat account"
    if isinstance(exc, httpx.TimeoutException):
        return "post_message failed: the chat service did not respond in time"
    return "post_message failed: internal error"
