"""The quiet failure: a retried write with no stable key charges the invoice twice.

There is no outage here and no alert fires. The run succeeds, the logs look
clean, and a month later somebody reconciling a ledger finds two rows.

Four clients, same lost response, same retry:

    python3 duplicate_writes.py

Idempotency semantics follow Stripe's, implemented server-side in
fake_provider.py: https://docs.stripe.com/api/idempotent_requests
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from fake_provider import FakeProvider, TransportError, VirtualClock

PARAMS: dict[str, Any] = {"invoice": "inv_2026_0042", "amount_cents": 129_00}


def _new_provider() -> tuple[FakeProvider, VirtualClock]:
    clock = VirtualClock()
    return FakeProvider(clock, limit=10_000, window_seconds=10.0), clock


def operation_key(*, tenant: str, operation: str, params: dict[str, Any]) -> str:
    """A key that identifies the *operation*, not the attempt.

    The hash covers the parameters, so a genuinely different charge gets a
    different key while every retry of this one gets the same key. `tenant`
    keeps two customers' identical-looking operations apart. Stripe caps keys at
    255 characters; this is 71.

    Do not put an email address, an account number, or anything else sensitive
    in the key. Stripe says so directly, and the key is echoed in logs and
    dashboards that the request body is usually scrubbed out of.
    """
    fingerprint = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    key = f"{tenant}:{operation}:{fingerprint}"
    assert len(key) <= 255
    return key


def charge_with(provider: FakeProvider, key_for_attempt) -> tuple[int, list[str]]:
    """Attempt the charge; the first response is lost in transit; retry once."""
    notes: list[str] = []
    for attempt in range(2):
        key = key_for_attempt(attempt)
        try:
            response = provider.request(
                "POST",
                "/charges",
                params=PARAMS,
                idempotency_key=key,
                lose_the_response=(attempt == 0),
            )
        except TransportError as exc:
            notes.append(f"attempt {attempt}: {exc}")
            continue
        replayed = response.headers.get("Idempotent-Replayed") == "true"
        notes.append(
            f"attempt {attempt}: {response.status} "
            f"{'(replayed)' if replayed else '(executed)'} {response.body}"
        )
        break
    return len(provider.charges), notes


def main() -> None:
    print("Every client below sends the same charge, loses the first response, and retries once.\n")

    provider, _ = _new_provider()
    charges, notes = charge_with(provider, lambda attempt: None)
    print("no idempotency key at all")
    for note in notes:
        print(f"  {note}")
    print(f"  -> charges committed: {charges}\n")

    provider, _ = _new_provider()
    charges, notes = charge_with(provider, lambda attempt: f"charge:{uuid.uuid4()}")
    print("fresh uuid4 per attempt (the subtle one: a key that is never reused is not a key)")
    for note in notes:
        print(f"  {note}")
    print(f"  -> charges committed: {charges}\n")

    provider, _ = _new_provider()
    stable = operation_key(tenant="acme", operation="charge_invoice", params=PARAMS)
    charges, notes = charge_with(provider, lambda attempt: stable)
    print(f"stable operation key ({stable})")
    for note in notes:
        print(f"  {note}")
    print(f"  -> charges committed: {charges}\n")

    # The same key with different parameters is an error, not a silent overwrite.
    mismatch = provider.request(
        "POST",
        "/charges",
        params={**PARAMS, "amount_cents": 999_00},
        idempotency_key=stable,
    )
    print("same key, different amount")
    print(f"  {mismatch.status} {mismatch.body['error']['type']}: {mismatch.body['error']['message']}")
    print(f"  -> charges committed: {len(provider.charges)}\n")

    # A re-plan later in the run is not a retry, but with a per-attempt key it
    # looks exactly like one to the server. With an operation-scoped key the
    # duplicate is refused for free.
    replan = provider.request("POST", "/charges", params=PARAMS, idempotency_key=stable)
    print("the agent re-plans and decides to charge the same invoice again")
    print(f"  {replan.status} replayed={replan.headers.get('Idempotent-Replayed')} {replan.body}")
    print(f"  -> charges committed: {len(provider.charges)}")
    assert len(provider.charges) == 1


if __name__ == "__main__":
    main()
