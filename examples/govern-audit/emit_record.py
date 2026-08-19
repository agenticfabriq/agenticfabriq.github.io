"""Emit one structured audit record for one agent tool call.

Standard library only, so you can run it right now:

    python3 emit_record.py > records.jsonl
    python3 verify_chain.py records.jsonl

Pass a libpq connection string to append to the store in `schema.sql`:

    pip install "psycopg[binary]"
    python3 emit_record.py "postgresql://gateway_svc@localhost/auditdb"

Three properties are worth reading the code for:

1. The record is written at the *decision* point, around the call, so a denial
   produces a record even though no call ever happened.
2. Arguments are split into a digest (evidence) and a shape (convenience).
   The raw argument values never reach the record.
3. The exact bytes that were hashed are stored alongside the hash, in
   `canonical_form`. Verification reads those bytes back rather than
   re-deriving them from typed columns -- see the note above `seal()`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------

# sort_keys makes the digest independent of dict ordering; separators removes
# insignificant whitespace. This is a pragmatic canonical form, not RFC 8785
# JCS -- it does not normalise number formatting, so a float that round-trips
# as 1.0 in one language and 1 in another will digest differently. If records
# are produced by more than one runtime, use a JCS implementation instead of
# this function, and say which one in your control documentation.
def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Allowlist, not denylist. A denylist of secret-looking key names fails the
# first time somebody names a field `notes` and pastes a token into it.
SHAPE_ALLOWLIST: dict[str, frozenset[str]] = {
    "crm.contact": frozenset({"contact_id", "field", "limit"}),
    "billing.refund": frozenset({"invoice_id", "currency", "reason_code"}),
    "storage.object": frozenset({"bucket", "key", "range"}),
}


# A digest is a lookup key for anyone who can guess the input. Hashing a
# boolean, a four-digit PIN, or an account number from a known range hides
# nothing: an investigator -- or an attacker with a copy of the log -- digests
# every candidate and compares. Truncating the digest does not fix that; it only
# adds collisions.
#
# So the fingerprint below is an HMAC under a key that lives outside the audit
# store, held by whoever operates the redaction boundary rather than by whoever
# reads the log. Someone holding the log alone cannot enumerate; someone holding
# the log and the key can still confirm that two calls carried the same value,
# which is the property that makes the field worth recording.
#
# Without a key this falls back to a bare digest and says so in the record, so
# an investigator can see which fingerprints are enumerable rather than assuming
# none of them are.
def fingerprint(encoded: bytes) -> tuple[str, str]:
    key = os.environ.get("AUDIT_FINGERPRINT_KEY", "")
    if key:
        return "hmac-sha256", hmac.new(key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
    return "sha256-unkeyed", sha256_hex(encoded)


def describe(value: Any) -> dict[str, Any]:
    """Describe a withheld value without reproducing it."""
    encoded = canonical_json(value).encode("utf-8")
    algorithm, digest = fingerprint(encoded)
    return {
        "type": type(value).__name__,
        "bytes": len(encoded),
        "fingerprint_alg": algorithm,
        "fingerprint": digest,
    }


def split_arguments(resource: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (full-argument digest, per-key shape with values withheld).

    The digest is taken over the *complete* arguments, because its job is to
    let you confirm later that a specific argument set was the one used: you
    produce the candidate from the source system and check that it digests to
    the same value. That is a confirmation primitive, not a concealment one --
    anyone who can guess the whole argument set can also confirm it. It is
    acceptable here because a full argument set is rarely guessable; it would
    not be acceptable for a single low-entropy field, which is why individual
    withheld values go through `fingerprint` instead.

    The shape is what an investigator reads without an approval workflow.
    """
    digest = sha256_hex(canonical_json(arguments).encode("utf-8"))
    allowed = SHAPE_ALLOWLIST.get(resource, frozenset())
    shape: dict[str, Any] = {}
    for key in sorted(arguments):
        if key in allowed:
            shape[key] = arguments[key]
        else:
            shape[key] = {"redacted": describe(arguments[key])}
    return digest, shape


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRecord:
    record_id: str
    occurred_at: str          # RFC 3339, always UTC, always from one clock
    agent_id: str             # who acted
    principal_id: str | None  # who it acted for; explicit None, never omitted
    principal_type: str
    grant_id: str | None      # under what authority
    resource: str
    action: str
    arguments_sha256: str
    arguments_shape: dict[str, Any]
    decision: str             # allow | deny
    decision_reason: str
    policy_version: str
    outcome: str              # success | error | blocked
    outcome_detail: str | None
    correlation_id: str
    parent_span_id: str | None
    model_id: str | None
    prompt_version: str | None
    tool_version: str | None
    untrusted_input: bool
    chain_id: str
    chain_seq: int
    prev_hash: str | None


