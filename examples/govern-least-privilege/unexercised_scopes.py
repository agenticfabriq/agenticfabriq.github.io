"""Report which of an agent's granted scopes were never exercised in 30 days.

Unexercised scope is the only over-grant you can remove without an argument:
nobody has to agree it is unnecessary, because the evidence says it was unused.

The scope set analysed here is NOT hardcoded. It is derived from
`scope_manifest.py`, which the guide argues should be the single checked-in
source of truth for what an agent holds. Specifically it is the *pre-narrowing*
grant -- every scope currently in the manifest plus every scope in its RETIRED
section -- so running this reproduces the report that justified the removals.
The two scopes it flags are exactly the two the manifest records as retired.

    python3 unexercised_scopes.py

Input is a list of activity records. For Google Workspace these come from the
Admin SDK Reports API -- Activities.list() with applicationName=token -- where
the `activity` event carries the api_name and method_name parameters. Zendesk,
Slack, Linear and Notion publish no comparable per-scope usage feed, so for
those the only source is your own runtime or gateway log; both shapes are
modelled below. This script takes already-fetched records so it needs no
credentials.

Two honest caveats, both of which apply to any implementation of this metric:

1. The mapping from a called method to the scope that permitted it comes from
   each manifest row's own `calls` field. That keeps one table instead of two,
   but it inherits the manifest's errors: a grant whose `calls` list is
   incomplete will make a live scope look dead. Anything this script cannot map
   is printed under UNMAPPED rather than silently dropped.
2. "Exercised" means a call was attempted under that scope. A record of an
   attempt is not proof the call succeeded, and AWS documents the same
   limitation for its last-accessed data. Treat a low-count scope as a
   question, not an answer.
3. Thirty days is a review cadence, not an evidence window. A scope exercised
   once a quarter is indistinguishable from a dead one inside it, so before
   deleting on a zero, re-run over a window that contains one of every periodic
   thing the agent does -- a month-end close, a quarterly report.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scope_manifest import MANIFEST, RETIRED  # noqa: E402

# The grant as it stood before step 3 of the method ran: what the agent holds
# now, plus what the review took away.
GRANTED: tuple[str, ...] = tuple(
    [grant.scope for grant in MANIFEST] + [scope for _provider, scope, _note in RETIRED]
)

# Derived, not hand-written. Caveat 1 above.
METHOD_SCOPES: dict[str, tuple[str, ...]] = {}
for _grant in MANIFEST:
    for _call in _grant.calls:
        METHOD_SCOPES.setdefault(_call, ())
        METHOD_SCOPES[_call] += (_grant.scope,)

# Google Workspace: the Reports API activities resource. Each record carries an
# events list, each event a parameters list of {"name": ..., "value": ...}.
# The exact string a deployment sees in method_name is worth confirming against
# your own log before you trust a report built on it; match_method() below
# tolerates a differing prefix rather than requiring exact equality.
GOOGLE_RECORDS: tuple[dict, ...] = (
    {
        "id": {"time": "2026-08-01T09:14:02.000Z"},
        "events": [
            {
                "type": "token",
                "name": "activity",
                "parameters": [
                    {"name": "api_name", "value": "gmail"},
                    {"name": "method_name", "value": "gmail.users.messages.list"},
                    {"name": "client_id", "value": "support-triage.apps.googleusercontent.com"},
                ],
            }
        ],
    },
    {
        "id": {"time": "2026-08-01T09:14:05.000Z"},
        "events": [
            {
                "type": "token",
                "name": "activity",
                "parameters": [
                    {"name": "api_name", "value": "gmail"},
                    {"name": "method_name", "value": "gmail.users.messages.get"},
                    {"name": "client_id", "value": "support-triage.apps.googleusercontent.com"},
                ],
            }
        ],
    },
    {
        # An authorize event names every scope the user consented to. It is the
        # grant, not the usage, and called_methods() ignores it deliberately.
        "id": {"time": "2026-08-14T02:00:11.000Z"},
        "events": [
            {
                "type": "token",
                "name": "authorize",
                "parameters": [
                    {"name": "client_id", "value": "support-triage.apps.googleusercontent.com"},
                    {"name": "scope", "value": " ".join(GRANTED)},
                ],
            }
        ],
    },
)

# Everyone else: your own log. The only requirement is that it records the call
# in the same form the manifest names it, so the two can be joined.
GATEWAY_RECORDS: tuple[dict, ...] = (
    {"time": "2026-08-01T09:14:09.000Z", "agent": "support-triage", "call": "GET /api/v2/users/search"},
    {"time": "2026-08-01T09:14:11.000Z", "agent": "support-triage", "call": "POST /api/v2/tickets"},
    {"time": "2026-08-01T09:14:12.000Z", "agent": "support-triage", "call": "chat.postMessage"},
)


def called_methods(google_records: tuple[dict, ...], gateway_records: tuple[dict, ...]) -> Counter[str]:
    """Count one entry per attempted call, across both record shapes."""
    counts: Counter[str] = Counter()
    for record in google_records:
        for event in record.get("events", []):
            if event.get("name") != "activity":
                continue
            for parameter in event.get("parameters", []):
                if parameter.get("name") == "method_name" and parameter.get("value"):
                    counts[parameter["value"]] += 1
    for record in gateway_records:
        if record.get("call"):
            counts[record["call"]] += 1
    return counts


def match_method(method: str) -> tuple[str, ...]:
    """Resolve a logged call to the scopes that permit it."""
    if method in METHOD_SCOPES:
        return METHOD_SCOPES[method]
    # Tolerate a differing prefix (e.g. "users.messages.list") without guessing
    # at a match: require the logged name to end with a known key's tail.
    for known, scopes in METHOD_SCOPES.items():
        if "." not in known:
            continue
        tail = known.split(".", 1)[1]
        if method.endswith(tail):
            return scopes
    return ()


def report(
    granted: tuple[str, ...],
    google_records: tuple[dict, ...],
    gateway_records: tuple[dict, ...],
) -> tuple[str, dict[str, int]]:
    counts = called_methods(google_records, gateway_records)
    per_scope = {scope: 0 for scope in granted}
    unmapped: Counter[str] = Counter()

    for method, count in counts.items():
        scopes = match_method(method)
        if not scopes:
            unmapped[method] += count
            continue
        for scope in scopes:
            if scope in per_scope:
                per_scope[scope] += count

    exercised = [scope for scope, count in per_scope.items() if count > 0]
    unexercised = [scope for scope, count in per_scope.items() if count == 0]

    lines = [
        f"granted:    {len(granted)}  (manifest + retired, i.e. the pre-narrowing grant)",
        f"exercised:  {len(exercised)} ({len(exercised) / len(granted):.0%})",
        "",
        "EXERCISED",
    ]
    for scope in exercised:
        lines.append(f"  {per_scope[scope]:>5}  {scope}")
    lines += ["", "UNEXERCISED -- default action is removal"]
    for scope in unexercised:
        lines.append(f"      0  {scope}")
    if unmapped:
        lines += ["", "UNMAPPED CALLS -- no manifest row claims these; fix that before trusting the numbers"]
        for method, count in unmapped.most_common():
            lines.append(f"  {count:>5}  {method}")
    return "\n".join(lines), per_scope


def main() -> int:
    text, per_scope = report(GRANTED, GOOGLE_RECORDS, GATEWAY_RECORDS)
    print(text)
    # Non-zero exit turns this into a scheduled check that files an issue.
    return 1 if any(count == 0 for count in per_scope.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
