"""Report which of an agent's granted scopes were never exercised in 30 days.

Unexercised scope is the only over-grant you can remove without an argument:
nobody has to agree it is unnecessary, because the evidence says it was unused.

Input is a list of activity records. For Google Workspace these come from the
Admin SDK Reports API -- Activities.list() with applicationName=token -- where
the `activity` event carries the api_name and method_name parameters. This
script takes already-fetched records so it runs with no credentials:

    python3 unexercised_scopes.py

Two honest caveats, both of which apply to any implementation of this metric:

1. The mapping from a called method to the scope that permitted it is yours to
   maintain. No provider publishes it in a machine-readable form, and a method
   is frequently permitted by several scopes (users.messages.modify accepts
   three). METHOD_SCOPES below is a hand-written table; it is the part of this
   script most likely to be wrong, and a missing entry shows up as a scope that
   looks unused when it is not.
2. "Exercised" means a call was attempted under that scope. A record of an
   attempt is not proof the call succeeded, and AWS documents the same
   limitation for its last-accessed data. Treat a low-count scope as a question,
   not an answer.
"""

from __future__ import annotations

from collections import Counter

# Scopes currently granted to the agent, from scope_manifest.py.
GRANTED: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
)

# Hand-maintained: which granted scope permits which method. Caveat 1 above.
METHOD_SCOPES: dict[str, tuple[str, ...]] = {
    "gmail.users.messages.list": ("https://www.googleapis.com/auth/gmail.readonly",),
    "gmail.users.messages.get": ("https://www.googleapis.com/auth/gmail.readonly",),
    "gmail.users.threads.get": ("https://www.googleapis.com/auth/gmail.readonly",),
    "gmail.users.messages.send": ("https://www.googleapis.com/auth/gmail.send",),
    "gmail.users.labels.list": ("https://www.googleapis.com/auth/gmail.labels",),
}

# Shape follows the Reports API activities resource: each record carries an
# events list, each event a parameters list of {"name": ..., "value": ...}.
# The exact string a deployment sees in method_name is worth confirming against
# your own log before you trust a report built on it; match_method() below
# tolerates a provider prefix rather than requiring an exact equality.
SAMPLE_RECORDS: tuple[dict, ...] = (
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


def called_methods(records: tuple[dict, ...]) -> Counter[str]:
    """Count method_name values across `activity` events only.

    An `authorize` event names every scope the user consented to, which is the
    grant, not the usage. Counting it would make every scope look exercised --
    the exact mistake this report exists to avoid.
    """
    counts: Counter[str] = Counter()
    for record in records:
        for event in record.get("events", []):
            if event.get("name") != "activity":
                continue
            for parameter in event.get("parameters", []):
                if parameter.get("name") == "method_name" and parameter.get("value"):
                    counts[parameter["value"]] += 1
    return counts


def match_method(method: str) -> tuple[str, ...]:
    """Resolve a logged method name to the scopes that permit it."""
    if method in METHOD_SCOPES:
        return METHOD_SCOPES[method]
    # Tolerate a differing prefix (e.g. "users.messages.list") without guessing
    # at a match: require the logged name to end with a known key's tail.
    for known, scopes in METHOD_SCOPES.items():
        tail = known.split(".", 1)[1] if "." in known else known
        if method.endswith(tail):
            return scopes
    return ()


def report(granted: tuple[str, ...], records: tuple[dict, ...]) -> tuple[str, dict[str, int]]:
    counts = called_methods(records)
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
        f"granted:    {len(granted)}",
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
        lines += ["", "UNMAPPED METHODS -- fix METHOD_SCOPES before trusting the numbers above"]
        for method, count in unmapped.most_common():
            lines.append(f"  {count:>5}  {method}")
    return "\n".join(lines), per_scope


def main() -> int:
    text, per_scope = report(GRANTED, SAMPLE_RECORDS)
    print(text)
    # Non-zero exit turns this into a scheduled check that files an issue.
    return 1 if any(count == 0 for count in per_scope.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
