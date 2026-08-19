"""Scope one agent incident from an audit export, or refuse to.

Run with no arguments to see two fixtures: one that produces a bounded answer,
and one that refuses because the records cannot support one.

    python3 examples/fail-index/incident_scope.py
    python3 examples/fail-index/incident_scope.py --records export.json \
        --agent agent:meeting-brief@v3 \
        --from 2026-08-17T09:00:00Z --to 2026-08-17T09:45:00Z

Nothing here touches the network. The record shape is the one in
`examples/govern-audit/schema.sql`: one row per *attempted* action, carrying
both principals, the policy decision, the outcome, and a taint flag.

What this answers: within the window you give it, what did one agent do, for
whom, to what, and which of those actions were taken while untrusted content
was in the model's context. What it cannot answer: anything that did not go
through the audited path. A credential used directly against the provider
leaves no row here, which is why the provider's own log is the second source.

The action vocabulary is yours. `WRITE_ACTIONS` and `READ_ACTIONS` below are
this file's defaults, and a verb in neither is reported as unclassified rather
than assumed to be a read - see the second demo block. Edit both sets to match
your own schema before you put a count from this file in a report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Iterable

# Fields an incident cannot be scoped without. `principal_id` is on the list,
# but see `_missing_fields`: an explicit null is a legitimate value meaning
# "unattended", while an absent key means the emitter never captured a
# principal at all. Those are different answers and must not collapse.
REQUIRED_FIELDS = (
    "record_id",
    "occurred_at",
    "recorded_at",
    "agent_id",
    "principal_id",
    "resource",
    "action",
    "decision",
    "outcome",
    "correlation_id",
    "untrusted_input",
)

# Both vocabularies are declared, and neither is the complement of the other.
# `examples/govern-audit/schema.sql` types `action` as free text with no
# controlled vocabulary, so a store with its own verbs - `post`, `patch`,
# `post_message`, `PUT` - is the expected case rather than the odd one. Edit
# these two sets to match yours before you trust a count from this file.
WRITE_ACTIONS = frozenset({"write", "create", "update", "delete", "send"})
READ_ACTIONS = frozenset({"read", "list", "search", "get"})

# Display cap only. The count printed above the list is never truncated.
REVIEW_LIST_CAP = 20


class IncidentInconclusive(Exception):
    """The records in the window cannot bound the incident.

    Raised rather than returning a partial count, because a partial count of a
    blast radius reads as a complete one and gets written into the report.
    """

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _parse_ts(value: str) -> datetime:
    """RFC 3339 with a trailing Z, which `fromisoformat` accepts from 3.11."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_ts(value: datetime) -> str:
    """RFC 3339 with microsecond precision and a literal Z, as the schema stores it."""
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _missing_fields(record: dict[str, Any]) -> list[str]:
    return [field for field in REQUIRED_FIELDS if field not in record]


def in_window(record: dict[str, Any], agent_id: str, since: datetime, until: datetime) -> bool:
    if record.get("agent_id") != agent_id:
        return False
    occurred_at = record.get("occurred_at")
    if not isinstance(occurred_at, str):
        return False
    try:
        occurred = _parse_ts(occurred_at)
    except ValueError:
        # A record whose timestamp will not parse cannot be placed in or out of
        # the window. Include it so the completeness check below sees it and
        # refuses, rather than silently dropping it from the count.
        return True
    return since <= occurred < until


def check_soundness(records: list[dict[str, Any]]) -> None:
    """Refuse on anything that would make the answer wrong rather than partial."""
    reasons: list[str] = []

    incomplete = Counter()
    for record in records:
        for field in _missing_fields(record):
            incomplete[field] += 1
    for field, count in sorted(incomplete.items()):
        reasons.append(f"{count} record(s) in the window are missing {field!r}")

    unparseable = 0
    skewed = 0
    for record in records:
        try:
            occurred = _parse_ts(record["occurred_at"])
            recorded = _parse_ts(record["recorded_at"])
        except (KeyError, ValueError, TypeError):
            unparseable += 1
            continue
        if occurred > recorded:
            skewed += 1
    if unparseable:
        reasons.append(f"{unparseable} record(s) have a timestamp that will not parse")
    if skewed:
        reasons.append(
            f"{skewed} record(s) claim an occurred_at later than the server's "
            "recorded_at, so the ordering is not sound"
        )

    if reasons:
        raise IncidentInconclusive(reasons)


