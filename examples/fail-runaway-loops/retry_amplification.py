"""Count the calls a layered retry stack actually produces. No network.

Three demonstrations, all by counting rather than by arithmetic on a whiteboard:

1. A stack where every layer retries, versus the same stack where only the layer
   immediately above the failing dependency retries.
2. The same stack one layer deeper, because the growth is multiplicative.
3. Google's per-client retry budget: a cap on the ratio of retries to requests,
   which turns a 3x amplification into roughly 1.1x.

Sources for the shape of (1) and (3): Google's SRE book, "Handling Overload",
https://sre.google/sre-book/handling-overload/ -- "a per-request retry budget of
up to three attempts", "A request will only be retried as long as this ratio is
below 10%", and "If multiple layers retried, we'd have a combinatorial
explosion."

    python3 retry_amplification.py
"""

from __future__ import annotations

from typing import Callable


class Unavailable(Exception):
    """The dependency is down. Every attempt below returns this."""


class Dependency:
    """The bottom of the stack. It counts every request that reaches it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        raise Unavailable("dependency is down")


def retrying_layer(inner: Callable[[], None], attempts: int) -> Callable[[], None]:
    """A layer that makes up to `attempts` total tries before giving up.

    `attempts=1` means "call once, do not retry" -- a passthrough layer.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    def call() -> None:
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return inner()
            except Unavailable as exc:
                last = exc
        raise last if last is not None else Unavailable("unreachable")

    return call


def stack_calls(layer_attempts: list[int]) -> int:
    """Build a stack, issue one request at the top, return calls at the bottom.

    `layer_attempts` is listed outermost first, so [3, 3, 3] is three layers that
    each retry twice after the first try.
    """
    dependency = Dependency()
    call: Callable[[], None] = dependency
    for attempts in reversed(layer_attempts):
        call = retrying_layer(call, attempts)
    try:
        call()
    except Unavailable:
        pass
    return dependency.calls


class PerClientRetryBudget:
    """Retry only while retries are under `ratio` of everything this client sent.

    Google's SRE book: "Each client keeps track of the ratio of requests that
    correspond to retries. A request will only be retried as long as this ratio
    is below 10%."
    """

    def __init__(self, ratio: float = 0.10) -> None:
        self.ratio = ratio
        self.sent = 0
        self.retries = 0

    def record_send(self, *, is_retry: bool) -> None:
        self.sent += 1
        if is_retry:
            self.retries += 1

    def allows_retry(self) -> bool:
        if self.sent == 0:
            return True
        return (self.retries / self.sent) < self.ratio


def budgeted_traffic(
    initial_requests: int, *, per_request_attempts: int = 3, ratio: float | None = 0.10
) -> int:
    """Total attempts a client sends for `initial_requests` requests that all fail."""
    budget = PerClientRetryBudget(ratio if ratio is not None else 1.0)
    dependency = Dependency()
    for _ in range(initial_requests):
        for attempt in range(per_request_attempts):
            is_retry = attempt > 0
            if is_retry and ratio is not None and not budget.allows_retry():
                break
            budget.record_send(is_retry=is_retry)
            try:
                dependency()
            except Unavailable:
                continue
    return dependency.calls


def main() -> None:
    every_layer = stack_calls([3, 3, 3])
    one_layer = stack_calls([1, 1, 3])
    four_deep = stack_calls([3, 3, 3, 3])

    print("one request into a three-layer stack, dependency down")
    print(f"  every layer retries 3x        -> {every_layer} calls at the dependency")
    print(f"  only the layer above retries  -> {one_layer} calls at the dependency")
    print(f"  four layers, all retrying 3x  -> {four_deep} calls at the dependency")
    print()

    n = 1000
    unbudgeted = budgeted_traffic(n, ratio=None)
    budgeted = budgeted_traffic(n, ratio=0.10)
    print(f"{n} requests from one client, dependency down, 3 attempts per request")
    print(f"  no per-client budget -> {unbudgeted} attempts ({unbudgeted / n:.2f}x)")
    print(f"  10% retry budget     -> {budgeted} attempts ({budgeted / n:.2f}x)")


if __name__ == "__main__":
    main()
