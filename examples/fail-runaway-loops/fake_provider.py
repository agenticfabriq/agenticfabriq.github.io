"""An in-process stand-in for a rate-limited HTTP provider. No network, no clock.

Everything in this directory runs offline and deterministically. That is
deliberate: a guide about runaway loops should let you run the runaway, and you
cannot do that against somebody else's API.

Two substitutions make it work:

* `VirtualClock` replaces `time.monotonic` and `time.sleep`. Sleeping advances a
  number instead of a thread, so a run that backs off for four minutes of
  simulated time finishes in milliseconds and produces the same numbers on every
  machine. In production you inject `RealClock` and change nothing else.
* `FakeProvider` replaces the HTTP call. It enforces a fixed-window rate limit,
  returns `429` with a `Retry-After` header, can be put into an outage, and
  implements server-side idempotency the way Stripe documents it.

The idempotency semantics implemented below are Stripe's, from
https://docs.stripe.com/api/idempotent_requests and
https://docs.stripe.com/error-low-level#idempotency:

* the key travels in the `Idempotency-Key` header and is at most 255 characters;
* the result of the first request under a key is stored -- status code and body,
  success or failure -- and replayed to later requests carrying the same key;
* a replayed response is marked with `Idempotent-Replayed: true`;
* reusing a key with a request that does not match the first request's endpoint
  and parameters is an `idempotency_error`;
* a second request arriving while the first is still executing conflicts, and
  Stripe's status table gives `409 Conflict` for "the request conflicts with
  another request (perhaps due to using the same idempotent key)";
* stored results are pruned after 24 hours, after which the key is new again.

Run this file directly for a self-check of those semantics:

    python3 fake_provider.py
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60  # Stripe prunes keys at >= 24 hours old.
MAX_IDEMPOTENCY_KEY_LENGTH = 255  # "Idempotency keys are up to 255 characters long."


class TransportError(Exception):
    """The response never arrived. The write may or may not have committed.

    This is the only interesting failure for an idempotency key. A `500` is at
    least a response; a dropped connection leaves the client with no idea
    whether the server acted, which is exactly the state idempotency resolves.
    """


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class VirtualClock:
    """A monotonic clock you advance by sleeping. Deterministic and instant."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)
        self.total_slept = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("negative sleep")
        self._now += seconds
        self.total_slept += seconds


class RealClock:
    """The production clock. Same two methods, so nothing else changes."""

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        import time

        if seconds < 0:
            raise ValueError("negative sleep")
        time.sleep(seconds)