@dataclass(frozen=True)
class SealedRecord:
    """A record plus the exact bytes that were hashed, plus the hash."""

    record: AuditRecord
    canonical_form: str
    record_hash: str

    def as_line(self) -> str:
        """One JSONL line carrying the record, the hashed bytes, and the hash.

        `canonical_form` duplicates every field above it, and that redundancy is
        deliberate. It is what lets a verifier check the hash without
        re-deriving the byte string, which is exactly what fails when records
        come back out of a database: PostgreSQL renders `timestamptz` with a
        space instead of the T, a two-digit offset instead of +00:00, and
        trailing zeros stripped, so an occurred_at emitted as
        ...:41.979183+00:00 reads back as ...:41.979183+00 and rehashes to a
        different value. That is EVERY record, not the tenth of them whose
        microseconds happen to end in a zero. Hash the bytes, store the bytes,
        verify the bytes.
        """
        payload = asdict(self.record)
        payload["canonical_form"] = self.canonical_form
        payload["record_hash"] = self.record_hash
        return json.dumps(payload, sort_keys=True)


def seal(record: AuditRecord) -> SealedRecord:
    canonical_form = canonical_json(asdict(record))
    return SealedRecord(
        record=record,
        canonical_form=canonical_form,
        record_hash=sha256_hex(canonical_form.encode("utf-8")),
    )


