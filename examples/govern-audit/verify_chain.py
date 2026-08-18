"""Verify the hash chain over an export of audit records.

    python3 emit_record.py > records.jsonl        # straight from the emitter
    python3 verify_chain.py records.jsonl

    psql -t -A -v chain="'chain:emitter-1'" \
         -f export.sql > records.jsonl            # or out of the store
    python3 verify_chain.py records.jsonl

Both inputs work because both carry `canonical_form`: the exact byte string the
emitter hashed. This verifier never re-derives that string from the record's
fields, and that is the whole design. Re-deriving it is the obvious approach and
it fails against a database -- PostgreSQL renders `timestamptz` with trailing
zeros stripped, so a record emitted at ...:41.979180+00:00 reads back as
...:41.97918+00:00 and rehashes differently. That produces a tamper alert on
roughly one record in ten, and an integrity control that cries wolf is worse
than none.

Four checks per record:

  * record_hash is the SHA-256 of canonical_form -- no field was edited;
  * prev_hash equals the previous record's record_hash -- nothing was removed
    from the middle or reordered;
  * chain_seq increments by one -- the same, stated independently of any clock;
  * the queryable columns still agree with canonical_form -- so editing a column
    an investigator reads, while leaving the hashed bytes alone, is caught.

Read the limits before you rely on this. It does NOT detect:

  * truncation of the tail -- lopping off the last N records leaves a chain that
    verifies perfectly;
  * forgery by whoever holds the append privilege -- a well-formed record
    chained to the genuine head verifies, because appending is exactly what that
    role is allowed to do;
  * wholesale rewriting by someone who can modify records and recompute every
    hash from the edit forward.

The first and third close the same way: publish the head hash and the record
count it covers, on a fixed cadence, to a system under different credentials.
Then a truncation is a head hash you cannot reproduce, not a silence. That is
the construction certificate transparency uses, where a signed tree head is
gossiped so no single operator can quietly rewrite history.

The second does not close with cryptography at all. It closes by the emitter not
being the component under audit.
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Iterator

# Fields cross-checked between the queryable columns and the hashed bytes.
# Deliberately all strings and integers: types that survive a round trip
# unchanged. Timestamps are excluded because they do not, which is the bug this
# whole file is shaped around.
CROSS_CHECKED = (
    "record_id",
    "agent_id",
    "principal_id",
    "resource",
    "action",
    "decision",
    "outcome",
    "chain_id",
    "chain_seq",
)


def read_lines(path: str) -> Iterator[tuple[int, dict[str, Any]]]:
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{line_number}: not valid JSON: {error}") from error


def verify(path: str) -> list[str]:
    problems: list[str] = []
    # One head per chain. Records from several emitters may share a file, and
    # each chain advances on its own.
    heads: dict[str, tuple[str | None, int]] = {}
    count = 0

    for line_number, line in read_lines(path):
        count += 1
        canonical_form = line.get("canonical_form")
        record_hash = line.get("record_hash")

        if not isinstance(canonical_form, str) or not isinstance(record_hash, str):
            problems.append(
                f"line {line_number}: no canonical_form/record_hash pair. "
                "Export with export.sql rather than reconstructing the record "
                "from typed columns."
            )
            continue

        body = json.loads(canonical_form)
        record_id = body.get("record_id", f"<line {line_number}>")

        computed = hashlib.sha256(canonical_form.encode("utf-8")).hexdigest()
        if computed != record_hash:
            problems.append(f"{record_id}: record_hash does not cover canonical_form")

        chain_id = body.get("chain_id")
        expected_prev, expected_seq = heads.get(chain_id, (None, 1))

        if body.get("prev_hash") != expected_prev:
            problems.append(
                f"{record_id}: prev_hash {body.get('prev_hash')!r} does not match "
                f"the previous record_hash {expected_prev!r} in chain {chain_id!r} "
                "-- a record was removed, reordered, or inserted"
            )
        if body.get("chain_seq") != expected_seq:
            problems.append(
                f"{record_id}: chain_seq {body.get('chain_seq')!r} breaks the "
                f"sequence in chain {chain_id!r}, expected {expected_seq}"
            )

        # Cross-check the queryable columns against the hashed bytes.
        columns = line.get("columns", line)
        for field in CROSS_CHECKED:
            if field in columns and columns[field] != body.get(field):
                problems.append(
                    f"{record_id}: column {field}={columns[field]!r} disagrees with "
                    f"canonical_form {body.get(field)!r} -- the queryable copy was "
                    "edited while the hashed bytes were left alone"
                )

        # Chain from the values the record actually carries, not the ones it
        # should have carried. Otherwise a single break cascades into a failure
        # on every later record and the report says nothing about where it
        # started.
        heads[chain_id] = (record_hash, body.get("chain_seq", expected_seq) + 1)

    if count == 0:
        problems.append(f"{path}: no records")
    return problems


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_chain.py <records.jsonl>")

    problems = verify(sys.argv[1])
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        raise SystemExit(1)
    print(
        "chain intact -- no edit, no internal deletion, columns agree with the "
        "hashed bytes. This does NOT prove the tail is complete or that every "
        "record describes something that happened: compare the head hash "
        "against your external anchor, and remember that whoever may append "
        "may also append a lie."
    )


if __name__ == "__main__":
    main()
