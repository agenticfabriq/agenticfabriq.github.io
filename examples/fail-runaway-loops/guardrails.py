"""The four controls that bound an agent run: budget, backoff, breaker, kill switch.

Nothing here is agent-specific. It is the retry layer a well-behaved API client
has always needed, plus a per-run ledger, because an agent decides how many
calls to make at runtime and a hand-written client does not.

Two rules the code enforces that are easy to get wrong:

1. Retries are attempted at exactly one layer -- this one. The agent loop above
   must treat a raised `ToolFailed` as final for that step. Google's SRE book
   puts it plainly: "a failed request from the DB Frontend should only be
   retried by Backend B, the layer immediately above it. If multiple layers
   retried, we'd have a combinatorial explosion."
   https://sre.google/sre-book/handling-overload/
2. Every attempt, including every retry, spends budget. A budget that counts
   logical tool calls but not the retries underneath them does not bound the
   number of requests the provider sees, which is the only number that matters
   to the provider.

Run this file directly to execute its self-checks:

    python3 guardrails.py
"""

from __future__ import annotations

import json
import random
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from fake_provider import RealClock, Response, TransportError, VirtualClock

Clock = VirtualClock | RealClock

# RFC 9110 section 10.2.3: "delay-seconds = 1*DIGIT", "a non-negative decimal
# integer". str.isdigit() is wrong here -- it accepts non-ASCII digits such as
# U+0663 ARABIC-INDIC DIGIT THREE, which int() then happily parses.
_DELAY_SECONDS_RE = re.compile(r"^[0-9]+$")

# Statuses that mean "the dependency is unhealthy, come back later". A 400 or a
# 422 means the agent sent something wrong; retrying it burns budget and opening
# a breaker on it hides the bug behind a plausible-looking outage.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class RunStopped(Exception):
    """Base class for every reason a run halts on purpose."""


class BudgetExhausted(RunStopped):
    def __init__(self, limit_name: str, limit: Any, observed: Any) -> None:
        super().__init__(f"budget exhausted: {limit_name} limit {limit}, would reach {observed}")
        self.limit_name = limit_name
        self.limit = limit
        self.observed = observed


class CircuitOpen(RunStopped):
    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


class Killed(RunStopped):
    pass


class ToolFailed(Exception):
    """The call failed after this layer exhausted its own retries. Do not retry."""

    def __init__(self, message: str, *, attempts: int, last_status: int | None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------

# 0.5 * 2 ** 1024 raises "OverflowError: int too large to convert to float".
# Nothing should ever retry a thousand times, but "should" is what this whole
# guide is about, so the exponent is clamped rather than trusted.
_MAX_EXPONENT = 30


def full_jitter_delay(attempt: int, *, base: float, cap: float, rng: random.Random) -> float:
    """AWS "Full Jitter": sleep = random_between(0, min(cap, base * 2 ** attempt)).

    From https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/,
    whose own simulator spells it out at
    https://github.com/aws-samples/aws-arch-backoff-simulator/blob/master/src/backoff_simulator.py:

        def expo(self, n):
            return min(self.cap, pow(2, n) * self.base)

        class ExpoBackoffFullJitter(Backoff):
            def backoff(self, n):
                v = self.expo(n)
                return random.uniform(0, v)

    The article's ranking is narrower than it is usually reported. Un-jittered
    exponential backoff is "the clear loser"; Equal Jitter "does slightly more
    work than 'Full Jitter', and takes much longer"; and against Decorrelated
    Jitter, "the 'Full Jitter' approach uses less work, but slightly more time".

    `attempt` is 0 for the first retry, so the first ceiling is `base`. The
    window starts at zero, not at the previous ceiling: that is what
    decorrelates clients that all failed at the same instant, and it is why a
    fixed `base * 2 ** attempt` sleep -- backoff with no jitter -- keeps a herd
    synchronised no matter how long the sleeps get.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    if base <= 0 or cap <= 0:
        raise ValueError("base and cap must be > 0")
    ceiling = min(cap, base * (2 ** min(attempt, _MAX_EXPONENT)))
    return rng.uniform(0.0, ceiling)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header. RFC 9110 s10.2.3: `HTTP-date / delay-seconds`.

    Returns seconds to wait, or None if the header is absent or unparseable.
    An HTTP-date already in the past yields 0.0, not a negative sleep.
    """
    if value is None:
        return None
    raw = value.strip()
    if _DELAY_SECONDS_RE.match(raw):
        return float(int(raw))
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:  # HTTP-date is GMT by definition
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (when - reference).total_seconds())


