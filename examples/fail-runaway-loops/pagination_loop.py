"""Pagination that never terminates, and the four guards that make it terminate.

The provider in fake_provider.py serves 25 items, 10 per page, and the last page
carries `"next_cursor": null` -- a present key with a null value, which is the
shape that breaks the two most common cursor loops.

    python3 pagination_loop.py

Nothing here reaches the network. The buggy loops are stopped by the run budget,
which is the point: a budget is what converts "this hangs forever" into "this
returns an error you can alert on".
"""

from __future__ import annotations

import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from fake_provider import FakeProvider, VirtualClock
from guardrails import (
    BudgetExhausted,
    CircuitBreaker,
    GovernedClient,
    KillSwitch,
    RunBudget,
    RunLedger,
)

BUDGET = RunBudget(
    max_provider_calls=40,
    max_tokens=1_000_000,
    max_cost_usd=Decimal("5.00"),
    max_wall_clock_seconds=300.0,
)


def _client(name: str) -> tuple[GovernedClient, RunLedger, FakeProvider]:
    clock = VirtualClock()
    provider = FakeProvider(clock, limit=10_000, window_seconds=10.0)
    ledger = RunLedger(BUDGET, clock, run_id=name)
    client = GovernedClient(
        provider,
        ledger,
        CircuitBreaker(clock),
        KillSwitch(Path("kill-switch.json"), clock),
        clock,
        agent_id="pager",
        user_id="user_1",
        rng=random.Random(11),
    )
    return client, ledger, provider


def loop_forgets_the_cursor() -> tuple[int, str]:
    """Bug 1: trusts `has_more`, never sends the cursor. Page 1, forever."""
    client, ledger, _ = _client("forgets-cursor")
    pages = 0
    try:
        while True:
            body = client.call("GET", "/items", params={}).body
            pages += 1
            if not body["has_more"]:
                break
    except BudgetExhausted as exc:
        return pages, str(exc)
    return pages, "terminated"


def loop_checks_key_presence() -> tuple[int, str]:
    """Bug 2: `if "next_cursor" in body` -- true on the last page too.

    The key is present with a null value, so the loop sends `cursor=None`, the
    server reads that as "start from the beginning", and the walk restarts.
    """
    client, ledger, _ = _client("key-presence")
    pages = 0
    cursor: Any = None
    try:
        while True:
            body = client.call("GET", "/items", params={"cursor": cursor}).body
            pages += 1
            if "next_cursor" not in body:
                break
            cursor = body["next_cursor"]
    except BudgetExhausted as exc:
        return pages, str(exc)
    return pages, "terminated"


def guarded_walk() -> tuple[int, int, str]:
    """Four guards, any one of which alone would have stopped both bugs above.

    1. Terminate on the documented end condition -- the cursor's *value*, not
       the presence of its key.
    2. Terminate if the server hands back a cursor you already sent. A provider
       that echoes its input is the failure mode you cannot fix from your side.
    3. Terminate if a page returns no rows. No progress is a stop condition.
    4. Cap the page count anyway, well above the largest legitimate result set.
       Guards 1 to 3 are all beliefs about the provider's behaviour; this one is
       not.
    """
    client, ledger, _ = _client("guarded")
    max_pages = 100
    seen_cursors: set[str] = set()
    cursor: str | None = None
    items: list[dict[str, Any]] = []
    pages = 0

    while pages < max_pages:
        body = client.call("GET", "/items", params={"cursor": cursor}).body
        pages += 1
        page_items = body.get("items") or []
        if not page_items:
            return pages, len(items), "stopped: empty page, no progress"
        items.extend(page_items)

        nxt = body.get("next_cursor")
        if nxt is None:
            return pages, len(items), "stopped: next_cursor is null"
        if nxt in seen_cursors or nxt == cursor:
            return pages, len(items), f"stopped: cursor {nxt!r} repeated"
        seen_cursors.add(nxt)
        cursor = nxt

    return pages, len(items), f"stopped: page cap {max_pages} reached"


def main() -> None:
    pages, why = loop_forgets_the_cursor()
    print(f"bug 1 (never sends the cursor): {pages} pages, {why}")

    pages, why = loop_checks_key_presence()
    print(f"bug 2 ('next_cursor' in body):  {pages} pages, {why}")

    pages, count, why = guarded_walk()
    print(f"guarded walk:                   {pages} pages, {count} items, {why}")
    assert count == 25, count


if __name__ == "__main__":
    main()
