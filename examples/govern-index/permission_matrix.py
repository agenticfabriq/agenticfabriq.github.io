"""The permission matrix for one agent, evaluated per call.

    python3 permission_matrix.py

No credentials and no network. Everything here is the decision layer: the four
axes (agent, user, resource, action), a policy evaluated at the moment of the
call rather than at the moment of the grant, a revocation with three-part
granularity, and one audit record per attempt including the attempts that were
refused.

What this file deliberately does NOT model: the user's own per-object
entitlements. Those belong to the resource and are checked there -- see
`Resource.fetch` below -- because a copy of the directory inside the agent is
stale in whichever direction is least convenient. The local decision is a
fail-closed pre-check that stops a request from leaving the process. It is
never the boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

AGENT = "agent:oncall-assistant@v3"
NOW = datetime(2026, 8, 18, 9, 14, 22, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The four axes.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One (agent, subject, resource, action) cell, plus the task it belongs to."""

    agent: str
    subject: str | None  # the human whose authority is borrowed; None = unattended
    resource: str
    action: str
    correlation_id: str
    arguments: dict[str, object] = field(default_factory=dict)


# What this agent may EVER do, whoever asks. Registered against the agent's own
# identity, reviewed as a unit, and the ceiling on every cell below.
AGENT_GRANT: frozenset[tuple[str, str]] = frozenset(
    {
        ("slack:channel", "read_history"),
        ("slack:channel", "post_message"),
        ("graph:drive", "read_file"),
        ("github:issues", "create"),
    }
)

# Resources whose contents belong to a person. A call against one of these
# without a subject is unattributable by construction, so it is refused before
# it is evaluated rather than run as the agent.
USER_SCOPED: frozenset[str] = frozenset({"graph:drive"})

# One row per (agent, subject): what this human consented to let this agent do
# for them, when, and until when. This is the artifact OAuth does not keep for
# you -- the granted `scope` of a token response, preserved as a record.
DELEGATIONS: dict[tuple[str, str], dict[str, object]] = {
    (AGENT, "oid:ravi"): {
        "grant_id": "grant:01JQ8Z9RMK",
        "granted": frozenset({("graph:drive", "read_file")}),
        "expires_at": NOW + timedelta(hours=2),
    },
    (AGENT, "oid:priya"): {
        "grant_id": "grant:01JQ8ZB4TT",
        "granted": frozenset({("graph:drive", "read_file")}),
        "expires_at": NOW + timedelta(hours=2),
    },
}

# The revocation table, keyed on all three parts of the question you have to be
# able to answer during an incident: which agent, for which user, against which
# resource. A `None` in a position means "any".
REVOKED: set[tuple[str, str | None, str | None]] = set()

POLICY_VERSION = "oncall-assistant/2026-08-18"


def revoke(agent: str, subject: str | None = None, resource: str | None = None) -> None:
    """Stop one agent, for one user, against one resource, from the next call on."""
    REVOKED.add((agent, subject, resource))


def _revocation_matching(call: Call) -> tuple[str, str | None, str | None] | None:
    for entry in REVOKED:
        agent, subject, resource = entry
        if agent != call.agent:
            continue
        if subject is not None and subject != call.subject:
            continue
        if resource is not None and resource != _resource_class(call.resource):
            continue
        return entry
    return None


def _resource_class(resource: str) -> str:
    """`slack:channel/C-INC-402` -> `slack:channel`. Grants name classes, not instances."""
    return resource.split("/", 1)[0]


# --------------------------------------------------------------------------
# The decision, made at call time.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str

    @property
    def verdict(self) -> str:
        return "allow" if self.allow else "deny"


def decide(call: Call) -> Decision:
    cell = (_resource_class(call.resource), call.action)

    # 1. The agent axis. An action outside the agent's own registered grant is
    #    refused no matter which human asked, which is the half of the
    #    intersection that no prompt can talk its way past.
    if cell not in AGENT_GRANT:
        return Decision(False, f"agent_grant: {AGENT} holds no {cell[1]} on {cell[0]}")

    # 2. The user axis. Data that belongs to a person needs a person in the
    #    request; running it as the agent is the confused-deputy shape.
    if cell[0] in USER_SCOPED:
        if call.subject is None:
            return Decision(False, f"subject_required: {cell[0]} is user-scoped and no subject was supplied")

        delegation = DELEGATIONS.get((call.agent, call.subject))
        if delegation is None:
            return Decision(False, f"no_delegation: {call.subject} has not granted {call.agent}")
        if NOW >= delegation["expires_at"]:
            return Decision(False, f"delegation_expired: {delegation['grant_id']} expired")

        # 3. The intersection, never the union. Missing from either side denies.
        if cell not in delegation["granted"]:
            return Decision(False, f"not_delegated: {delegation['grant_id']} does not cover {cell[1]}")

    # 4. Revocation, consulted per call rather than per grant, so it lands on
    #    the next request instead of at the next token expiry.
    match = _revocation_matching(call)
    if match is not None:
        return Decision(False, f"revoked: {match}")

    if cell[0] in USER_SCOPED:
        grant_id = DELEGATIONS[(call.agent, call.subject)]["grant_id"]
        return Decision(True, f"intersection of agent_grant and {grant_id} covers {cell[0]}:{cell[1]}")
    return Decision(True, f"agent_grant covers {cell[0]}:{cell[1]}, no user data reached")