def next_delay(
    attempt: int,
    response: Response | None,
    *,
    base: float,
    cap: float,
    rng: random.Random,
    jitter_window: float = 1.0,
) -> float:
    """Combine the server's instruction with client-side jitter.

    Retry-After is a floor, not a suggestion: sleeping `random(0, base)` when the
    server asked for 30 seconds is how a client earns a 403. But every throttled
    client reading the same `Retry-After: 30` wakes at the same instant, which
    rebuilds the herd the jitter was there to break up. So honour the floor and
    scatter above it.
    """
    jittered = full_jitter_delay(attempt, base=base, cap=cap, rng=rng)
    if response is None:
        return jittered
    server_floor = parse_retry_after(response.headers.get("Retry-After"))
    if server_floor is None:
        return jittered
    return server_floor + rng.uniform(0.0, jitter_window)


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunBudget:
    """Hard ceilings for one run. Exceeding any of them ends the run.

    Set every number from a measured healthy run, not from taste. `baseline()`
    in runaway_agent.py shows the measurement.
    """

    max_provider_calls: int
    max_tokens: int
    max_cost_usd: Decimal
    max_wall_clock_seconds: float


@dataclass
class DailySpendLedger:
    """Per-user spend for the day: reserve, then either reconcile or release.

    Reserve-then-reconcile is not pedantry. Charging after the response means N
    concurrent workers can each pass the check and each then spend, so the cap
    is exceeded by a factor of N. In production this is a row per (user, day)
    and an atomic `UPDATE ... SET reserved = reserved + $1 WHERE reserved + $1
    <= cap` that returns zero rows when the cap is hit.

    The half that is easy to forget is `release()`. A reservation is a promise
    to spend, and every path that decides not to spend has to take the promise
    back. The dangerous one is not an exception you expected -- it is the
    circuit breaker short-circuiting after the reservation is taken, because
    that happens hundreds of times in a row during exactly the outage the cap
    exists to survive. A leaked reservation turns the daily cap into a second
    outage of your own manufacture.
    """

    cap_usd: Decimal
    spent: dict[tuple[str, str], Decimal] = field(default_factory=dict)

    def reserve(self, user_id: str, day: str, amount: Decimal) -> None:
        key = (user_id, day)
        current = self.spent.get(key, Decimal("0"))
        if current + amount > self.cap_usd:
            raise BudgetExhausted("daily_spend_usd", self.cap_usd, current + amount)
        self.spent[key] = current + amount

    def release(self, user_id: str, day: str, amount: Decimal) -> None:
        """Hand back a reservation for a call that never reached the provider."""
        key = (user_id, day)
        remaining = self.spent.get(key, Decimal("0")) - amount
        if remaining < 0:
            # Releasing more than was reserved means something released twice.
            # Fail loudly: a silent clamp here hides a cap that no longer holds.
            raise ValueError(f"released {amount} but only {self.spent.get(key)} was reserved")
        self.spent[key] = remaining

    def reconcile(self, user_id: str, day: str, reserved: Decimal, actual: Decimal) -> None:
        """Swap the up-front estimate for what the attempt actually cost."""
        if actual < 0 or reserved < 0:
            raise ValueError("amounts must be >= 0")
        self.release(user_id, day, reserved)
        key = (user_id, day)
        self.spent[key] = self.spent.get(key, Decimal("0")) + actual

    def total(self, user_id: str, day: str) -> Decimal:
        return self.spent.get((user_id, day), Decimal("0"))