def _fingerprint(method: str, path: str, params: dict[str, Any]) -> str:
    """Hash of endpoint plus parameters, per Stripe's idempotency_error rule.

    Stripe: "Idempotency errors occur when an Idempotency-Key is re-used on a
    request that does not match the first request's API endpoint and
    parameters." So the fingerprint covers all three, canonicalised so that key
    ordering in a dict cannot make two identical requests look different.
    """
    canonical = json.dumps(
        {"method": method.upper(), "path": path, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _IdempotencyRecord:
    fingerprint: str
    status: int
    body: dict[str, Any]
    stored_at: float


class FakeProvider:
    """A provider with a fixed-window rate limit, outages, and idempotency.

    Counters are public because the whole point of the demos is to assert on
    them: `calls` is every request that reached the server, including the ones
    it rejected, which is the number a provider's abuse team sees.
    """

    def __init__(
        self,
        clock: VirtualClock | RealClock,
        *,
        limit: int = 20,
        window_seconds: float = 10.0,
        abuse_threshold: int | None = None,
        item_count: int = 25,
        latency_seconds: float = 0.12,
    ) -> None:
        self.clock = clock
        self.latency_seconds = float(latency_seconds)
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.item_count = item_count
        # After this many 429s the provider stops throttling and starts
        # refusing: the integration is disabled and a human has to ask for it
        # back. Providers really do this, and it is the outcome a run budget
        # exists to prevent you from reaching.
        self.abuse_threshold = abuse_threshold
        self.disabled = False
        self.rate_limited = 0
        self.timeouts = 0
        self.calls = 0
        self.status_counts: dict[int, int] = {}
        self.endpoint_counts: dict[str, int] = {}
        self.charges: list[dict[str, Any]] = []
        self.outage_until = 0.0
        self._window_start = clock.monotonic()
        self._window_calls = 0
        self._records: dict[str, _IdempotencyRecord] = {}
        self._inflight: set[str] = set()

    # -- test hooks -------------------------------------------------------

    def start_outage(self, seconds: float) -> None:
        self.outage_until = self.clock.monotonic() + seconds

    @contextmanager
    def concurrent_attempt(self, key: str) -> Iterator[None]:
        """Pretend another worker is mid-flight on `key`, so a duplicate 409s.

        The demos are single-threaded, so without this hook the concurrent
        branch below would never be exercised -- and an unexercised branch in a
        guide about retries is exactly the branch that is wrong.
        """
        self._inflight.add(key)
        try:
            yield
        finally:
            self._inflight.discard(key)

    # -- the server -------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        lose_the_response: bool = False,
        timeout: float | None = None,
    ) -> Response:
        params = dict(params or {})
        self.calls += 1
        endpoint = f"{method.upper()} {path}"
        self.endpoint_counts[endpoint] = self.endpoint_counts.get(endpoint, 0) + 1

        if timeout is not None and timeout < self.latency_seconds:
            # The client hung up first. Whether the server went on to finish the
            # work is unknowable from here -- this stand-in stops short, but a
            # real one might not, which is why the write still carries a key.
            self.clock.sleep(max(0.0, timeout))
            self.timeouts += 1
            raise TransportError(f"read timeout after {timeout:.2f}s on {endpoint}")
        self.clock.sleep(self.latency_seconds)
        now = self.clock.monotonic()

        if self.disabled:
            return self._record(
                Response(
                    403,
                    {},
                    {
                        "error": {
                            "type": "integration_disabled",
                            "message": "this integration has been disabled for abuse; contact support",
                        }
                    },
                )
            )

        # Rate limiting runs before the idempotency layer. Stripe says so
        # explicitly: "a request that's rate limited with a 429 can produce a
        # different result with the same idempotency key because rate limiters
        # run before the API's idempotency layer."
        if now - self._window_start >= self.window_seconds:
            self._window_start = now
            self._window_calls = 0
        self._window_calls += 1
        if self._window_calls > self.limit:
            wait = self._window_start + self.window_seconds - now
            retry_after = max(1, math.ceil(wait))
            self.rate_limited += 1
            if self.abuse_threshold is not None and self.rate_limited >= self.abuse_threshold:
                self.disabled = True
            return self._record(
                Response(
                    429,
                    {"Retry-After": str(retry_after)},
                    {"error": {"type": "rate_limit_error", "message": "too many requests"}},
                )
            )

        if now < self.outage_until:
            return self._record(
                Response(
                    503,
                    {"Retry-After": "2"},
                    {"error": {"type": "api_error", "message": "service unavailable"}},
                )
            )

        if method.upper() == "GET":
            # GET is idempotent by definition; Stripe says not to send a key on
            # one, and this server ignores it if you do.
            return self._answer(self._handle(method, path, params), endpoint, lose_the_response)

        if idempotency_key is None:
            # A write with no key. The server has no way to deduplicate it, so
            # every retry is a new operation. This is the default, and it is why
            # duplicate records are the quietest failure in this guide.
            return self._answer(self._handle(method, path, params), endpoint, lose_the_response)

        if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            return self._record(
                Response(
                    400,
                    {},
                    {"error": {"type": "invalid_request_error", "message": "idempotency key too long"}},
                )
            )

        self._prune(now)
        fingerprint = _fingerprint(method, path, params)
        record = self._records.get(idempotency_key)

        if record is not None:
            if record.fingerprint != fingerprint:
                return self._record(
                    Response(
                        400,
                        {},
                        {
                            "error": {
                                "type": "idempotency_error",
                                "message": (
                                    "Keys for idempotent requests can only be used with the same "
                                    "parameters they were first used with."
                                ),
                            }
                        },
                    )
                )
            return self._record(
                Response(record.status, {"Idempotent-Replayed": "true"}, dict(record.body))
            )

        if idempotency_key in self._inflight:
            return self._record(
                Response(
                    409,
                    {},
                    {
                        "error": {
                            "type": "idempotency_error",
                            "message": "a request with the same idempotency key is in progress",
                        }
                    },
                )
            )

        self._inflight.add(idempotency_key)
        try:
            response = self._handle(method, path, params)
            # Store before answering. The record has to survive the response
            # being lost, or idempotency protects only the cases that did not
            # need it.
            self._records[idempotency_key] = _IdempotencyRecord(
                fingerprint=fingerprint,
                status=response.status,
                body=dict(response.body),
                stored_at=now,
            )
        finally:
            self._inflight.discard(idempotency_key)

        return self._answer(response, endpoint, lose_the_response)

    def _answer(self, response: Response, endpoint: str, lose_the_response: bool) -> Response:
        """Record the outcome, then optionally drop the response on the floor.

        The server acted either way. That asymmetry -- committed on the server,
        unknown to the client -- is the entire reason idempotency keys exist.
        """
        self._record(response)
        if lose_the_response:
            raise TransportError(f"connection reset after {endpoint} committed")
        return response

    # -- endpoints --------------------------------------------------------

    def _handle(self, method: str, path: str, params: dict[str, Any]) -> Response:
        method = method.upper()
        if method == "GET" and path == "/items":
            return self._list_items(params)
        if method == "POST" and path == "/charges":
            return self._create_charge(params)
        return Response(
            404, {}, {"error": {"type": "invalid_request_error", "message": f"no route {method} {path}"}}
        )

    def _list_items(self, params: dict[str, Any]) -> Response:
        """A cursor-paginated list of `item_count` items, 10 per page.

        The last page carries `"next_cursor": null`. It is a real key with a
        null value, not an absent key -- which is the shape that turns a
        truthiness check into an infinite loop. See pagination_loop.py.
        """
        total = self.item_count
        page_size = 10
        try:
            start = int(params.get("cursor") or 0)
        except (TypeError, ValueError):
            return Response(
                400, {}, {"error": {"type": "invalid_request_error", "message": "bad cursor"}}
            )
        if start < 0 or start > total:
            return Response(
                400, {}, {"error": {"type": "invalid_request_error", "message": "bad cursor"}}
            )
        items = [{"id": f"item_{i:03d}"} for i in range(start, min(start + page_size, total))]
        next_start = start + len(items)
        return Response(
            200,
            {},
            {
                "items": items,
                "has_more": next_start < total,
                "next_cursor": str(next_start) if next_start < total else None,
            },
        )

    def _create_charge(self, params: dict[str, Any]) -> Response:
        invoice = params.get("invoice")
        amount = params.get("amount_cents")
        if not isinstance(invoice, str) or not isinstance(amount, int):
            return Response(
                400,
                {},
                {"error": {"type": "invalid_request_error", "message": "invoice and amount_cents required"}},
            )
        charge = {
            "id": f"ch_{len(self.charges) + 1:04d}",
            "invoice": invoice,
            "amount_cents": amount,
        }
        self.charges.append(charge)
        return Response(200, {}, charge)

    # -- bookkeeping ------------------------------------------------------

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, record in self._records.items()
            if now - record.stored_at >= IDEMPOTENCY_TTL_SECONDS
        ]
        for key in expired:
            del self._records[key]

    def _record(self, response: Response) -> Response:
        self.status_counts[response.status] = self.status_counts.get(response.status, 0) + 1
        return response

    def summary(self) -> str:
        statuses = ", ".join(f"{code}x{count}" for code, count in sorted(self.status_counts.items()))
        return f"provider saw {self.calls} calls [{statuses}], committed {len(self.charges)} charges"