def scope(
    records: Iterable[dict[str, Any]],
    *,
    agent_id: str,
    since: datetime,
    until: datetime,
) -> dict[str, Any]:
    window = [r for r in records if in_window(r, agent_id, since, until)]
    check_soundness(window)

    reads = [r for r in window if r["action"] in READ_ACTIONS]
    writes = [r for r in window if r["action"] in WRITE_ACTIONS]
    # An action in neither vocabulary is not a read. Classifying it as one
    # would drop it out of the review list below, which is the single output
    # this tool exists to produce, and it would do so silently.
    unclassified = [
        r for r in window if r["action"] not in READ_ACTIONS and r["action"] not in WRITE_ACTIONS
    ]
    denied = [r for r in window if r["decision"] == "deny"]
    tainted = [r for r in window if r["untrusted_input"]]

    def _needs_review(record: dict[str, Any]) -> bool:
        return (
            record["untrusted_input"]
            and record["decision"] == "allow"
            and record["outcome"] == "success"
        )

    # Unclassified actions join the review list rather than being excluded from
    # it: over-reporting sends a human to read a row that turns out to be a
    # read, and under-reporting sends nobody to read a row that was a write.
    tainted_writes = [r for r in writes if _needs_review(r)]
    tainted_unclassified = [r for r in unclassified if _needs_review(r)]
    principals = sorted({r["principal_id"] or "(unattended)" for r in window})

    return {
        "agent_id": agent_id,
        "since": since,
        "until": until,
        "records": len(window),
        "principals": principals,
        "correlation_ids": sorted({r["correlation_id"] for r in window}),
        "reads": len(reads),
        "read_resources": sorted({r["resource"] for r in reads}),
        "writes": len(writes),
        "unclassified": len(unclassified),
        "unclassified_actions": sorted({r["action"] for r in unclassified}),
        "denied": [(r["resource"], r.get("decision_reason", "")) for r in denied],
        "tainted": len(tainted),
        "tainted_writes": tainted_writes,
        "tainted_unclassified": tainted_unclassified,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"== {report['agent_id']}  "
        f"{report['since'].isoformat().replace('+00:00', 'Z')} .. "
        f"{report['until'].isoformat().replace('+00:00', 'Z')} ==",
        f"  records in window ......... {report['records']}",
        f"  principals ................ {', '.join(report['principals'])}",
        f"  tasks (correlation ids) ... {len(report['correlation_ids'])}",
        f"  reads ..................... {report['reads']} over "
        f"{len(report['read_resources'])} distinct resources",
        f"  writes .................... {report['writes']}",
    ]
    if report["unclassified"]:
        lines.append(
            f"  unclassified actions ...... {report['unclassified']}  "
            f"(verbs: {', '.join(report['unclassified_actions'])})"
        )
        lines.append(
            "    these are in neither WRITE_ACTIONS nor READ_ACTIONS. They are not "
            "counted as reads,\n    and the tainted ones are listed for review below. "
            "Add them to a vocabulary."
        )
    else:
        lines.append(f"  unclassified actions ...... 0")
    lines.append(f"  denied .................... {len(report['denied'])}")
    for resource, reason in report["denied"]:
        lines.append(f"    {resource}  {reason}")
    lines.append(f"  untrusted input in context. {report['tainted']} record(s)")
    review = [(r, "write") for r in report["tainted_writes"]]
    review += [(r, "unclassified") for r in report["tainted_unclassified"]]
    lines.append(
        f"  actions taken with untrusted input in context, not reads: {len(review)}"
        "   <- review by hand"
    )
    for record, kind in review[:REVIEW_LIST_CAP]:
        lines.append(
            f"    {record['occurred_at']}  {record['resource']}  {record['action']}  "
            f"({kind})  for {record['principal_id'] or '(unattended)'}"
        )
    if len(review) > REVIEW_LIST_CAP:
        # Truncate the display, never the count on the line above it.
        lines.append(f"    ... and {len(review) - REVIEW_LIST_CAP} more; export the full list")
    lines.append(
        "  bound: this covers calls that crossed the audited path. A credential "
        "used directly\n         against the provider leaves no row here, so the "
        "provider's own log is the\n         second source you still have to pull."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Fixtures. Two runs: one that answers, one that refuses.
# --------------------------------------------------------------------------

def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "record_id": "00000000",
        "occurred_at": "2026-08-17T09:02:11.000100Z",
        "recorded_at": "2026-08-17T09:02:11.140000Z",
        "agent_id": "agent:meeting-brief@v3",
        "principal_id": "user:dana@example.com",
        "principal_type": "human",
        "resource": "drive.file",
        "action": "read",
        "decision": "allow",
        "decision_reason": "grant:drive.readonly matched",
        "outcome": "success",
        "correlation_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "untrusted_input": False,
    }
    base.update(overrides)
    return base