class RunLedger:
    """Live counters for one run, checked before every attempt."""

    def __init__(self, budget: RunBudget, clock: Clock, *, run_id: str = "run") -> None:
        self.budget = budget
        self.clock = clock
        self.run_id = run_id
        self.started_at = clock.monotonic()
        self.provider_calls = 0
        self.tokens = 0
        self.cost_usd = Decimal("0")
        self.stopped_by: str | None = None

    @property
    def deadline(self) -> float:
        return self.started_at + self.budget.max_wall_clock_seconds

    @property
    def seconds_left(self) -> float:
        return self.deadline - self.clock.monotonic()

    def check_before_call(self, *, tokens: int, cost_usd: Decimal) -> None:
        """Raise if this attempt would cross a limit. Called before the request.

        Checking afterwards is checking whether you have already overspent.
        """
        if self.provider_calls + 1 > self.budget.max_provider_calls:
            self.stopped_by = "provider_calls"
            raise BudgetExhausted(
                "provider_calls", self.budget.max_provider_calls, self.provider_calls + 1
            )
        if self.tokens + tokens > self.budget.max_tokens:
            self.stopped_by = "tokens"
            raise BudgetExhausted("tokens", self.budget.max_tokens, self.tokens + tokens)
        if self.cost_usd + cost_usd > self.budget.max_cost_usd:
            self.stopped_by = "cost_usd"
            raise BudgetExhausted("cost_usd", self.budget.max_cost_usd, self.cost_usd + cost_usd)
        if self.seconds_left <= 0:
            self.stopped_by = "wall_clock"
            raise BudgetExhausted(
                "wall_clock_seconds",
                self.budget.max_wall_clock_seconds,
                self.clock.monotonic() - self.started_at,
            )

    def check_sleep(self, seconds: float) -> None:
        """A backoff that outlives the run is a run that never returns."""
        if self.clock.monotonic() + seconds > self.deadline:
            self.stopped_by = "wall_clock"
            raise BudgetExhausted(
                "wall_clock_seconds",
                self.budget.max_wall_clock_seconds,
                self.clock.monotonic() + seconds - self.started_at,
            )

    def spend(self, *, tokens: int, cost_usd: Decimal) -> None:
        self.provider_calls += 1
        self.tokens += tokens
        self.cost_usd += cost_usd

    def summary(self) -> str:
        return (
            f"{self.run_id}: {self.provider_calls} provider calls, {self.tokens} tokens, "
            f"${self.cost_usd:.4f}, {self.clock.monotonic() - self.started_at:.1f}s"
        )


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class CircuitBreaker:
    """Three states, per Martin Fowler's CircuitBreaker: closed, open, half-open.

    https://martinfowler.com/bliki/CircuitBreaker.html

    * closed   -- calls pass through. Consecutive failures are counted; a
                  success resets the count to zero. On reaching
                  `failure_threshold` the breaker trips to open.
    * open     -- "All further calls to the circuit breaker return with an
                  error, without the protected call being made at all." After
                  `reset_timeout` seconds the next caller is promoted to
                  half-open.
    * half-open -- "the circuit is ready to make a real call as trial to see if
                  the problem is fixed". At most `half_open_max_calls` trials
                  are admitted; a trial "will either reset the breaker if
                  successful or restart the timeout if not".

    `success_threshold` above 1 is the one liberty taken with Fowler's
    description, which resets on a single successful trial. Requiring two
    stops a breaker from flapping closed on the one request that happened to
    land on the single healthy replica.

    Only dependency failures count -- see is_dependency_failure(). Fowler:
    "Not all errors should trip the circuit, some should reflect normal
    failures and be dealt with as part of regular logic."

    Scope matters more than the thresholds. One breaker per (dependency,
    credential) pair, shared across the worker pool if you can -- forty
    processes each holding a private breaker need forty times the failures
    before anything trips, which is a breaker that has never once fired.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        name: str = "provider",
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        success_threshold: int = 2,
        half_open_max_calls: int = 1,
    ) -> None:
        if failure_threshold < 1 or success_threshold < 1 or half_open_max_calls < 1:
            raise ValueError("thresholds must be >= 1")
        self.clock = clock
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.success_threshold = success_threshold
        self.half_open_max_calls = half_open_max_calls
        self.state = "closed"
        self.consecutive_failures = 0
        self.half_open_successes = 0
        self.half_open_inflight = 0
        self.opened_at = 0.0
        self.transitions: list[tuple[float, str]] = []
        self.short_circuited = 0

    def before_call(self) -> None:
        if self.state == "open":
            elapsed = self.clock.monotonic() - self.opened_at
            if elapsed >= self.reset_timeout:
                self._to("half_open")
                self.half_open_successes = 0
                self.half_open_inflight = 0
            else:
                self.short_circuited += 1
                raise CircuitOpen(self.name, self.reset_timeout - elapsed)
        if self.state == "half_open":
            if self.half_open_inflight >= self.half_open_max_calls:
                self.short_circuited += 1
                raise CircuitOpen(self.name, self.reset_timeout)
            self.half_open_inflight += 1

    def record_success(self) -> None:
        if self.state == "half_open":
            self.half_open_inflight = max(0, self.half_open_inflight - 1)
            self.half_open_successes += 1
            if self.half_open_successes >= self.success_threshold:
                self._to("closed")
                self.consecutive_failures = 0
                self.half_open_successes = 0
            return
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        if self.state == "half_open":
            self.half_open_inflight = max(0, self.half_open_inflight - 1)
            self._trip()
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self.opened_at = self.clock.monotonic()
        self.half_open_successes = 0
        self._to("open")

    def _to(self, state: str) -> None:
        if state != self.state:
            self.transitions.append((self.clock.monotonic(), state))
        self.state = state


def is_dependency_failure(response: Response | None, error: Exception | None) -> bool:
    """Does this outcome say the dependency is sick, or that the caller is wrong?

    Only the first kind should trip a breaker or trigger a retry. A 401 is a
    credential problem, a 403 is a permission problem, a 400 or 422 is the
    agent's own bad request; all three are permanent until something changes,
    and all three will happily consume an entire budget if you retry them.
    """
    if error is not None:
        return isinstance(error, TransportError)
    if response is None:
        return False
    return response.status in RETRYABLE_STATUSES


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------


class KillSwitch:
    """A stop flag read from outside the deploy artifact, cached for `ttl`.

    The demo backs it with a JSON file so you can toggle it mid-run from another
    terminal. In production this is a row, a feature-flag key, or a control
    plane endpoint. What it must not be is a constant in the code or an
    environment variable baked into the image, because both of those turn "stop
    this agent" into "ship a release", and a release is a build, a rollout, and
    a worker restart that re-queues exactly the work you were trying to stop.

    Granularity is the whole design. `{"global": true}` stops everything;
    `{"agents": ["invoice-bot"]}` stops one agent for everyone;
    `{"pairs": [["invoice-bot", "user_42"]]}` stops one agent for one user and
    leaves the other 900 running.
    """

    def __init__(
        self,
        path: Path | str,
        clock: Clock,
        *,
        ttl: float = 5.0,
        max_staleness: float = 60.0,
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.ttl = ttl
        self.max_staleness = max_staleness
        self._cached: dict[str, Any] = {}
        self._cached_at: float | None = None
        self.reads = 0

    def _load(self) -> dict[str, Any]:
        now = self.clock.monotonic()
        if self._cached_at is not None and now - self._cached_at < self.ttl:
            return self._cached
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            if not isinstance(raw, dict):
                raise ValueError("kill switch document must be an object")
            self._cached = raw
            self._cached_at = now
            self.reads += 1
        except (OSError, ValueError):
            # Serve the last known good answer, but not forever. A control plane
            # that has been unreachable longer than `max_staleness` is a control
            # plane that cannot stop you, and an agent nobody can stop should
            # not be running. Failing closed here costs availability; failing
            # open costs you the incident this guide is about.
            if self._cached_at is None or now - self._cached_at > self.max_staleness:
                raise Killed("kill switch unreadable and last read is stale; stopping") from None
        return self._cached

    def check(self, agent_id: str, user_id: str) -> None:
        state = self._load()
        if state.get("global"):
            raise Killed("kill switch: global stop")
        if agent_id in set(state.get("agents", [])):
            raise Killed(f"kill switch: agent {agent_id} stopped")
        pairs = {tuple(pair) for pair in state.get("pairs", []) if len(pair) == 2}
        if (agent_id, user_id) in pairs:
            raise Killed(f"kill switch: agent {agent_id} stopped for user {user_id}")


# --------------------------------------------------------------------------
# The governed client
# --------------------------------------------------------------------------


@dataclass
class CallCost:
    """What one attempt costs. Substitute your provider's published prices.

    `usd` is the up-front reservation: the worst case, because you have to
    reserve before you know. `refused_usd` is what the attempt actually cost
    when the provider rejected it without doing the work -- a 429 or a 503 is
    a request you paid for and a result you did not get. Reconciling the
    difference back is what keeps a throttled hour from eating a user's whole
    daily cap.

    In production neither number is a constant. Take the actual from the
    provider's own usage response or its billing export, not from a table in
    your code that goes stale the next time a price changes.
    """

    tokens: int = 900
    usd: Decimal = Decimal("0.0090")
    refused_usd: Decimal = Decimal("0.0005")


# Statuses where the provider answered without doing the work you asked for.
REFUSED_STATUSES = frozenset({403, 408, 429, 503})


class GovernedClient:
    """Kill switch, then budget, then breaker, then the call. In that order.

    The order is not arbitrary. The kill switch is free and must win over
    everything. The budget is local and must be checked before anything is
    spent. The breaker decides whether the call is worth making at all. Only
    then does anyone touch the network.

    That order puts the daily reservation between two things that can refuse:
    the reservation is taken, and then the breaker may short-circuit without
    the provider ever hearing about it. So every attempt runs inside a
    try/finally that either reconciles the reservation against what the call
    actually cost or gives it back untouched. Getting this wrong is silent:
    the money is never spent, the cap fills up anyway, and the daily limit
    fires in the middle of the outage that caused it.
    """

    def __init__(
        self,
        provider: Any,
        ledger: RunLedger,
        breaker: CircuitBreaker,
        kill_switch: KillSwitch,
        clock: Clock,
        *,
        agent_id: str,
        user_id: str,
        rng: random.Random,
        daily: DailySpendLedger | None = None,
        day: str = "1970-01-01",
        max_attempts: int = 4,
        base_delay: float = 0.5,
        cap_delay: float = 30.0,
        request_timeout: float = 10.0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.provider = provider
        self.ledger = ledger
        self.breaker = breaker
        self.kill_switch = kill_switch
        self.clock = clock
        self.agent_id = agent_id
        self.user_id = user_id
        self.rng = rng
        self.daily = daily
        self.day = day
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.cap_delay = cap_delay
        self.request_timeout = request_timeout
        self.on_event = on_event or (lambda event: None)

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        cost: CallCost | None = None,
        **provider_kwargs: Any,
    ) -> Response:
        cost = cost or CallCost()
        if method.upper() != "GET" and idempotency_key is None:
            # Retrying a write with no key is how you get two of something. The
            # client refuses rather than quietly making the unsafe choice.
            raise ValueError(f"{method} {path} is a write and needs an idempotency_key")

        last_response: Response | None = None
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            self.kill_switch.check(self.agent_id, self.user_id)
            self.ledger.check_before_call(tokens=cost.tokens, cost_usd=cost.usd)
            if self.daily is not None:
                self.daily.reserve(self.user_id, self.day, cost.usd)

            response = None
            error = None
            settled = False
            try:
                # Every exit from here down either settles the reservation or
                # unwinds through the `finally` that gives it back. The breaker
                # is the path that matters: it raises CircuitOpen *after* the
                # money is reserved and without spending any of it.
                self.breaker.before_call()

                # No single request may outlive the run. A 30-second socket
                # timeout inside a run with 4 seconds left is a run that
                # overshoots its own deadline by 26 seconds, once per attempt.
                timeout = min(self.request_timeout, max(0.0, self.ledger.seconds_left))
                try:
                    response = self.provider.request(
                        method,
                        path,
                        params=params,
                        idempotency_key=idempotency_key,
                        timeout=timeout,
                        **provider_kwargs,
                    )
                except TransportError as exc:
                    error = exc

                # The attempt happened, so the reservation becomes real spend --
                # at what it actually cost, not at the worst case reserved.
                actual_usd = cost.usd
                if response is not None and response.status in REFUSED_STATUSES:
                    actual_usd = cost.refused_usd
                self.ledger.spend(tokens=cost.tokens, cost_usd=actual_usd)
                if self.daily is not None:
                    self.daily.reconcile(self.user_id, self.day, cost.usd, actual_usd)
                settled = True
            finally:
                if not settled and self.daily is not None:
                    self.daily.release(self.user_id, self.day, cost.usd)

            unhealthy = is_dependency_failure(response, error)
            if unhealthy:
                self.breaker.record_failure()
            elif error is None:
                self.breaker.record_success()

            self.on_event(
                {
                    "run_id": self.ledger.run_id,
                    "agent_id": self.agent_id,
                    "user_id": self.user_id,
                    "endpoint": f"{method.upper()} {path}",
                    "attempt": attempt,
                    "status": response.status if response is not None else None,
                    "replayed": response.headers.get("Idempotent-Replayed") == "true"
                    if response is not None
                    else False,
                    "error": type(error).__name__ if error else None,
                    "breaker": self.breaker.state,
                    "calls_so_far": self.ledger.provider_calls,
                }
            )

            if error is None and response is not None and not unhealthy:
                return response

            last_response, last_error = response, error
            if not unhealthy:
                # A permanent error. Hand it back untouched; the caller's job is
                # to fix the request, not to ask again more slowly.
                return response  # type: ignore[return-value]
            if attempt == self.max_attempts - 1:
                break

            delay = next_delay(
                attempt, response, base=self.base_delay, cap=self.cap_delay, rng=self.rng
            )
            self.ledger.check_sleep(delay)
            self.clock.sleep(delay)

        status = last_response.status if last_response is not None else None
        raise ToolFailed(
            f"{method.upper()} {path} failed after {self.max_attempts} attempts "
            f"(last status {status}, last error {type(last_error).__name__ if last_error else None})",
            attempts=self.max_attempts,
            last_status=status,
        )


class _AlwaysDown:
    """A provider that would return 503 -- if anything ever reached it."""

    def __init__(self) -> None:
        self.calls = 0

    def request(self, method: str, path: str, **kwargs: Any) -> Response:
        self.calls += 1
        return Response(503, {"Retry-After": "1"}, {"error": {"type": "api_error"}})


class _AlwaysThrottled:
    """A provider that answers every request with a 429."""

    def __init__(self) -> None:
        self.calls = 0

    def request(self, method: str, path: str, **kwargs: Any) -> Response:
        self.calls += 1
        return Response(429, {"Retry-After": "1"}, {"error": {"type": "rate_limit_error"}})


def _self_check() -> None:
    rng = random.Random(7)

    # Full jitter never exceeds its ceiling and never goes negative.
    for attempt in range(0, 12):
        ceiling = min(30.0, 0.5 * 2 ** attempt)
        for _ in range(200):
            delay = full_jitter_delay(attempt, base=0.5, cap=30.0, rng=rng)
            assert 0.0 <= delay <= ceiling, (attempt, delay, ceiling)
    # And it does not overflow when someone passes an absurd attempt number.
    assert 0.0 <= full_jitter_delay(5000, base=0.5, cap=30.0, rng=rng) <= 30.0

    # Retry-After, both RFC 9110 forms.
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("٣") is None, "non-ASCII digits are not 1*DIGIT"
    reference = datetime(1999, 12, 31, 23, 57, 59, tzinfo=timezone.utc)
    assert parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT", now=reference) == 120.0
    past = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT", now=past) == 0.0

    # Retry-After is a floor: never sleep less than the server asked for.
    throttled = Response(429, {"Retry-After": "30"}, {})
    for _ in range(500):
        assert next_delay(0, throttled, base=0.5, cap=30.0, rng=rng) >= 30.0

    # Breaker state machine.
    clock = VirtualClock()
    breaker = CircuitBreaker(clock, failure_threshold=3, reset_timeout=10.0, success_threshold=2)
    assert breaker.state == "closed"
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed", "two failures is below the threshold"
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed", "a success resets the consecutive counter"
    breaker.record_failure()
    assert breaker.state == "open"
    try:
        breaker.before_call()
        raise AssertionError("open breaker must short-circuit")
    except CircuitOpen:
        pass
    clock.sleep(10.0)
    breaker.before_call()
    assert breaker.state == "half_open"
    breaker.record_failure()
    assert breaker.state == "open", "one failed trial reopens and restarts the timeout"
    clock.sleep(10.0)
    breaker.before_call()
    breaker.record_success()
    assert breaker.state == "half_open", "one success is below success_threshold=2"
    breaker.before_call()
    breaker.record_success()
    assert breaker.state == "closed"

    # Only dependency failures count.
    assert is_dependency_failure(Response(503, {}, {}), None)
    assert is_dependency_failure(Response(429, {}, {}), None)
    assert is_dependency_failure(None, TransportError("reset"))
    assert not is_dependency_failure(Response(400, {}, {}), None)
    assert not is_dependency_failure(Response(401, {}, {}), None)
    assert not is_dependency_failure(Response(200, {}, {}), None)

    # Daily spend cap.
    daily = DailySpendLedger(cap_usd=Decimal("1.00"))
    daily.reserve("user_42", "2026-08-18", Decimal("0.60"))
    daily.reconcile("user_42", "2026-08-18", Decimal("0.60"), Decimal("0.40"))
    assert daily.total("user_42", "2026-08-18") == Decimal("0.40")
    daily.reserve("user_42", "2026-08-18", Decimal("0.50"))
    try:
        daily.reserve("user_42", "2026-08-18", Decimal("0.20"))
        raise AssertionError("cap must hold")
    except BudgetExhausted as exc:
        assert exc.limit_name == "daily_spend_usd"

    # Releasing more than was reserved is a double-release bug, not a clamp.
    fresh = DailySpendLedger(cap_usd=Decimal("1.00"))
    fresh.reserve("user_42", "2026-08-18", Decimal("0.10"))
    fresh.release("user_42", "2026-08-18", Decimal("0.10"))
    assert fresh.total("user_42", "2026-08-18") == Decimal("0")
    try:
        fresh.release("user_42", "2026-08-18", Decimal("0.01"))
        raise AssertionError("over-release must raise")
    except ValueError:
        pass

    # A short-circuit storm must not consume a single cent of a daily cap. The
    # breaker refuses after the reservation is taken, which is the path that
    # leaks if the release is missing.
    clock = VirtualClock()
    provider = _AlwaysDown()
    budget = RunBudget(
        max_provider_calls=1000,
        max_tokens=10_000_000,
        max_cost_usd=Decimal("100.00"),
        max_wall_clock_seconds=10_000.0,
    )
    ledger = RunLedger(budget, clock, run_id="run_leak")
    breaker = CircuitBreaker(clock, failure_threshold=1, reset_timeout=10_000.0)
    breaker.record_failure()
    assert breaker.state == "open"
    daily = DailySpendLedger(cap_usd=Decimal("100.00"))
    client = GovernedClient(
        provider,
        ledger,
        breaker,
        KillSwitch(Path("nonexistent-kill-switch.json"), clock),
        clock,
        agent_id="leak-check",
        user_id="user_42",
        rng=random.Random(3),
        daily=daily,
        day="2026-08-18",
    )
    short_circuited = 0
    for _ in range(20):
        try:
            client.call("GET", "/items", params={})
        except CircuitOpen:
            short_circuited += 1
    assert short_circuited == 20, short_circuited
    assert provider.calls == 0, provider.calls
    assert ledger.provider_calls == 0, ledger.provider_calls
    assert daily.total("user_42", "2026-08-18") == Decimal("0"), daily.total(
        "user_42", "2026-08-18"
    )

    # A refused call reconciles down to what it actually cost, not the reserve.
    breaker_ok = CircuitBreaker(clock, failure_threshold=99, reset_timeout=1.0)
    daily2 = DailySpendLedger(cap_usd=Decimal("100.00"))
    ledger2 = RunLedger(budget, clock, run_id="run_refused")
    refusing = GovernedClient(
        _AlwaysThrottled(),
        ledger2,
        breaker_ok,
        KillSwitch(Path("nonexistent-kill-switch.json"), clock),
        clock,
        agent_id="refused-check",
        user_id="user_42",
        rng=random.Random(3),
        daily=daily2,
        day="2026-08-18",
        max_attempts=1,
    )
    try:
        refusing.call("GET", "/items", params={})
        raise AssertionError("a 429 on the last attempt must raise ToolFailed")
    except ToolFailed:
        pass
    assert daily2.total("user_42", "2026-08-18") == CallCost().refused_usd

    # A kill switch that cannot be read stops the run once its cache is stale.
    stale_clock = VirtualClock()
    never_read = KillSwitch(Path(__file__), stale_clock, max_staleness=60.0)
    try:
        never_read.check("any-agent", "any-user")  # a .py file is not a JSON object
        raise AssertionError("an unreadable kill switch with no cache must stop the run")
    except Killed:
        pass

    with tempfile.TemporaryDirectory() as tmp:
        flag = Path(tmp) / "kill-switch.json"
        flag.write_text("{}", encoding="utf-8")
        switch = KillSwitch(flag, stale_clock, ttl=1.0, max_staleness=60.0)
        switch.check("any-agent", "any-user")  # one good read, now cached

        flag.write_text("not json at all", encoding="utf-8")
        stale_clock.sleep(30.0)
        switch.check("any-agent", "any-user")  # broken store, cache still fresh enough

        stale_clock.sleep(31.0)  # now past max_staleness
        try:
            switch.check("any-agent", "any-user")
            raise AssertionError("a stale, unreadable kill switch must stop the run")
        except Killed:
            pass

    print("guardrail self-checks passed")


if __name__ == "__main__":
    _self_check()