# --------------------------------------------------------------------------
# The resource, which enforces the user's own entitlements.
# --------------------------------------------------------------------------


class Resource:
    """Stands in for Graph, Slack, or GitHub applying its own access control.

    The point of the class is the last two lines of `fetch`: a call the agent
    allowed can still be refused here, because whether Priya may open this
    particular file is a fact the resource holds and the agent does not.
    """

    def __init__(self, readable_by: dict[str, frozenset[str]]) -> None:
        self._readable_by = readable_by

    def fetch(self, call: Call) -> tuple[str, str]:
        allowed = self._readable_by.get(call.resource)
        if allowed is None or call.subject is None or call.subject in allowed:
            return "success", f"{call.action} {call.resource}"
        return "error", "403 from the resource: the caller is not entitled to this object"


RESOURCES = Resource(
    readable_by={
        # Ravi owns the runbook. Priya has never been given it. Nothing in the
        # agent knows that, and nothing in the agent needs to.
        "graph:drive/runbook-payments.docx": frozenset({"oid:ravi"}),
    }
)


# --------------------------------------------------------------------------
# One record per attempt, both principals, decision and outcome separately.
# --------------------------------------------------------------------------


def canonical_digest(arguments: dict[str, object]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execute(call: Call) -> dict[str, object]:
    decision = decide(call)

    if decision.allow:
        outcome, detail = RESOURCES.fetch(call)
    else:
        outcome, detail = "blocked", None

    delegation = DELEGATIONS.get((call.agent, call.subject)) if call.subject else None

    return {
        "occurred_at": NOW.isoformat(),
        "agent_id": call.agent,
        "principal_id": call.subject,
        "principal_type": "human" if call.subject else "service",
        "grant_id": delegation["grant_id"] if delegation else None,
        "resource": call.resource,
        "action": call.action,
        "arguments_sha256": canonical_digest(call.arguments),
        "decision": decision.verdict,
        "decision_reason": decision.reason,
        "policy_version": POLICY_VERSION,
        "outcome": outcome,
        "outcome_detail": detail,
        "correlation_id": call.correlation_id,
    }


# --------------------------------------------------------------------------
# The same four requests, with no agent identity and no subject.
# --------------------------------------------------------------------------


def without_identity(call: Call) -> str:
    """What a shared API key can decide.

    A bearer credential carries one fact: somebody presented it. There is no
    agent axis because every holder is the same principal, and no user axis
    because the credential does not know a request was made for anyone. Every
    cell in the matrix collapses to this one answer.
    """
    return "allow: the caller possesses the key"


# --------------------------------------------------------------------------


TASK = "4bf92f3577b34da6a3ce929d0e0e4736"

REQUESTS = [
    Call(AGENT, "oid:ravi", "slack:channel/C-INC-402", "read_history", TASK),
    Call(AGENT, "oid:ravi", "graph:drive/runbook-payments.docx", "read_file", TASK, {"path": "/runbooks"}),
    Call(AGENT, "oid:priya", "graph:drive/runbook-payments.docx", "read_file", TASK, {"path": "/runbooks"}),
    Call(AGENT, "oid:ravi", "github:issues/acme-payments-api", "create", TASK, {"title": "INC-402"}),
    Call(AGENT, None, "slack:channel/C-INC-402", "post_message", TASK, {"text": "summary"}),
    Call(AGENT, "oid:ravi", "graph:drive/runbook-payments.docx", "delete_file", TASK),
    Call(AGENT, None, "graph:drive/runbook-payments.docx", "read_file", TASK),
]


def show(label: str, records: list[dict[str, object]]) -> None:
    print(f"\n== {label} ==")
    for record in records:
        principal = record["principal_id"] or "-"
        print(
            f"  {record['decision']:<5} {record['outcome']:<8} "
            f"{principal:<10} {record['resource']:<38} {record['action']:<13} "
            f"{record['decision_reason']}"
        )


def main() -> None:
    show("one agent, seven cells, decided per call", [execute(call) for call in REQUESTS])

    verdicts = {without_identity(call) for call in REQUESTS}
    print(f"\n== the same {len(REQUESTS)} requests, decided by a shared API key ==")
    for verdict in verdicts:
        print(f"  {verdict}  (x{len(REQUESTS)})")

    # The three-part revocation test: one agent, one user, one resource, now.
    revoke(AGENT, subject="oid:ravi", resource="graph:drive")
    after = [
        execute(REQUESTS[1]),  # Ravi's runbook read -- the cell that was revoked
        execute(REQUESTS[0]),  # Ravi's Slack read -- a different resource
        execute(REQUESTS[4]),  # the agent's own post -- a different subject
    ]
    show("after revoke(agent, subject='oid:ravi', resource='graph:drive')", after)

    print("\n== the record for the denial ==")
    print(json.dumps(after[0], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