def _self_check() -> None:
    clock = VirtualClock()
    provider = FakeProvider(clock, limit=1000, window_seconds=10.0)
    key = "run_1:step_3:charge"
    params = {"invoice": "inv_9", "amount_cents": 4200}

    first = provider.request("POST", "/charges", params=params, idempotency_key=key)
    assert first.status == 200, first
    assert "Idempotent-Replayed" not in first.headers

    replay = provider.request("POST", "/charges", params=params, idempotency_key=key)
    assert replay.status == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.body == first.body
    assert len(provider.charges) == 1, "replay must not commit a second charge"

    mismatch = provider.request(
        "POST", "/charges", params={**params, "amount_cents": 9999}, idempotency_key=key
    )
    assert mismatch.status == 400
    assert mismatch.body["error"]["type"] == "idempotency_error"

    with provider.concurrent_attempt("run_1:step_4:charge"):
        conflict = provider.request(
            "POST", "/charges", params=params, idempotency_key="run_1:step_4:charge"
        )
    assert conflict.status == 409, conflict

    # Same key, but 24 hours later: pruned, so it is a brand new request.
    clock.sleep(IDEMPOTENCY_TTL_SECONDS)
    after_ttl = provider.request("POST", "/charges", params=params, idempotency_key=key)
    assert after_ttl.status == 200
    assert "Idempotent-Replayed" not in after_ttl.headers
    assert len(provider.charges) == 2, "a pruned key is a new request, and that is the trap"

    print("idempotency semantics self-check passed")
    print(provider.summary())


if __name__ == "__main__":
    _self_check()