class Denied(Exception):
    """Policy refused the call. Carries the reason so the record can state it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def now_rfc3339() -> str:
    # timespec="microseconds" keeps ordering stable for calls inside the same
    # millisecond. Never use a local-time clock here: an audit trail spanning
    # two regions with two offsets cannot be sorted.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Chain sources
# ---------------------------------------------------------------------------
#
# A hash chain has one head, so it admits one writer at a time. Two emitters
# that read the same head produce two records claiming the same predecessor,
# and that fork is indistinguishable from tampering when the verifier runs.
#
# There are exactly two ways out, and you must pick one:
#
#   1. Serialise every writer on a chain behind a lock.
#   2. Give each emitter its own chain, and anchor each chain's head separately.
#
# This file does both at once: one chain per emitter instance (option 2), with
# the head handed out under a row lock (option 1) so that two processes which
# accidentally share a chain id still cannot fork it.


class LocalChain:
    """In-process chain. Correct for exactly one process, as a demo is."""

    def __init__(self, chain_id: str) -> None:
        self.chain_id = chain_id
        self._prev: str | None = None
        self._seq = 0

    def next_link(self) -> tuple[str | None, int]:
        self._seq += 1
        return self._prev, self._seq

    def advance(self, record_hash: str) -> None:
        self._prev = record_hash


class DatabaseChain:
    """Chain head read from the store under a row lock held to commit.

    The writer role holds no SELECT on audit.agent_action -- it cannot read the
    records it appends. `audit.next_link` is SECURITY DEFINER precisely so the
    writer can obtain the one value it needs without that privilege.
    """

    def __init__(self, connection, chain_id: str) -> None:
        self.connection = connection
        self.chain_id = chain_id

    def next_link(self) -> tuple[str | None, int]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT encode(prev_hash, 'hex'), next_seq FROM audit.next_link(%s)",
                (self.chain_id,),
            )
            return cursor.fetchone()

    def advance(self, record_hash: str) -> None:
        # The BEFORE INSERT trigger advances audit.chain_head itself, so there
        # is nothing to do here. Committing releases the lock taken above.
        self.connection.commit()


# ---------------------------------------------------------------------------
# Recording one call
# ---------------------------------------------------------------------------


def record_tool_call(
    *,
    agent_id: str,
    principal_id: str | None,
    principal_type: str,
    grant_id: str | None,
    resource: str,
    action: str,
    arguments: dict[str, Any],
    correlation_id: str,
    parent_span_id: str | None,
    model_id: str,
    prompt_version: str,
    tool_version: str,
    untrusted_input: bool,
    policy,
    invoke,
    chain,
) -> tuple[SealedRecord, Any]:
    """Evaluate policy, call the tool, and return the sealed record.

    `policy(...)` raises Denied to refuse. `invoke(...)` performs the real call.
    Both are injected so this function is the only place that decides what the
    record says, which is the property that makes the record trustworthy.
    """
    digest, shape = split_arguments(resource, arguments)
    occurred_at = now_rfc3339()

    decision, reason, outcome, detail, result = "allow", "", "success", None, None
    try:
        reason = policy(agent_id, principal_id, resource, action, arguments)
    except Denied as denial:
        decision, reason, outcome = "deny", denial.reason, "blocked"
    else:
        try:
            result = invoke(resource, action, arguments)
        except Exception as error:  # noqa: BLE001 - the record must survive any failure
            outcome = "error"
            # The class name and nothing else. Exception strings routinely
            # carry the argument that caused them, which is how a redacted
            # field reappears in a column you did not think of as an argument.
            detail = type(error).__name__

    # Claim the chain slot only once the outcome is known, so the lock is held
    # for the write and not for the duration of the tool call.
    prev_hash, chain_seq = chain.next_link()

    sealed = seal(
        AuditRecord(
            record_id=str(uuid.uuid4()),
            occurred_at=occurred_at,
            agent_id=agent_id,
            principal_id=principal_id,
            principal_type=principal_type,
            grant_id=grant_id,
            resource=resource,
            action=action,
            arguments_sha256=digest,
            arguments_shape=shape,
            decision=decision,
            decision_reason=reason,
            policy_version=os.environ.get("POLICY_VERSION", "policy-2026-08-04"),
            outcome=outcome,
            outcome_detail=detail,
            correlation_id=correlation_id,
            parent_span_id=parent_span_id,
            model_id=model_id,
            prompt_version=prompt_version,
            tool_version=tool_version,
            untrusted_input=untrusted_input,
            chain_id=chain.chain_id,
            chain_seq=chain_seq,
            prev_hash=prev_hash,
        )
    )
    return sealed, result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

# recorded_at is deliberately absent from this column list. The writer role has
# no INSERT privilege on it -- see the column-level GRANT in schema.sql -- so
# naming it here would turn every append into a permission error.
INSERT = """
INSERT INTO audit.agent_action (
    record_id, occurred_at, agent_id, principal_id, principal_type, grant_id,
    resource, action, arguments_sha256, arguments_shape,
    decision, decision_reason, policy_version, outcome, outcome_detail,
    correlation_id, parent_span_id,
    model_id, prompt_version, tool_version, untrusted_input,
    canonical_form, prev_hash, record_hash, chain_id, chain_seq
) VALUES (
    %(record_id)s, %(occurred_at)s, %(agent_id)s, %(principal_id)s,
    %(principal_type)s, %(grant_id)s, %(resource)s, %(action)s,
    decode(%(arguments_sha256)s, 'hex'), %(arguments_shape)s,
    %(decision)s, %(decision_reason)s, %(policy_version)s,
    %(outcome)s, %(outcome_detail)s, %(correlation_id)s, %(parent_span_id)s,
    %(model_id)s, %(prompt_version)s, %(tool_version)s, %(untrusted_input)s,
    %(canonical_form)s, decode(%(prev_hash)s, 'hex'),
    decode(%(record_hash)s, 'hex'), %(chain_id)s, %(chain_seq)s
)
"""


def append(connection, sealed: SealedRecord) -> None:
    """Append one record inside the transaction that holds the chain lock.

    Commit the audit write separately from the business transaction. Sharing a
    transaction means a rollback erases the evidence that the work was
    attempted, which is exactly the case you most want a record of.
    """
    params = asdict(sealed.record)
    params["arguments_shape"] = json.dumps(params["arguments_shape"])
    params["canonical_form"] = sealed.canonical_form
    params["record_hash"] = sealed.record_hash
    with connection.cursor() as cursor:
        cursor.execute(INSERT, params)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo_policy(agent_id, principal_id, resource, action, arguments) -> str:
    if action == "write" and arguments.get("amount_cents", 0) > 50_000:
        raise Denied("refund_ceiling: amount_cents exceeds delegated limit 50000")
    return "grant:refunds.write matched, amount within delegated limit"


def demo_invoke(resource, action, arguments):
    return {"status": "ok"}


CALLS = [
    {"invoice_id": "inv_88213", "currency": "USD", "reason_code": "duplicate",
     "amount_cents": 4200, "customer_note": "charged twice on the 3rd"},
    {"invoice_id": "inv_88300", "currency": "USD", "reason_code": "goodwill",
     "amount_cents": 250_000, "customer_note": "asked for a full refund"},
]


def run(chain, connection=None) -> None:
    correlation_id = "4bf92f3577b34da6a3ce929d0e0e4736"  # a W3C trace-id

    for arguments in CALLS:
        sealed, _ = record_tool_call(
            agent_id="agent:billing-triage@v7",
            principal_id="user:ada@example.com",
            principal_type="human",
            grant_id="grant:01JQ8Z9RMK",
            resource="billing.refund",
            action="write",
            arguments=arguments,
            correlation_id=correlation_id,
            parent_span_id="00f067aa0ba902b7",
            model_id="claude-sonnet-4-5-20250929",
            prompt_version="triage-prompt@3f2a1c9",
            tool_version="billing-tools@2.4.1",
            untrusted_input=True,  # the note came from a customer email
            policy=demo_policy,
            invoke=demo_invoke,
            chain=chain,
        )
        if connection is not None:
            append(connection, sealed)
        chain.advance(sealed.record_hash)

        # One record per line, so this composes:
        #   python3 emit_record.py > records.jsonl
        #   python3 verify_chain.py records.jsonl
        print(sealed.as_line())


def main() -> None:
    dsn = sys.argv[1] if len(sys.argv) > 1 else None
    # One chain per emitter instance. In a real deployment this is the emitter's
    # stable identity -- pod name, worker id -- not a fresh uuid per process, or
    # you accumulate one anchor per restart.
    chain_id = os.environ.get("AUDIT_CHAIN_ID", "chain:emitter-1")

    if dsn is None:
        run(LocalChain(chain_id))
        return

    import psycopg  # imported here so the demo runs without the driver

    with psycopg.connect(dsn) as connection:
        run(DatabaseChain(connection, chain_id), connection)
    # stderr, so it never lands in the record stream on stdout.
    print(f"appended {len(CALLS)} records to chain {chain_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