def answerable_fixture() -> list[dict[str, Any]]:
    records = [
        _record(record_id="r000", resource="drive.file:1AGENDA", action="read"),
        # The agenda doc carried the injected instruction. Everything after it
        # ran with untrusted content in the context.
        _record(
            record_id="r001",
            occurred_at="2026-08-17T09:02:44.000100Z",
            recorded_at="2026-08-17T09:02:44.130000Z",
            resource="drive.search",
            action="read",
            untrusted_input=True,
        ),
    ]
    first_fetch = _parse_ts("2026-08-17T09:02:45.000100Z")
    for index in range(20):
        occurred = first_fetch + timedelta(seconds=index)
        records.append(
            _record(
                record_id=f"r{index + 2:03d}",
                occurred_at=_format_ts(occurred),
                recorded_at=_format_ts(occurred + timedelta(milliseconds=120)),
                resource=f"drive.file:1DOC{index:03d}",
                action="read",
                untrusted_input=True,
            )
        )
    records.append(
        _record(
            record_id="r022",
            occurred_at="2026-08-17T09:03:09.000100Z",
            recorded_at="2026-08-17T09:03:09.110000Z",
            resource="slack.channel:C0ENG",
            action="send",
            decision="allow",
            decision_reason="channel_allowlist: C0ENG is a declared destination",
            outcome="success",
            untrusted_input=True,
        )
    )
    records.append(
        _record(
            record_id="r023",
            occurred_at="2026-08-17T09:03:12.000100Z",
            recorded_at="2026-08-17T09:03:12.100000Z",
            resource="slack.channel:C0GENERAL",
            action="send",
            decision="deny",
            decision_reason="channel_allowlist: C0GENERAL is not a declared destination",
            outcome="blocked",
            untrusted_input=True,
        )
    )
    # A different agent in the same window. It must not appear in the report.
    records.append(_record(record_id="r900", agent_id="agent:support-triage@v7"))
    return records


def unclassified_fixture() -> list[dict[str, Any]]:
    """The same incident from a store whose schema uses its own verbs."""
    records = answerable_fixture()
    # Only the write verb differs, which is the case that costs something: a
    # write classified as a read leaves the review list silently short.
    renamed = {"send": "post_message"}
    return [{**r, "action": renamed.get(r["action"], r["action"])} for r in records]


def inconclusive_fixture() -> list[dict[str, Any]]:
    records = answerable_fixture()
    stripped = dict(records[3])
    del stripped["principal_id"]
    records[3] = stripped
    records[4] = {**records[4], "recorded_at": "2026-08-17T09:02:40.000000Z"}
    return records


def _demo() -> int:
    since = _parse_ts("2026-08-17T09:00:00Z")
    until = _parse_ts("2026-08-17T09:45:00Z")
    agent = "agent:meeting-brief@v3"

    print(render(scope(answerable_fixture(), agent_id=agent, since=since, until=until)))
    print()
    print("== the same window, from a store that uses its own action verbs ==")
    print(render(scope(unclassified_fixture(), agent_id=agent, since=since, until=until)))
    print()
    print("== the same window, from a log that cannot support an answer ==")
    try:
        scope(inconclusive_fixture(), agent_id=agent, since=since, until=until)
    except IncidentInconclusive as inconclusive:
        for reason in inconclusive.reasons:
            print(f"  refusing to scope: {reason}")
    else:  # pragma: no cover - the fixture is built to fail
        print("  BUG: the inconclusive fixture produced a report")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", help="path to a JSON array of audit records")
    parser.add_argument("--agent", help="agent_id to scope")
    parser.add_argument("--from", dest="since", help="RFC 3339 start of the window")
    parser.add_argument("--to", dest="until", help="RFC 3339 end of the window")
    args = parser.parse_args(argv)

    if not args.records:
        return _demo()

    if not (args.agent and args.since and args.until):
        parser.error("--records requires --agent, --from and --to")

    with open(args.records, encoding="utf-8") as handle:
        records = json.load(handle)

    try:
        report = scope(
            records,
            agent_id=args.agent,
            since=_parse_ts(args.since),
            until=_parse_ts(args.until),
        )
    except IncidentInconclusive as inconclusive:
        for reason in inconclusive.reasons:
            print(f"refusing to scope: {reason}", file=sys.stderr)
        return 2

    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
