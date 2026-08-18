"""Verify a Slack request signature, then answer the Events API handshake.

Standard library only. Run it, point a tunnel at it, and paste the public URL
into your app's Event Subscriptions page:

    SLACK_SIGNING_SECRET=... python3 verify_signature.py

Slack signs every request it sends to a request URL with the app's signing
secret. The basestring is "v0:{timestamp}:{raw_body}", hashed with HMAC-SHA256,
and the result arrives as "v0={hex}" in the X-Slack-Signature header alongside
X-Slack-Request-Timestamp. Slack's own guidance is to reject a timestamp more
than five minutes from local time and to compare with an HMAC compare function
rather than ==.

The raw bytes matter. Sign what arrived on the wire, before any JSON parsing or
re-encoding, or a semantically identical body will produce a different digest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"].encode("utf-8")
MAX_SKEW_SECONDS = 60 * 5  # Slack's documented five-minute replay window.
MAX_BODY_BYTES = 2 * 1024 * 1024

# Slack retries a failed delivery up to three times and re-sends the same
# event_id. Anything with a side effect must be idempotent. This in-memory set
# is per-process and therefore only correct for a single replica; a fleet needs
# a shared store with a TTL longer than Slack's five-minute final retry.
SEEN_EVENT_IDS: set[str] = set()


def is_signature_valid(headers, raw_body: bytes, now: float | None = None) -> bool:
    """Return True only if this request really came from Slack, recently."""
    timestamp = headers.get("X-Slack-Request-Timestamp")
    signature = headers.get("X-Slack-Signature")
    if not timestamp or not signature:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False

    now = time.time() if now is None else now
    if abs(now - sent_at) > MAX_SKEW_SECONDS:
        # A captured request replayed later fails here and nowhere else.
        return False

    basestring = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    digest = hmac.new(SIGNING_SECRET, basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def handle_event(payload: dict) -> None:
    """Do the slow work here, off the request thread in anything real."""
    event = payload.get("event", {})
    if event.get("type") == "app_mention":
        print(f"mention in {event.get('channel')} by {event.get('user')}: {event.get('text')}")
    elif event.get("type") == "tokens_revoked":
        # tokens.oauth holds user ids whose xoxp- tokens died, tokens.bot the
        # user ids behind revoked xoxb- tokens. Either way: stop calling.
        print(f"revoked: {event.get('tokens')}")
    else:
        print(f"unhandled event type {event.get('type')!r}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send(413)
            return
        raw_body = self.rfile.read(length)

        if not is_signature_valid(self.headers, raw_body):
            self._send(401, b"bad signature")
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b"unparseable body")
            return

        if payload.get("type") == "url_verification":
            # One-time handshake when you save the request URL. Echo the
            # challenge back exactly.
            self._send(200, payload.get("challenge", "").encode("utf-8"))
            return

        event_id = payload.get("event_id")
        if event_id in SEEN_EVENT_IDS:
            self._send(200)  # A retry of something already handled.
            return
        if event_id:
            SEEN_EVENT_IDS.add(event_id)

        # Acknowledge first. Slack requires an HTTP 2xx within three seconds,
        # and an app that fails more than 95% of deliveries in 60 minutes has
        # its subscriptions temporarily disabled.
        self._send(200)
        handle_event(payload)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    print(f"listening on http://127.0.0.1:{port}/slack/events")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
