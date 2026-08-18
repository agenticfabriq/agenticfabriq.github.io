"""Verify the hash chain over an exported run of audit records.

    python3 verify_chain.py records.jsonl

Reads newline-delimited JSON records as produced by emit_record.py and checks
two things per record:

  * record_hash is the SHA-256 of every other field, so no field was edited;
  * prev_hash equals the previous record's record_hash, so no record in the
    middle was removed or reordered.

Read the limits before you rely on this. A self-contained chain detects edits
and internal deletions. It does NOT detect:

  * truncation of the tail -- lopping off the last N records leaves a chain
    that still verifies perfectly;
  * wholesale rewriting -- an attacker with the ability to modify records can
    recompute every hash from the edit forward.

Both holes close the same way: publish something outside the store's control
that pins the chain at a point in time. Write the head hash to a system with
different credentials on a fixed cadence, and record the count it covers. Then
a truncation is a head hash you cannot reproduce, not a silence. That is the
same construction certificate transparency logs use, where a signed tree head
is gossiped so that no single operator can quietly rewrite history.
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Iterator


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def read_records(path: str) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{line_number}: not valid JSON: {error}") from error


def verify(path: str) -> list[str]:
    problems: list[str] = []
    expected_prev: str | None = None
    count = 0

    for record in read_records(path):
        count += 1
        record_id = record.get("record_id", f"<record {count}>")

        body = {key: value for key, value in record.items() if key != "record_hash"}
        computed = hashlib.sha256(canonical_json(body)).hexdigest()
        if computed != record.get("record_hash"):
            problems.append(f"{record_id}: record_hash does not cover its own fields")

        if record.get("prev_hash") != expected_prev:
            problems.append(
                f"{record_id}: prev_hash {record.get('prev_hash')!r} "
                f"does not match the previous record_hash {expected_prev!r} "
                "-- a record was removed, reordered, or inserted"
            )

        # Chain from the hash the record actually carries, not the one it should
        # have carried. Otherwise a single break cascades into a failure on
        # every record after it and the report says nothing about where it
        # started.
        expected_prev = record.get("record_hash")

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
    print("chain intact (this proves no edit or internal deletion, not that "
          "the tail is complete -- compare the head hash against your anchor)")


if __name__ == "__main__":
    main()
