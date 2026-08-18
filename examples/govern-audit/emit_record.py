"""Emit one structured audit record for one agent tool call.

Standard library only, so you can run it right now:

    python3 emit_record.py

It prints the record it would append and the SHA-256 chain link. Pass a libpq
connection string to actually insert into the schema in `schema.sql`:

    pip install "psycopg[binary]"
    python3 emit_record.py "postgresql://writer@localhost/auditdb"

Two properties are worth reading the code for:

1. The record is written at the *decision* point, around the call, so a denial
   produces a record even though no call ever happened.
2. Arguments are split into a digest (evidence) and a shape (convenience).
   The raw argument values never reach the record.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
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
def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


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
    encoded = canonical_json(value)
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
    digest = sha256_hex(canonical_json(arguments))
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
    prev_hash: str | None
    record_hash: str = field(default="")

    def sealed(self, prev_hash: str | None) -> "AuditRecord":
        """Return a copy carrying prev_hash and the hash over every field."""
        body = asdict(self)
        body.pop("record_hash")
        body["prev_hash"] = prev_hash
        return AuditRecord(
            **body, record_hash=sha256_hex(canonical_json(body))
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
    prev_hash: str | None,
) -> tuple[AuditRecord, Any]:
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
            # field reappears three columns to the right.
            detail = type(error).__name__

    unsealed = AuditRecord(
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
        prev_hash=None,
    )
    return unsealed.sealed(prev_hash), result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

INSERT = """
INSERT INTO audit.agent_action (
    record_id, occurred_at, agent_id, principal_id, principal_type, grant_id,
    resource, action, arguments_sha256, arguments_shape,
    decision, decision_reason, policy_version, outcome, outcome_detail,
    correlation_id, parent_span_id,
    model_id, prompt_version, tool_version, untrusted_input,
    prev_hash, record_hash
) VALUES (
    %(record_id)s, %(occurred_at)s, %(agent_id)s, %(principal_id)s,
    %(principal_type)s, %(grant_id)s, %(resource)s, %(action)s,
    decode(%(arguments_sha256)s, 'hex'), %(arguments_shape)s,
    %(decision)s, %(decision_reason)s, %(policy_version)s,
    %(outcome)s, %(outcome_detail)s, %(correlation_id)s, %(parent_span_id)s,
    %(model_id)s, %(prompt_version)s, %(tool_version)s, %(untrusted_input)s,
    decode(%(prev_hash)s, 'hex'), decode(%(record_hash)s, 'hex')
)
"""


def append(connection, record: AuditRecord) -> None:
    """Append one record. Commit it separately from the business transaction.

    Sharing a transaction with the work being audited means a rollback erases
    the evidence that the work was attempted, which is exactly the case you
    most want a record of.
    """
    import json as _json

    params = asdict(record)
    params["arguments_shape"] = _json.dumps(params["arguments_shape"])
    with connection.cursor() as cursor:
        cursor.execute(INSERT, params)
    connection.commit()


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo_policy(agent_id, principal_id, resource, action, arguments) -> str:
    if action == "write" and arguments.get("amount_cents", 0) > 50_000:
        raise Denied("refund_ceiling: amount_cents exceeds delegated limit 50000")
    return "grant:refunds.write matched, amount within delegated limit"


def demo_invoke(resource, action, arguments):
    return {"status": "ok"}


def main() -> None:
    correlation_id = "4bf92f3577b34da6a3ce929d0e0e4736"  # a W3C trace-id
    prev_hash = None

    calls = [
        {"invoice_id": "inv_88213", "currency": "USD", "reason_code": "duplicate",
         "amount_cents": 4200, "customer_note": "charged twice on the 3rd"},
        {"invoice_id": "inv_88300", "currency": "USD", "reason_code": "goodwill",
         "amount_cents": 250_000, "customer_note": "asked for a full refund"},
    ]

    records = []
    for arguments in calls:
        record, _ = record_tool_call(
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
            prev_hash=prev_hash,
        )
        prev_hash = record.record_hash
        records.append(record)
        # One record per line, so this composes:
        #   python3 emit_record.py > records.jsonl
        #   python3 verify_chain.py records.jsonl
        print(json.dumps(asdict(record), sort_keys=True))

    dsn = sys.argv[1] if len(sys.argv) > 1 else None
    if dsn:
        import psycopg  # imported here so the demo runs without the driver

        with psycopg.connect(dsn) as connection:
            for record in records:
                append(connection, record)
        # stderr, so it never lands in the record stream on stdout.
        print(f"appended {len(records)} records", file=sys.stderr)


if __name__ == "__main__":
    main()
