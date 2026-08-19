"""Verify the hash chain over an export of audit records.

    python3 emit_record.py > records.jsonl        # straight from the emitter
    python3 verify_chain.py records.jsonl

    psql -t -A -v ON_ERROR_STOP=1 -v chain="'chain:emitter-1'" \
         -f export.sql > records.jsonl            # or out of the store
    python3 verify_chain.py records.jsonl

Both inputs work because both carry `canonical_form`: the exact byte string the
emitter hashed. This verifier never re-derives that string from the record's
fields, and that is the whole design. Re-deriving it is the obvious approach and
it fails against a database. PostgreSQL renders `timestamptz` with a space
instead of the T, a two-digit offset instead of +00:00, and trailing zeros
stripped, so a record emitted at ...:41.979183+00:00 reads back as
...:41.979183+00 and rehashes differently. Re-derivation therefore fails on
EVERY record, not on some unlucky tenth of them, and an integrity control that
cries wolf against a healthy store is worse than none.

Four checks per record:

  * record_hash is the SHA-256 of canonical_form -- no field was edited;
  * prev_hash equals the previous record's record_hash -- nothing was removed
    from the middle or reordered;
  * chain_seq increments by one -- the same, stated independently of any clock;
  * every column the exporter ships still agrees with canonical_form -- so
    editing a column an investigator reads, while leaving the hashed bytes
    alone, is caught. export.sql re-renders each column into the emitter's
    spelling, so this covers occurred_at and the bytea and jsonb columns too,
    not only the ones that happen to be plain text. Coverage is exactly what
    export.sql selects: a column added to the table but not to that file is not
    checked here, and nothing in this script can tell.

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

# Every field the exporter puts in `columns` is cross-checked -- there is no
# allowlist here, on purpose. An earlier version named nine fields while the
# docstring claimed the check covered what an investigator reads; that left
# decision_reason, grant_id, untrusted_input and occurred_at unchecked, all four
# of which investigate.sql prints. Adding a column to the exporter now extends
# the check automatically instead of silently widening the gap.
#
# One field is necessarily absent, and its absence is not a gap: recorded_at is
# stamped by the store and never reaches the emitter, so it is not in
# canonical_form and there is nothing to compare it against. It is protected by
# the writer holding no INSERT privilege on it, not by this file.
NOT_IN_CANONICAL_FORM = frozenset({"recorded_at"})

# Keys that are envelope, not record content, and so are never cross-checked.
RESERVED_KEYS = frozenset({"canonical_form", "record_hash", "columns", "__malformed__"})


def read_lines(path: str) -> Iterator[tuple[int, dict[str, Any]]]:
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as error:
                # Report rather than raise, so one malformed record is listed
                # alongside every other problem in the file. A verifier that
                # aborts on the first bad line makes you fix an export one
                # record at a time, which is how people end up not fixing it.
                yield line_number, {"__malformed__": str(error)}


def verify(path: str) -> list[str]:
    problems: list[str] = []
    # One head per chain. Records from several emitters may share a file, and
    # each chain advances on its own.
    heads: dict[str, tuple[str | None, int]] = {}
    count = 0

    for line_number, line in read_lines(path):
        count += 1
        if "__malformed__" in line:
            problems.append(f"line {line_number}: not valid JSON: {line['__malformed__']}")
            continue

        canonical_form = line.get("canonical_form")
        record_hash = line.get("record_hash")

        if not isinstance(canonical_form, str) or not isinstance(record_hash, str):
            problems.append(
                f"line {line_number}: no canonical_form/record_hash pair. "
                "Export with export.sql rather than reconstructing the record "
                "from typed columns."
            )
            continue

        try:
            body = json.loads(canonical_form)
        except json.JSONDecodeError as error:
            problems.append(f"line {line_number}: canonical_form is not valid JSON: {error}")
            continue
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

        # Cross-check EVERY exported column against the hashed bytes. Iterating
        # the exporter's own output rather than a hand-maintained field list is
        # what keeps this honest: a column added to the store and to export.sql
        # is checked from that moment, with no second edit to remember.
        #
        # `columns` is how export.sql ships them; emit_record.py's stdout carries
        # the same fields at the top level instead. Fall back to that, but never
        # fall back to *nothing*: an export that lost its columns must fail
        # rather than sail through a check that had no work to do and still
        # printed a clean bill of health.
        #
        # Count the comparisons actually made, not the size of the mapping. A
        # non-empty `columns` whose every key is skipped below -- {"recorded_at":
        # ...}, or a mapping of nothing but reserved keys -- performs zero
        # comparisons, and testing the mapping for emptiness would pass it. That
        # is the same failure this check exists to prevent, one level further in:
        # a control that reports success while doing nothing.
        columns = line.get("columns")
        if columns is None:
            columns = {k: v for k, v in line.items() if k not in RESERVED_KEYS}
        compared = 0
        for field, value in sorted(columns.items()):
            if field in NOT_IN_CANONICAL_FORM or field in RESERVED_KEYS:
                continue
            if field not in body:
                problems.append(
                    f"{record_id}: column {field} is absent from canonical_form, "
                    "so it was never hashed and nothing vouches for it"
                )
                continue
            compared += 1
            if value != body[field]:
                problems.append(
                    f"{record_id}: column {field}={value!r} disagrees with "
                    f"canonical_form {body[field]!r} -- the queryable copy was "
                    "edited while the hashed bytes were left alone"
                )

        if compared == 0:
            problems.append(
                f"{record_id}: zero columns cross-checked -- this record carries a "
                "hash and nothing that was compared against it. Re-export with "
                "export.sql."
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
        "chain intact -- no edit, no internal deletion, and every exported "
        "column agrees with the hashed bytes. This does NOT prove the tail is "
        "complete or that any record describes something that happened: "
        "compare the head hash against your external anchor, and remember that "
        "whoever may append may also append a lie."
    )


if __name__ == "__main__":
    main()
