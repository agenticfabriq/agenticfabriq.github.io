"""One buggy agent, two harnesses. The bug is identical; only the harness differs.

The agent settles a queue of invoices. Its planner is broken in the way planners
break: "am I done?" is a judgement call rather than a checkable predicate, so it
finishes a pass, decides it had better verify, and starts the pass again.

    python3 runaway_agent.py

Five sections run in order:

  baseline   -- 200 healthy runs, so the budget comes from measurement
  ungoverned -- the broken planner with naive retries and no budget
  governed   -- the same broken planner inside a run budget
  breaker    -- a dependency outage, and what the breaker saves
  killswitch -- stopping one agent for one user, mid-run, without a deploy

Every number printed is produced by running the code. Nothing here touches the
network; see fake_provider.py for what stands in for it.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import tempfile
import textwrap
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from duplicate_writes import operation_key
from fake_provider import FakeProvider, Response, TransportError, VirtualClock
from guardrails import (
    BudgetExhausted,
    CallCost,
    CircuitBreaker,
    CircuitOpen,
    DailySpendLedger,
    GovernedClient,
    Killed,
    KillSwitch,
    RunBudget,
    RunLedger,
    ToolFailed,
)

AGENT_ID = "invoice-settler"
USER_ID = "user_42"
TODAY = "2026-08-18"

# Placeholder prices. Substitute the numbers your model and API providers
# publish; the point of the arithmetic is the shape, not these figures.
COST = CallCost(tokens=900, usd=Decimal("0.0090"))


class IntegrationDisabled(Exception):
    """The provider stopped you. There is no retry for this one."""


# --------------------------------------------------------------------------
# The agent. It is the same object in every scenario below.
# --------------------------------------------------------------------------


CallFn = Callable[..., Response]


def settle_queue(call: CallFn, *, checkable_done: bool, max_passes: int = 10_000) -> dict[str, Any]:
    """Walk the queue and settle every invoice, then decide whether to stop.

    `checkable_done=True` stops on a predicate a machine can evaluate: every id
    the listing returned is in the settled set, and the last pass settled
    nothing new. `checkable_done=False` is the bug -- the planner has no such
    predicate, so "verify once more" always wins and the pass repeats.
    """
    settled: set[str] = set()
    passes = 0

    while passes < max_passes:
        passes += 1
        seen: set[str] = set()
        cursor: str | None = None
        newly_settled = 0

        while True:
            body = call("GET", "/items", params={"cursor": cursor}).body
            items = body.get("items") or []
            for item in items:
                item_id = item["id"]
                seen.add(item_id)
                params = {"invoice": item_id, "amount_cents": 100}
                key = operation_key(tenant=USER_ID, operation="settle", params=params)
                response = call("POST", "/charges", params=params, idempotency_key=key)
                if response.headers.get("Idempotent-Replayed") != "true":
                    newly_settled += 1
                settled.add(item_id)
            nxt = body.get("next_cursor")
            if nxt is None or nxt == cursor:
                break
            cursor = nxt

        if checkable_done and seen <= settled and newly_settled == 0:
            break

    return {"passes": passes, "settled": len(settled)}


# --------------------------------------------------------------------------
# The naive harness: retries at two layers, fixed backoff, no budget.
# --------------------------------------------------------------------------


class NaiveClient:
    """What an agent gets when nobody thinks about the retry layer.

    Two nested retry loops, three attempts each, one second of flat backoff, no
    jitter, no budget, no breaker, and no idempotency key on writes -- the
    default shape of a tool wrapper somebody added a retry to twice.
    """

    def __init__(self, provider: FakeProvider, clock: VirtualClock) -> None:
        self.provider = provider
        self.clock = clock
        self.attempts = 0

    def _once(self, method: str, path: str, params: dict[str, Any] | None) -> Response:
        self.attempts += 1
        # Note what is missing: idempotency_key. The write below is retried
        # anyway, because retrying is what the wrapper does.
        return self.provider.request(method, path, params=params)

    def __call__(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        for _outer in range(3):
            for _inner in range(3):
                try:
                    response = self._once(method, path, params)
                except TransportError:
                    self.clock.sleep(1.0)
                    continue
                if response.ok:
                    return response
                if response.status == 403:
                    raise IntegrationDisabled(response.body["error"]["message"])
                self.clock.sleep(1.0)  # flat backoff: every client wakes together
        raise RuntimeError(f"{method} {path} gave up after 9 attempts")


# --------------------------------------------------------------------------
# 1. Baseline. Set budgets from a measured healthy run, not from taste.
# --------------------------------------------------------------------------


def baseline(runs: int = 200, seed: int = 20260818) -> dict[str, Any]:
    rng = random.Random(seed)
    calls_per_run: list[int] = []
    cost_per_run: list[float] = []
    seconds_per_run: list[float] = []

    for index in range(runs):
        clock = VirtualClock()
        provider = FakeProvider(
            clock, limit=10_000, window_seconds=10.0, item_count=rng.randint(12, 34)
        )
        counter = {"n": 0}

        def call(method: str, path: str, **kwargs: Any) -> Response:
            counter["n"] += 1
            return provider.request(method, path, **kwargs)

        started = clock.monotonic()
        settle_queue(call, checkable_done=True)
        calls_per_run.append(counter["n"])
        cost_per_run.append(float(COST.usd) * counter["n"])
        seconds_per_run.append(clock.monotonic() - started)

    def pct(values: list[float], q: float) -> float:
        ordered = sorted(values)
        rank = max(0, math.ceil(q * len(ordered)) - 1)
        return ordered[rank]

    return {
        "runs": runs,
        "calls_p50": pct(calls_per_run, 0.50),
        "calls_p95": pct(calls_per_run, 0.95),
        "calls_p99": pct(calls_per_run, 0.99),
        "calls_max": max(calls_per_run),
        "cost_p99": pct(cost_per_run, 0.99),
        "seconds_p99": pct(seconds_per_run, 0.99),
        "calls_mean": statistics.fmean(calls_per_run),
    }


def budget_from(measured: dict[str, Any], *, wall_clock_seconds: float = 120.0) -> RunBudget:
    """Alert at p99, stop at roughly twice p99.

    Two multiples, two purposes. The alert threshold sits just above the worst
    healthy run so a real regression pages someone. The hard budget sits far
    enough above the alert that a legitimately large queue does not get killed,
    and far enough below a runaway that the provider never notices.

    Calls, tokens, and cost all come off the same measured distribution. Wall
    clock does not, and pretending otherwise is a mistake: a healthy run never
    backs off, so its duration says nothing about how long a legitimate run that
    hits throttling should be allowed to take. Set the wall-clock ceiling from
    how long the answer is still worth having -- if the invoice run has not
    finished in two minutes, nobody wants it -- and treat it as the last wall,
    not the first.
    """
    calls = int(math.ceil(measured["calls_p99"] * 2))
    return RunBudget(
        max_provider_calls=calls,
        max_tokens=COST.tokens * calls,
        max_cost_usd=(COST.usd * calls).quantize(Decimal("0.0001")),
        max_wall_clock_seconds=wall_clock_seconds,
    )


# --------------------------------------------------------------------------
# 2. Ungoverned. The provider is the thing that stops it.
# --------------------------------------------------------------------------


def ungoverned() -> dict[str, Any]:
    clock = VirtualClock()
    provider = FakeProvider(
        clock, limit=20, window_seconds=10.0, item_count=25, abuse_threshold=60
    )
    client = NaiveClient(provider, clock)
    outcome = "completed"
    try:
        settle_queue(client, checkable_done=False)
    except IntegrationDisabled as exc:
        outcome = f"provider disabled the integration: {exc}"
    except RuntimeError as exc:
        outcome = f"gave up: {exc}"
    return {
        "outcome": outcome,
        "provider_calls": provider.calls,
        "rate_limited": provider.status_counts.get(429, 0),
        "forbidden": provider.status_counts.get(403, 0),
        "charges_committed": len(provider.charges),
        "distinct_invoices": len({charge["invoice"] for charge in provider.charges}),
        "elapsed": f"{clock.monotonic() - 1000.0:.2f}s",
        "cost_usd": f"{float(COST.usd) * provider.calls:.4f}",
    }


# --------------------------------------------------------------------------
# 3. Governed. The budget is the thing that stops it.
# --------------------------------------------------------------------------


def governed(budget: RunBudget, kill_switch_path: Path) -> dict[str, Any]:
    clock = VirtualClock()
    provider = FakeProvider(
        clock, limit=20, window_seconds=10.0, item_count=25, abuse_threshold=60
    )
    ledger = RunLedger(budget, clock, run_id="run_7f31")
    events: list[dict[str, Any]] = []
    client = GovernedClient(
        provider,
        ledger,
        CircuitBreaker(clock, failure_threshold=5, reset_timeout=30.0),
        KillSwitch(kill_switch_path, clock),
        clock,
        agent_id=AGENT_ID,
        user_id=USER_ID,
        rng=random.Random(4242),
        daily=DailySpendLedger(cap_usd=Decimal("2.00")),
        day=TODAY,
        on_event=events.append,
    )

    outcome = "completed"
    try:
        settle_queue(lambda *a, **kw: client.call(*a, cost=COST, **kw), checkable_done=False)
    except BudgetExhausted as exc:
        outcome = str(exc)
    except (ToolFailed, CircuitOpen, Killed) as exc:
        outcome = f"{type(exc).__name__}: {exc}"

    return {
        "outcome": outcome,
        "provider_calls": provider.calls,
        "rate_limited": provider.status_counts.get(429, 0),
        "disabled": provider.disabled,
        "charges_committed": len(provider.charges),
        "distinct_invoices": len({charge["invoice"] for charge in provider.charges}),
        "idempotent_replays": sum(1 for event in events if event["replayed"]),
        "elapsed": f"{clock.monotonic() - 1000.0:.2f}s",
        "cost_usd": f"{ledger.cost_usd:.4f}",
        "stopped_by": ledger.stopped_by,
    }


# --------------------------------------------------------------------------
# 4. Circuit breaker under a real outage.
# --------------------------------------------------------------------------


def breaker_demo(kill_switch_path: Path) -> dict[str, Any]:
    clock = VirtualClock()
    provider = FakeProvider(clock, limit=10_000, window_seconds=10.0, item_count=25)
    provider.start_outage(200.0)
    budget = RunBudget(
        max_provider_calls=400,
        max_tokens=10_000_000,
        max_cost_usd=Decimal("10.00"),
        max_wall_clock_seconds=600.0,
    )
    ledger = RunLedger(budget, clock, run_id="run_outage")
    breaker = CircuitBreaker(clock, failure_threshold=5, reset_timeout=30.0, success_threshold=2)
    client = GovernedClient(
        provider,
        ledger,
        breaker,
        KillSwitch(kill_switch_path, clock),
        clock,
        agent_id=AGENT_ID,
        user_id=USER_ID,
        rng=random.Random(99),
    )

    attempted = 0
    short_circuited = 0
    succeeded = 0
    outcome = "completed"
    try:
        for _ in range(60):
            attempted += 1
            try:
                if client.call("GET", "/items", params={}, cost=COST).ok:
                    succeeded += 1
            except CircuitOpen:
                short_circuited += 1
            except ToolFailed:
                pass
            # The agent has other work; the scheduler comes back in five
            # seconds. Without this the breaker's reset timeout never elapses,
            # because a short-circuited call takes no time at all.
            clock.sleep(5.0)
    except BudgetExhausted as exc:
        outcome = str(exc)

    return {
        "outcome": outcome,
        "logical_calls": attempted,
        "provider_calls": provider.calls,
        "short_circuited": short_circuited,
        "succeeded": succeeded,
        "breaker_state": breaker.state,
        "transitions": [state for _, state in breaker.transitions],
    }


# --------------------------------------------------------------------------
# 4b. The reservation a short-circuited call must give back.
# --------------------------------------------------------------------------


def reservation_demo(kill_switch_path: Path) -> dict[str, Any]:
    """Twenty calls into an open breaker. The daily cap must be untouched.

    This is the arithmetic that decides whether your daily cap survives an
    outage. The reservation is taken before the breaker is consulted, so if
    nothing gives it back, a breaker-open storm spends a user's entire day on
    calls the provider never received.
    """
    clock = VirtualClock()
    provider = FakeProvider(clock, limit=10_000, window_seconds=10.0, item_count=25)
    provider.start_outage(10_000.0)
    budget = RunBudget(
        max_provider_calls=1000,
        max_tokens=10_000_000,
        max_cost_usd=Decimal("100.00"),
        max_wall_clock_seconds=10_000.0,
    )
    ledger = RunLedger(budget, clock, run_id="run_reserve")
    breaker = CircuitBreaker(clock, failure_threshold=1, reset_timeout=10_000.0)
    daily = DailySpendLedger(cap_usd=Decimal("100.00"))
    client = GovernedClient(
        provider,
        ledger,
        breaker,
        KillSwitch(kill_switch_path, clock),
        clock,
        agent_id=AGENT_ID,
        user_id=USER_ID,
        rng=random.Random(17),
        daily=daily,
        day=TODAY,
    )

    # Trip the breaker directly so nothing else can account for the spend, then
    # make twenty calls that never leave the process.
    breaker.record_failure()
    assert breaker.state == "open"

    short_circuited = 0
    for _ in range(20):
        try:
            client.call("GET", "/items", params={}, cost=COST)
        except CircuitOpen:
            short_circuited += 1

    return {
        "short_circuited": short_circuited,
        "provider_calls": provider.calls,
        "run_ledger_calls": ledger.provider_calls,
        "daily_spend_usd": f"{daily.total(USER_ID, TODAY):.4f}",
        "daily_cap_usd": f"{daily.cap_usd:.4f}",
    }


# --------------------------------------------------------------------------
# 5. Kill switch, flipped mid-run.
# --------------------------------------------------------------------------


def kill_switch_demo(path: Path) -> dict[str, Any]:
    clock = VirtualClock()
    provider = FakeProvider(clock, limit=10_000, window_seconds=10.0, item_count=25)
    budget = RunBudget(
        max_provider_calls=500,
        max_tokens=10_000_000,
        max_cost_usd=Decimal("10.00"),
        max_wall_clock_seconds=600.0,
    )
    ledger = RunLedger(budget, clock, run_id="run_kill")
    switch = KillSwitch(path, clock, ttl=1.0)
    client = GovernedClient(
        provider,
        ledger,
        CircuitBreaker(clock),
        switch,
        clock,
        agent_id=AGENT_ID,
        user_id=USER_ID,
        rng=random.Random(5),
    )

    path.write_text(json.dumps({"pairs": []}), encoding="utf-8")
    calls = 0
    outcome = "completed"
    try:
        for index in range(200):
            if index == 12:
                # An on-call engineer runs one command against the control
                # plane. No build, no rollout, no restart.
                path.write_text(
                    json.dumps({"pairs": [[AGENT_ID, USER_ID]]}), encoding="utf-8"
                )
            client.call("GET", "/items", params={}, cost=COST)
            calls += 1
    except Killed as exc:
        outcome = str(exc)

    other_user = GovernedClient(
        provider,
        RunLedger(budget, clock, run_id="run_other"),
        CircuitBreaker(clock),
        KillSwitch(path, clock, ttl=1.0),
        clock,
        agent_id=AGENT_ID,
        user_id="user_77",
        rng=random.Random(6),
    )
    other_ok = other_user.call("GET", "/items", params={}, cost=COST).ok

    return {
        "outcome": outcome,
        "flag_set_before_call": 12,
        "calls_before_stop": calls,
        "cache_ttl_seconds": switch.ttl,
        "other_user_still_running": other_ok,
    }


def main() -> None:
    measured = baseline()
    print("== baseline: 200 healthy runs, checkable done predicate ==")
    print(
        f"  provider calls per run  p50={measured['calls_p50']} "
        f"p95={measured['calls_p95']} p99={measured['calls_p99']} max={measured['calls_max']}"
    )
    print(f"  cost per run p99        ${measured['cost_p99']:.4f}")
    print(f"  wall clock per run p99  {measured['seconds_p99']:.2f}s")
    budget = budget_from(measured)
    print(f"  derived budget          {budget}")
    print(f"  alert threshold         {measured['calls_p99']} calls per run\n")

    with tempfile.TemporaryDirectory() as tmp:
        kill_switch_path = Path(tmp) / "kill-switch.json"
        kill_switch_path.write_text("{}", encoding="utf-8")

        print("== ungoverned: same broken planner, naive retries, no budget ==")
        bad = ungoverned()
        for key, value in bad.items():
            print(f"  {key}: {value}")
        print()

        print("== governed: same broken planner, inside the budget ==")
        good = governed(budget, kill_switch_path)
        for key, value in good.items():
            print(f"  {key}: {value}")
        print()

        print("== circuit breaker: dependency down for 200s, agent polls every 5s ==")
        outage = breaker_demo(kill_switch_path)
        for key, value in outage.items():
            if key == "transitions":
                # Wrapped here rather than in the terminal, so a transcript of
                # this run can be pasted anywhere without being reflowed.
                for line in textwrap.wrap(
                    ", ".join(value),
                    width=72,
                    initial_indent=f"  {key}: ",
                    subsequent_indent="    ",
                ):
                    print(line)
                continue
            print(f"  {key}: {value}")
        print()

        print("== reservations: 20 calls into an open breaker ==")
        reserved = reservation_demo(kill_switch_path)
        for key, value in reserved.items():
            print(f"  {key}: {value}")
        print()

        print("== kill switch: stop one agent for one user, mid-run ==")
        killed = kill_switch_demo(Path(tmp) / "switch.json")
        for key, value in killed.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
