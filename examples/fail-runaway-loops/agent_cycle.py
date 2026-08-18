"""Two loops that no rate limiter will save you from: mutual calls, and echoes.

A retry storm at least announces itself with 429s. These two produce perfectly
valid traffic at a perfectly reasonable rate, forever.

    python3 agent_cycle.py

Both fixes are the same idea in different clothes: carry the causal history with
the work, and refuse work whose history says you have already handled it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any


class CycleDetected(Exception):
    pass


class DepthExceeded(Exception):
    pass


# --------------------------------------------------------------------------
# Shape 1: two agents that each think the other is the expert.
# --------------------------------------------------------------------------


@dataclass
class CallContext:
    """Travels with every agent-to-agent call. The whole fix lives in here.

    `path` is the chain of agent ids that led to this call, so any repeat --
    including a three-agent cycle that comparing against the immediate caller
    would miss -- is caught. `max_depth` covers what the path check cannot: a
    chain of *distinct* agents that never repeats and never ends. See
    distinct_chain(). `root_run_id` ties the whole tree back to one user action
    in your audit trail.
    """

    root_run_id: str
    path: tuple[str, ...] = ()
    max_depth: int = 8

    def enter(self, agent_id: str) -> CallContext:
        if agent_id in self.path:
            raise CycleDetected(f"{' -> '.join(self.path)} -> {agent_id}")
        if len(self.path) + 1 > self.max_depth:
            raise DepthExceeded(f"depth {len(self.path) + 1} exceeds {self.max_depth}")
        return CallContext(self.root_run_id, self.path + (agent_id,), self.max_depth)


@dataclass
class Registry:
    agents: dict[str, Any] = field(default_factory=dict)
    calls: int = 0

    def invoke(self, agent_id: str, question: str, ctx: CallContext | None) -> str:
        self.calls += 1
        if ctx is not None:
            ctx = ctx.enter(agent_id)
        return self.agents[agent_id](self, question, ctx)


def make_deferrer(peer_id: str):
    """An agent that answers every question by asking the other one."""

    def agent(registry: Registry, question: str, ctx: CallContext | None) -> str:
        return registry.invoke(peer_id, question, ctx)

    return agent


def make_chain(next_id: str | None):
    """An agent that hands the question one step further down a chain."""

    def agent(registry: Registry, question: str, ctx: CallContext | None) -> str:
        if next_id is None:
            return "answered"
        return registry.invoke(next_id, question, ctx)

    return agent


def mutual_recursion(*, guarded: bool) -> tuple[int, str]:
    registry = Registry()
    registry.agents["billing"] = make_deferrer("support")
    registry.agents["support"] = make_deferrer("billing")
    ctx = CallContext("run_a1b2") if guarded else None
    try:
        registry.invoke("billing", "who owns this refund?", ctx)
    except CycleDetected as exc:
        return registry.calls, f"CycleDetected: {exc}"
    except DepthExceeded as exc:
        return registry.calls, f"DepthExceeded: {exc}"
    except RecursionError:
        # Unguarded, the only thing that stops this is the interpreter's own
        # stack limit -- and only because the calls are synchronous. Across a
        # queue or an HTTP hop there is no stack, so there is no limit.
        return registry.calls, "RecursionError: stopped by the interpreter, not by you"
    return registry.calls, "returned"


def distinct_chain(length: int, *, max_depth: int = 8) -> tuple[int, str]:
    """A chain of agents that never repeats, so the path check never fires.

    This is the case the depth ceiling exists for. Every id on the path is
    unique, so `agent_id in self.path` is false at every hop; only `max_depth`
    stops it. A guard you claim catches something should be shown catching it.
    """
    registry = Registry()
    ids = [f"agent_{i:02d}" for i in range(length)]
    for index, agent_id in enumerate(ids):
        nxt = ids[index + 1] if index + 1 < length else None
        registry.agents[agent_id] = make_chain(nxt)
    try:
        registry.invoke(ids[0], "escalate this", CallContext("run_c3d4", max_depth=max_depth))
    except DepthExceeded as exc:
        return registry.calls, f"DepthExceeded: {exc}"
    except CycleDetected as exc:  # pragma: no cover - ids are distinct by construction
        return registry.calls, f"CycleDetected: {exc}"
    return registry.calls, "returned"


# --------------------------------------------------------------------------
# Shape 2: the agent's own write re-triggers the agent.
# --------------------------------------------------------------------------


@dataclass
class Ticket:
    ticket_id: str
    comments: list[dict[str, Any]] = field(default_factory=list)


class TicketSystem:
    """A ticket store that emits an event for every comment, as they all do."""

    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}
        self.events: list[dict[str, Any]] = []
        self.writes = 0

    def comment(self, ticket_id: str, author: str, body: str, *, actor_tag: str | None) -> None:
        self.writes += 1
        ticket = self.tickets.setdefault(ticket_id, Ticket(ticket_id))
        comment = {"author": author, "body": body, "actor_tag": actor_tag}
        ticket.comments.append(comment)
        self.events.append({"type": "comment.created", "ticket_id": ticket_id, **comment})


def echo_loop(*, tag_own_writes: bool, hard_cap: int = 500) -> tuple[int, str]:
    """The agent comments; the comment fires an event; the event wakes the agent.

    `tag_own_writes` marks the agent's own writes with its actor id and makes
    the handler ignore events it caused. Filtering on the author's display name
    is the version that breaks the first time somebody renames the bot.
    """
    system = TicketSystem()
    agent_tag = "agent:triage-bot@v3"
    system.comment("t_1", "customer", "my card was declined", actor_tag=None)

    processed = 0
    cursor = 0
    while cursor < len(system.events):
        event = system.events[cursor]
        cursor += 1
        if tag_own_writes and event.get("actor_tag") == agent_tag:
            continue
        processed += 1
        if system.writes >= hard_cap:
            return system.writes, f"hit the hard cap of {hard_cap} writes; still looping"
        system.comment(
            event["ticket_id"],
            "triage-bot",
            "Acknowledged, looking into it.",
            actor_tag=agent_tag if tag_own_writes else None,
        )
    return system.writes, f"drained after {processed} events processed"


def main() -> None:
    calls, why = mutual_recursion(guarded=False)
    print(f"two agents, no call context:   {calls} invocations, {why}")
    print(f"  (that count is sys.getrecursionlimit() showing through: {sys.getrecursionlimit()})")

    calls, why = mutual_recursion(guarded=True)
    print(f"two agents, call path carried: {calls} invocations, {why}")

    calls, why = distinct_chain(20, max_depth=8)
    print(f"20 distinct agents in a chain: {calls} invocations, {why}")
    print()

    writes, why = echo_loop(tag_own_writes=False)
    print(f"webhook echo, untagged writes: {writes} writes, {why}")

    writes, why = echo_loop(tag_own_writes=True)
    print(f"webhook echo, tagged writes:   {writes} writes, {why}")


if __name__ == "__main__":
    main()
