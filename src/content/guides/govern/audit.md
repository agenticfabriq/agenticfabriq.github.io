---
title: Audit Trails and Compliance for Agent Actions
description: "What belongs in an AI agent audit log, field by field: both principals, the policy decision, immutability, redaction, retention, and the query you will run."
pillar: govern
updated: "2026-08-18"
keywords:
  - ai agent audit log
  - agent action attribution
  - agent compliance evidence
  - append-only audit store
  - audit record schema
  - agent audit retention
relatedGuides:
  - govern/delegated-identity
  - govern/least-privilege
  - fail/token-leakage
howTo:
  - name: Write the question the trail has to answer before the schema
    text: Fix the four clauses — which human, through which agent, under what authority, could you have stopped it — and grade every proposed field against them.
  - name: Define the record field by field
    text: Give each field a named investigation it enables, and cut any field you cannot attach one to.
  - name: Split arguments into a digest and an allowlisted shape
    text: Hash the complete argument set for later confirmation, keep an allowlisted subset in the clear, and fingerprint everything else under a key held outside the audit store.
  - name: Emit the record at the policy decision point
    text: Wrap policy evaluation and the call in one function so a denial produces a record even though no call was ever made.
  - name: Make the store append-only and tamper-evident
    text: Grant INSERT column by column so the writer cannot supply its own recorded_at, block UPDATE and DELETE with a trigger, store the exact bytes you hashed, serialise writers per chain, and anchor the head hash somewhere the store's operator does not control.
  - name: Index for the query the investigation actually runs
    text: Put the two equality columns first and the time range last, so agent plus principal plus window is an index scan rather than a table scan.
  - name: Run the investigation and check the clocks
    text: Query everything one agent did for one principal in one window, then compare emitter timestamps against server-recorded ones before you trust the ordering.
faq:
  - question: What should an AI agent audit log contain?
    answer: One record per attempted action carrying a timestamp, the agent identity, the delegated human identity, the resource, the action, a digest of the arguments, the policy decision and the reason for it, the outcome, and a correlation id shared by every call in the same task. Add the model identifier, prompt version, tool version, and whether the input contained untrusted content.
  - question: Does SOC 2 require an immutable audit log?
    answer: SOC 2 is an attestation engagement against the AICPA Trust Services Criteria, and the categories in scope are selected for the engagement rather than fixed. The criteria are control objectives, and your auditor tests the controls you describe against them, so the specifics — field list, storage technology, retention period — come from your own documented policy plus any regulation that applies to you. If someone quotes you a SOC 2 logging requirement, ask which criterion it is and read that criterion.
  - question: How long should you keep AI agent audit logs?
    answer: There is no general-purpose standard number. NIST SP 800-53 writes audit record retention as an organisation-defined time period consistent with your records retention policy, rather than as a fixed value. Where a number does come from outside your company it is usually a sector rule — financial and health recordkeeping carry real statutory periods — so check whether one applies to you and cite it if it does. Otherwise set it where the cost of being unable to investigate an old incident meets the cost and risk of holding personal data longer, and write down which pressure set the number.
  - question: Are application logs good enough as an audit trail?
    answer: No. Application logs are lossy by design, their shape changes whenever a developer edits a log line, they usually omit denied actions entirely, and the service being audited normally holds permission to delete them. An audit trail needs a fixed schema, a record for every attempted action including refusals, and a store the audited component cannot write over.
  - question: Should an agent audit log store the arguments passed to a tool?
    answer: Store a digest of the complete argument set plus an allowlisted subset of low-risk keys in the clear, not the raw arguments. The digest lets you confirm later that a specific argument set was the one used, while keeping credentials, document bodies, and personal data out of a store that many people can read and that you may be unable to delete from.
  - question: How do you prove an agent audit log has not been tampered with?
    answer: Chain each record to a hash of the previous one, and store the exact bytes you hashed rather than re-deriving them from database columns, which do not round-trip — PostgreSQL alone changes the separator, the offset format, and trailing zeros on a timestamp. That detects any edited field and any record removed from the middle, provided you also compare every stored column back against those bytes rather than a subset of them. It does not detect truncation of the tail, so publish the head hash and record count on a fixed cadence to a system under different credentials. It also cannot detect a false record appended by whoever holds the append privilege, which is why the component being audited should not be the component writing the records.
---

## TL;DR

- Every audit trail exists to answer one question: **which human caused this, through which agent, under what authority, and could you have stopped it.** Grade any log you already have by whether it answers all four clauses, and treat a clause it cannot answer as a missing field, not a hard query.
- Application logs are not an audit trail. They are lossy by design, their shape changes whenever someone edits a log line, they usually contain only the calls that succeeded, and the service being audited can normally delete them.
- Write the record at the policy decision point, not after the call returns. A trail with no denials in it is not evidence that nothing was denied.
- Every field must have an investigation it enables. If you cannot name the query, cut the field. Hash the arguments; never store the credential, the document, or personal data the record does not need.
- Nobody hands you a retention number. NIST writes audit record retention as an organisation-defined period, and a number quoted at you as a framework requirement is almost always somebody's own policy. Pick one, name the two opposing pressures that set it, and be able to defend it.

## Who this is for

You are running agents that write to systems of record — refunds, tickets, code, mailboxes, rows — and at some point a person is going to ask you to reconstruct one of those actions in front of someone who is not on your team. This guide covers the record schema, where to emit it, how to keep it honest, what must never go in it, and how to query it during an actual investigation. Skip it if your agents are read-only against synthetic data in a sandbox: what you need there is tracing, and tracing has different rules — it is allowed to sample, an audit trail is not.

## The problem

Your refund agent pays out the same invoice twice on a Monday morning, and a customer is owed an explanation before they are owed the money back. You have logs. You grep, and you find the line:

```text
2026-08-17T09:14:22Z INFO tool_call name=refund invoice=inv_88213 status=200
```

Read what that line does not contain. It does not say which human's request set the agent going, so you cannot tell whether a support rep asked for this or a scheduled sweep decided it. It does not say what authority the agent was acting under, so you cannot tell whether the refund was inside a delegated limit or outside one nobody had configured. It does not say what the amount was, because someone decided amounts were sensitive and stripped them, and it does not say where the amount came from, so you cannot tell whether the figure was computed from the invoice or lifted out of a customer email that asked for it in plain language.

And there is one thing it does not contain that matters more than any of those. Seconds later the same agent attempted a second, much larger refund on `inv_88300`, and policy refused it. That attempt is nowhere in the log, because the log line is written after a successful API call. The near miss — the one piece of evidence that your controls were doing anything at all — does not exist. When someone asks "could we have stopped it", you have no data, and the honest answer is that you do not know.

The second half of the problem is quieter. That log line lives in the same index the application writes to, under the same service account, and that account has delete permission on the index because someone needed to clear a noisy field last quarter. So even if the line had said everything, it would be a record the audited system can rewrite. That is not an audit trail. That is a diary.

## Step by step

The path below builds one record, appends it to an append-only store, and runs the query an investigation actually runs. Every SQL statement in this guide was executed against PostgreSQL 17.11 in a container — as a non-superuser owner, through a login user holding only the writer role, and read back through a login user holding only the auditor role — and every output shown is real output from those runs. Full runnable sources: `examples/govern-audit/roles.sql`, `schema.sql`, `emit_record.py`, `export.sql`, `investigate.sql`, and `verify_chain.py`. Run `roles.sql` first and separately: it needs `CREATEROLE`, and `schema.sql` deliberately does not.

### 1. Write the question down before you write the schema

Fix this sentence somewhere your team can see it:

> Which human caused this, through which agent, under what authority, and could we have stopped it?

Those four clauses are the whole specification. Each one maps to fields you will need, and — more usefully — each one is a test that kills fields you do not. A field that does not help answer one of the four clauses is a field you are storing, retaining, securing, and eventually explaining to a regulator for no reason.

Grade the logging you have today against it right now. In most systems the first clause fails outright, because the request that started the run and the tool call the agent made are joined by nothing. The second usually passes by accident, because the service name is in the log prefix. The third almost always fails, because authority was checked in a different service that logged its own decision somewhere else with a different request id. The fourth fails whenever denials are not recorded, which is nearly always.

One distinction to get right at the start, because it decides several things later. An observability trace and an audit record describe the same event and are not the same object. A trace answers "why was this slow" and is allowed to drop the overwhelming majority of its spans to control cost; that sampling is a feature. An audit record answers "who authorised this" and must not sample, must not be dropped under back pressure, and must not be deleted when the retention policy on your metrics backend fires. They also have opposite access-control needs: traces are read by everyone on call, audit records are read by a small named group and their reads are themselves logged. Build them as two sinks, even if one library emits both.

### 2. Define the record, field by field

NIST SP 800-53 control AU-3 states the baseline for any audit record: it must contain information establishing what type of event occurred, when the event occurred, where the event occurred, the source of the event, the outcome of the event, and the "identity of any individuals, subjects, or objects/entities associated with the event". Agent systems strain that last clause, because there are two identities and the interesting one is not the one making the call. Everything below is that baseline plus what delegation forces you to add.

| Field | Why it exists — the investigation it enables |
| --- | --- |
| `record_id` | Answers "which row are we talking about?" when three refunds on one invoice look alike. It is the handle an incident file, a legal hold, or a later record uses to name this event rather than "the refund one on Monday". |
| `occurred_at` | When the emitter says it happened. Orders the story. Always UTC, RFC 3339, microsecond precision so two calls in the same millisecond still sort. |
| `recorded_at` | Answers "can I trust this ordering?" Stamped by the store, and — critically — *not writable by the emitter*, which takes a column-level grant rather than a `DEFAULT`. A gap against `occurred_at` means a bad clock or a late replay, and you want to know before you build a timeline on it. |
| `agent_id` | Which non-human principal acted, versioned. Answers "is this agent still deployed, and what else did it do?" |
| `principal_id` | The human whose authority was borrowed. Answers the first clause of the question. Explicit `NULL` for unattended work, never an absent field, because "there was no human" and "we failed to capture the human" are different answers. |
| `principal_type` | Human, service, or scheduled job. Answers "should there have been a human here at all?" — a write attributed to a scheduled job at 3pm on a Tuesday is either normal or the whole incident. |
| `grant_id` | The delegation or consent record that made this permissible. Answers "under what authority", by pointing at a document rather than asking an investigator to infer one. |
| `resource` | What was acted on, as a stable path. Investigations start from the damaged thing far more often than from the verb, so this is the field you index for prefix search. |
| `action` | Answers "was this a read or a write?" — the first triage question on any resource, and the one that decides whether you are writing an incident report or an exposure notice. Paired with `resource` it is also the unit a policy is written against, which is what makes `decision` checkable. |
| `arguments_sha256` | Digest of the complete argument set. Answers "was it *this* argument set?" — you reproduce the candidate from the source system and compare digests, without the log ever holding the values. |
| `arguments_shape` | An allowlisted subset in the clear, everything else described but withheld. Answers most questions without an approval workflow to read raw arguments. |
| `decision` | Allow or deny. Answers the fourth clause. Without it the trail contains only things that happened and none of the things that were stopped. |
| `decision_reason` | Which rule fired, in words. Answers "why was this allowed?" A decision with no reason is unfalsifiable — you cannot tell a correct allow from a policy that allows everything. |
| `policy_version` | Which ruleset evaluated it. Answers "was this allowed under the rules we had then, or the rules we have now?" Without it, every historical decision gets re-judged against today's policy, which is both unfair and wrong. |
| `outcome` | Success, error, or blocked. Separate from `decision`, because an allowed call can still fail and a denied call has no downstream outcome at all. |
| `outcome_detail` | Answers "is this the same failure as the other forty, or a new one?" — the question that separates a flaky dependency from a systematic one. Exception class or error code only, never the exception message, which routinely contains the argument that caused it. |
| `correlation_id` | One id per user-visible task, propagated to every call the task makes. Turns nine unrelated rows into one story, and it is the single field whose absence does the most damage. |
| `parent_span_id` | Position within the task. Answers "what did the agent do immediately before this?", which is the question you ask when the action looks reasonable in isolation. |

One choice in that list looks like a mistake and is not: `principal_id` is nullable. A `NOT NULL` here reads as stricter and behaves worse, because it pushes emitters into writing `"system"` or `"unknown"` into a column an auditor will read as a real principal.

The full DDL is in `examples/govern-audit/schema.sql`. One constraint is worth pulling out, because `decision` and `outcome` are the pair an auditor reads first and the pair a buggy emitter gets wrong:

```sql
-- A denied call cannot have succeeded.
CONSTRAINT agent_action_denied_blocked
    CHECK (decision <> 'deny' OR outcome = 'blocked')
```

Executed against a record claiming both, PostgreSQL refuses it:

```text
ERROR:  new row for relation "agent_action" violates check constraint "agent_action_denied_blocked"
```

That is the store refusing to hold a self-contradictory record, which is a different and better property than a store that holds whatever it is given.

### 3. Decide what must never enter the record

An audit store is read by more people than your production database, retained far longer, and — if you use immutable storage, which step 5 argues you should — may be genuinely undeletable for years. Everything you put in it inherits all three properties. Three categories never go in:

**Credentials.** Not the token, not a truncated prefix of it, not the password, not the signed URL. And not "the header, with the value replaced by asterisks", because that pattern breaks the first time a credential arrives in a field nobody classified as a credential. Store the credential's *identifier* instead — a key id, a grant id, a connection name — which is a lookup handle into a system that can revoke it, and worthless on its own. If you are unsure how tokens leak out of the surrounding machinery in the first place, the [failure pillar](/fail/) collects the patterns.

**Full document contents.** The body of the email, the row the agent read, the file it summarised. A log of what an agent read is not supposed to be a second copy of what it read. Record the resource identifier and the byte count; the source system still has the content, and it has the access controls the content was supposed to have.

**Personal data beyond the purpose.** GDPR Article 5(1)(c) requires personal data to be "adequate, relevant and limited to what is necessary" for the purpose it is processed for. The purpose here is attribution, and attribution needs a stable identifier for the human, not their name, their email body, or their address. Use an internal principal id that resolves to a person through a system you can query, so the audit store holds a pointer and the directory holds the person.

The pattern that satisfies all three is an allowlist per resource type, not a denylist of secret-looking key names. A denylist fails the first time someone names a field `notes` and a customer pastes an API key into it.

```python
SHAPE_ALLOWLIST: dict[str, frozenset[str]] = {
    "billing.refund": frozenset({"invoice_id", "currency", "reason_code"}),
}


def split_arguments(resource, arguments):
    digest = sha256_hex(canonical_json(arguments))
    allowed = SHAPE_ALLOWLIST.get(resource, frozenset())
    shape = {}
    for key in sorted(arguments):
        if key in allowed:
            shape[key] = arguments[key]
        else:
            shape[key] = {"redacted": describe(arguments[key])}
    return digest, shape
```

The two halves do different jobs and it is worth being precise about which. `digest` is a **confirmation** primitive: you produce a candidate argument set from the source system, digest it, and see whether it matches. It is not a concealment primitive, and treating it as one is where redaction schemes fail. A digest of a boolean, a four-digit PIN, or an account number drawn from a known range hides nothing — anyone holding the log digests every candidate and compares. Truncating the digest does not fix that; it only adds collisions.

So individual withheld values go through a keyed fingerprint instead, under a key held by whoever operates the redaction boundary rather than by whoever reads the log:

```python
def fingerprint(encoded: bytes) -> tuple[str, str]:
    key = os.environ.get("AUDIT_FINGERPRINT_KEY", "")
    if key:
        return "hmac-sha256", hmac.new(key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
    return "sha256-unkeyed", sha256_hex(encoded)
```

Whoever has the log alone cannot enumerate; whoever has the log and the key can still confirm that two calls carried the same value, which is what makes the field worth recording at all. When the key is absent the record says `sha256-unkeyed` in place of `hmac-sha256`, so an investigator can see which fingerprints are enumerable instead of assuming none are. Applying the digest to the *complete* argument set is acceptable because a whole argument set is rarely guessable; applying it to one low-entropy field is not.

One caveat on canonicalisation, since the digest is only as reproducible as the byte string it is taken over. `json.dumps(..., sort_keys=True, separators=(",", ":"))` is a pragmatic canonical form and is fine when one runtime produces every record. It does not normalise number formatting, so a value that serialises as `1.0` in one language and `1` in another digests differently. If records are emitted from more than one runtime, use an implementation of [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html), the JSON Canonicalization Scheme, and name the implementation in your control documentation so a future investigator can reproduce a digest rather than merely admire it.

### 4. Emit the record at the decision point

The single change that fixes most audit trails is moving the write. If the record is emitted after a successful call, it can only ever describe successes. Wrap the policy evaluation *and* the call, so the record exists in all three outcomes — allowed and worked, allowed and failed, denied and never attempted:

```python
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
    except Exception as error:
        outcome = "error"
        # The class name and nothing else. Exception strings routinely carry
        # the argument that caused them, which is how a redacted field
        # reappears three columns to the right.
        detail = type(error).__name__
```

Three details in that block do real work. `policy()` returns the reason on the allow path as well as the deny path, so `decision_reason` is populated for allows — a trail where only denials explain themselves cannot distinguish a correct allow from a policy that allows everything. The bare `except Exception` is deliberate: the record has to survive any failure of the call, including the ones you did not anticipate, because an unexpected exception is exactly when you will want the record. And `detail` is the exception *class*, never `str(error)`, for the reason the comment gives: a database driver will happily put the offending value into its message, and step 3 is undone by a column you did not think of as an argument.

Commit the audit write separately from the business transaction. Sharing a transaction means a rollback erases the evidence that the work was attempted, and an attempted-then-rolled-back write is a case you very much want a record of.

Running `examples/govern-audit/emit_record.py` produces one record per line. Here is the second one — the denial on `inv_88300`, the record that did not exist at all in the log from "The problem" — abbreviated to the fields that carry the answer, with the argument values withheld and fingerprinted. Run it exactly as written and `fingerprint_alg` reads `sha256-unkeyed`; the `hmac-sha256` below is what you get once `AUDIT_FINGERPRINT_KEY` is set, which is the only configuration you should ship:

```json
{
  "action": "write",
  "agent_id": "agent:billing-triage@v7",
  "arguments_sha256": "68f630bff167ae8feaf7d1c7c921b561a7c14cb012df0b07de5bfd4d90030492",
  "arguments_shape": {
    "amount_cents": {"redacted": {"bytes": 6, "fingerprint_alg": "hmac-sha256", "type": "int"}},
    "currency": "USD",
    "invoice_id": "inv_88300",
    "reason_code": "goodwill"
  },
  "chain_id": "chain:emitter-1",
  "chain_seq": 2,
  "correlation_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "decision": "deny",
  "decision_reason": "refund_ceiling: amount_cents exceeds delegated limit 50000",
  "grant_id": "grant:01JQ8Z9RMK",
  "outcome": "blocked",
  "principal_id": "user:ada@example.com",
  "principal_type": "human",
  "untrusted_input": true
}
```

Note the `correlation_id`. It is a [W3C Trace Context](https://www.w3.org/TR/trace-context/) trace-id — the 32-hex-digit field from the `traceparent` header — reused rather than invented, so the audit record and the distributed trace for the same task join on a value you are already propagating. Trace Context has been a W3C Recommendation since 23 November 2021, so if your services already propagate `traceparent` you already have a correlation id and do not need to invent a second one. Reuse the trace-id, keep the two sinks separate.

### 5. Make the store append-only and tamper-evident

Four mechanisms, in increasing order of what they buy and what they cost. Pick deliberately; picking all four is a legitimate answer and so is picking the first two.

**Privileges.** The writer role may append and nothing else. The auditor role may read and nothing else. Neither owns the table. Grant `INSERT` **column by column**, and leave `recorded_at` out of the list:

```sql
GRANT INSERT (
    record_id, occurred_at, agent_id, principal_id, principal_type, grant_id,
    resource, action, arguments_sha256, arguments_shape,
    decision, decision_reason, policy_version, outcome, outcome_detail,
    correlation_id, parent_span_id,
    model_id, prompt_version, tool_version, untrusted_input,
    canonical_form, prev_hash, record_hash, chain_id, chain_seq
) ON audit.agent_action TO agent_writer;

GRANT SELECT ON audit.agent_action TO auditor;
```

The column list is the control; `DEFAULT clock_timestamp()` is not. A default says what happens when a column is *not* supplied. It says nothing about who may supply it. Under a table-level `GRANT INSERT` the writer can send its own `recorded_at`, and then both timestamps come from the same untrusted party, agree by construction, and the skew check in step 8 cheerfully endorses a fabricated timeline. Executed as a member of `agent_writer`:

```text
-- INSERT naming recorded_at
ERROR:  permission denied for table agent_action
-- the same INSERT with recorded_at omitted
INSERT 0 1
```

*Buys:* the application cannot rewrite its own history, and cannot tell the store when things happened. *Costs:* almost nothing at runtime, plus one ongoing chore — adding a column means adding it to this grant, or every append starts failing. *Does not buy:* protection from anyone holding the owner or superuser role.

Note what is *not* in that block: a `REVOKE UPDATE, DELETE, TRUNCATE`. PostgreSQL grants no table privileges to `PUBLIC`, so these roles never held them, and the revoke would be a no-op that reads like a control. Check the real state with `SELECT relacl FROM pg_class WHERE relname = 'agent_action'` rather than trusting a line that only looks defensive.

**A trigger.** Defence in depth against a careless script run as the owner:

```sql
CREATE TRIGGER agent_action_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.agent_action
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
```

Executed as the table owner, both statements fail:

```text
ERROR:  audit.agent_action is append-only (attempted UPDATE)
ERROR:  audit.agent_action is append-only (attempted TRUNCATE)
```

*Buys:* accidents fail loudly instead of silently succeeding. *Costs:* a migration that legitimately needs to rewrite the table now needs an explicit, reviewable step to drop and recreate the trigger — which is a feature. *Does not buy:* anything against an attacker, since the owner can `DROP TRIGGER` and a superuser can `ALTER TABLE ... DISABLE TRIGGER`. Say so in your control documentation rather than letting a reader assume otherwise.

**Hash chaining.** Each record carries a hash over all of its own fields plus the previous record's hash. Two things about building one are not obvious, and both bite in production rather than in a demo.

*Store the bytes you hashed.* The natural implementation hashes a canonical serialisation of the record, writes the columns, and later re-derives that serialisation from the columns in order to verify. It does not work, because a database is not a byte store — it is a value store, and it renders values on the way out. PostgreSQL prints `timestamptz` its own way:

```text
in   2026-08-18T22:18:41.979183+00:00  ->  out  2026-08-18 22:18:41.979183+00
in   2026-08-18T22:18:41.979180+00:00  ->  out  2026-08-18 22:18:41.97918+00
in   2026-08-18T22:18:41.000000+00:00  ->  out  2026-08-18 22:18:41+00
```

Look closely at what changed there, because it is not only the trailing zeros. The `T` became a space and `+00:00` became `+00`, so re-serialising fails on **every** record — not on some unlucky tenth whose microseconds happen to end in a zero. A verifier built this way reports tampering against a perfectly healthy store, on a schedule, forever, and an integrity control that cries wolf gets switched off, after which you have neither the control nor the alarm.

So the schema carries `canonical_form text NOT NULL`, holding the exact string that was hashed, and `examples/govern-audit/export.sql` emits it verbatim instead of rebuilding it. The redundancy against the typed columns is the point: those columns are for querying, this one is the evidence.

That split creates a gap of its own, and it is worth closing properly rather than approximately. If the hash only covers `canonical_form`, then editing a *queryable* column — the copy an investigator actually reads — changes nothing the hash can see. So the exporter re-renders every column back into the emitter's spelling (`to_char` for `occurred_at`, `encode` for the `bytea` columns, `::text` for the enums) and the verifier compares all of them. That is 24 of the table's 27 columns; the other three are `record_hash`, which is checked by hashing, `canonical_form`, which is the thing being compared against, and `recorded_at`, which the emitter never sees and so was never hashed — that one is protected by the column-level grant above, not by the chain. Resist the temptation to cross-check a convenient subset: the first version of this checked nine columns of plain text and skipped `decision_reason`, `grant_id`, `untrusted_input`, and `occurred_at`, every one of which is printed by the investigation query in step 8.

*A chain has one head, so it admits one writer.* Two emitters that read the same head compute the same `prev_hash`, and the fork they produce is indistinguishable from tampering when the verifier runs. There are two ways out and you must pick one: serialise every writer on a chain behind a lock, or give each emitter its own chain and anchor each head separately. The schema does both — one chain per emitter, with the head handed out by a `SECURITY DEFINER` function under a row lock held until the writer commits, so two processes that accidentally share a chain id still cannot fork it. That function earns its keep twice over: the writer holds no `SELECT` anywhere in the schema, so it cannot read the chain head it needs, and the function hands it that one value and nothing else.

A `SECURITY DEFINER` function is a hole punched through the privilege system on purpose, so close it behind you. Two lines, in this order:

```sql
REVOKE EXECUTE ON FUNCTION audit.next_link(text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION audit.next_link(text) TO agent_writer;
```

Without the `REVOKE`, the `GRANT` is decorative — PostgreSQL grants `EXECUTE` on every new function to `PUBLIC`, so the read-only auditor role can call it too, creating chains and taking locks through a definer function while its direct `INSERT` is refused. Check it rather than assuming it, with `SELECT proname, proacl FROM pg_proc WHERE proname = 'next_link'`; a bare `=X/` entry is `PUBLIC`. Afterwards the auditor gets the answer it should:

```text
ERROR:  permission denied for function next_link
```

Scope the parameter too. `p_chain_id` is caller-supplied, so a function that locks and reads whatever chain it is handed lets one writer read another's head and stall it by holding its row lock. The schema records an `owner` on each chain — `session_user`, because `current_user` inside a definer function is the function's owner — and refuses anything else:

```text
ERROR:  chain chain:emitter-1 is not owned by other_svc
```

The lock does what locks do. Two writers on one chain, the first sleeping 1.5 seconds before committing:

```text
('agent:A', 'got_link',  0.0,  1)
('agent:A', 'committed', 1.51, 1)
('agent:B', 'got_link',  1.32, 2)   <- blocked until A committed, then got seq 2
('agent:B', 'committed', 1.32, 2)
```

A `BEFORE INSERT` trigger rejects any record that does not continue its chain, so a fork becomes a failed write rather than a verifier alert raised weeks later against records nobody can still explain:

```text
ERROR:  chain chain:normal forked: record claims prev_hash <NULL>, head is a1fce436...
ERROR:  chain chain:normal out of order: record claims seq 7, expected 2
```

With that in place, `examples/govern-audit/verify_chain.py` walks an export. Tested against a record whose hashed bytes were edited, an export with the first record removed, and a queryable column edited while `canonical_form` was left intact:

```text
47771d95-...: record_hash does not cover canonical_form
2007dff2-...: prev_hash '7439518d...' does not match the previous record_hash None
              in chain 'chain:emitter-1' -- a record was removed, reordered, or inserted
47771d95-...: column principal_id='user:someone-else@example.com' disagrees with
              canonical_form 'user:ada@example.com'
```

*Buys:* detection of any edited field, any record removed from the middle, and any divergence between the hashed bytes and the 24 exported columns — using only the data itself, no trusted third party, no special storage. *Costs:* a hash per write, a lock per chain, one duplicated column, and the discipline to actually run the verifier, since an unverified chain proves nothing. *Does not buy:* three things, and the third is the one people miss.

Truncation is undetectable — lopping off the last N records leaves a chain that verifies perfectly. An attacker who can modify records can recompute every hash from the edit forward. And, the important one, **anyone who can append can forge**. A well-formed record chained to the genuine head, describing an approval that never happened, verifies clean; appending is precisely what that role is permitted to do. Demonstrated against this schema as `agent_writer`, with a `decision_reason` of "FABRICATED: this call never happened", the verifier exits 0 and prints "chain intact". The role that can do this is the application — the component under audit. That is not a flaw in the chain. It is the structural reason the emitter should not be the thing it describes, which is the argument the scale section below picks up.

The first two holes close the same way, and it is worth doing because it is cheap: publish the head hash and the record count it covers, on a fixed cadence, to a system under different credentials. Then a truncation is a head hash you cannot reproduce rather than a silence. That is the shape of the construction certificate transparency uses — [RFC 9162](https://www.rfc-editor.org/rfc/rfc9162.html) describes the append-only property of a log as achieved with Merkle trees, "which can be used to efficiently prove that any particular instance of the log is a superset of any particular previous instance", with signed tree heads gossiped so no single operator can quietly rewrite history. You do not need a Merkle tree to get the useful half. You need the anchor. The third hole does not close with cryptography at all.

**Immutable storage.** Object storage with a retention lock is the strongest option and has the sharpest cost. In Amazon S3 Object Lock, a compliance-mode object "can't be overwritten or deleted by any user, including the root user in your AWS account", its retention mode cannot be changed and its period cannot be shortened; AWS notes that "the only way to delete an object under the compliance mode before its retention date expires is to delete the associated AWS account". Governance mode is the softer variant, overridable by a caller holding `s3:BypassGovernanceRetention` who sends `x-amz-bypass-governance-retention:true`.

*Buys:* the property nothing else gives you — that no insider, at any privilege level, can remove the record. *Costs:* exactly the same property, pointed at you. A mis-redaction is permanent. A record you are later obliged to delete cannot be deleted until retention expires. Storage is paid for the full period regardless of whether anyone ever reads it. Price that before choosing compliance mode over governance mode, and default to governance mode unless you have a specific reason not to.

Object Lock also requires S3 Versioning, and the interaction between the two produces a delete that appears to succeed — see the last failure mode before you test your protection.

**Separation of duties** cuts across all four and is the one people skip because it is organisational rather than technical. NIST SP 800-53 expresses it as two enhancements on AU-9: authorising access to management of audit logging functionality to only an organisation-defined subset of privileged users or roles, and separately authorising read-only access to audit information for those who only need to review it. The concrete version: whoever operates the agent platform must not hold the credentials that can alter the audit store, and every read of the audit store is itself recorded. *Buys:* the audited party cannot quiet the record. *Costs:* a real one, worth stating plainly — at 3am the person best placed to fix a broken audit pipeline is now the person who is not allowed to touch it. Staff and rota for that before you enforce it, or it gets bypassed the first time it hurts.

### 6. Add the model-specific fields

Four more columns exist only because the acting component is a model. Each one turns a multi-day investigation into a query.

| Field | The investigation it pays off in |
| --- | --- |
| `prompt_version` | The behaviour changed and nobody deployed code. `GROUP BY prompt_version` over the anomalous window shows whether the change tracks a prompt edit. Without it you are diffing a prompt repository against incident timestamps by eye, and prompts are edited far more often than code is deployed. |
| `tool_version` | The tool's argument schema changed under you — a field that meant cents now means dollars, an enum gained a value. Replaying old records against today's schema then produces a confidently wrong reading. This column tells you which schema to interpret each record under. |
| `model_id` | "Nothing changed on our side" is the sentence you need to be able to check. Pin the fully qualified identifier, including the dated snapshot suffix, so a provider-side change is visible as a change in your own data rather than as an unexplained shift in behaviour. |
| `untrusted_input` | The injection triage. When a page of content arrives from a customer email, a web fetch, or a shared document, everything downstream is suspect. The query that matters is denials where this is true — those are your controls catching what the content asked for — and successful *writes* where it is true, which is the list you actually have to review by hand. |

`untrusted_input` deserves the caveat, because it is the field most likely to be quietly wrong. It is your own taint classification, not an observed fact, and it is only as good as your propagation: content that arrives clean, gets summarised, and is passed to a second tool call is still tainted, and a naive implementation marks the second call `false`. A false `false` is worse than no column at all, because it will be trusted. So default it to `true` and require an emitter to prove otherwise, which is why the DDL reads `NOT NULL DEFAULT true`. An emitter that forgets the field produces a record that over-reports exposure and gets reviewed, rather than one that quietly asserts the input was clean.

### 7. Index for the query you will actually run

An audit trail nobody can query in an emergency is a backup. The real query is "everything agent X did for user Y between T1 and T2", so the index leads with the two equality columns and ends with the range column:

```sql
CREATE INDEX agent_action_agent_principal_time
    ON audit.agent_action (agent_id, principal_id, occurred_at DESC);
```

Column order is not a detail. On a 200,000-row table in the container used for this guide, the same query returning the same 96 rows planned as an index scan reading 103 buffers in 0.37 ms with the index above, and 619 buffers in 2.96 ms against an otherwise identical index ordered `(occurred_at, agent_id, principal_id)` — the time-first index cannot use either equality column to narrow the scan, so it walks the whole day. Those are figures from one container on one machine. What transfers is the shape, not the multiple: an index whose leading column is the range cannot use either equality column to narrow the scan, so it reads a whole time slice and discards most of it. How much that costs you depends on row width, selectivity, and physical correlation — measure it on your own data with `EXPLAIN (ANALYZE, BUFFERS)` rather than trusting a ratio from someone else's.

Three more indexes earn their keep, and each maps to a question you will be asked:

```sql
-- "Reconstruct this one task." Without it, a chain walk is a sequential scan.
CREATE INDEX agent_action_correlation
    ON audit.agent_action (correlation_id, occurred_at);

-- "What touched this resource?" Prefix matching on a resource path needs
-- text_pattern_ops to use the index under a non-C collation.
CREATE INDEX agent_action_resource_time
    ON audit.agent_action (resource text_pattern_ops, occurred_at DESC);

-- "Show me the denials." Partial, so the index stays tiny.
CREATE INDEX agent_action_denials
    ON audit.agent_action (occurred_at DESC)
    WHERE decision = 'deny';
```

### 8. Run the investigation

Run it with the auditor privileges and never the writer's — as a named human who is a member of the `auditor` role, adopting it with `SET ROLE auditor`. The role itself is `NOLOGIN` in `roles.sql` on purpose: reads of an audit store are themselves audit events, and you cannot log who read what if everyone shares one login.

```sql
SELECT occurred_at, correlation_id, resource, action,
       decision, outcome, decision_reason, grant_id, untrusted_input,
       encode(arguments_sha256, 'hex') AS args_digest
  FROM audit.agent_action
 WHERE agent_id     = :agent
   AND principal_id = :principal
   AND occurred_at >= :from::timestamptz
   AND occurred_at  < :to::timestamptz
 ORDER BY occurred_at;
```

Real output from the run described above, abbreviated to the columns that carry the answer:

```text
          occurred_at          |    resource    | decision | outcome | decision_reason
-------------------------------+----------------+----------+---------+---------------------------------
 2026-08-18 23:01:17.835207+00 | billing.refund | allow    | success | grant:refunds.write matched, ...
 2026-08-18 23:01:17.840033+00 | billing.refund | deny     | blocked | refund_ceiling: amount_cents ...
(2 rows)
```

Two rows, and the second one is the whole reason the trail exists. It answers the fourth clause with evidence rather than assertion: yes, a control was evaluated, and here is the rule that fired.

Run one more query before you write anything down, because it decides whether the ordering above is trustworthy at all:

```sql
SELECT agent_id,
       count(*)                                         AS records,
       max(recorded_at - occurred_at)                   AS worst_lag,
       count(*) FILTER (WHERE occurred_at > recorded_at) AS from_the_future
  FROM audit.agent_action
 WHERE occurred_at >= :from::timestamptz
   AND occurred_at  < :to::timestamptz
 GROUP BY agent_id;
```

`occurred_at` is claimed by the emitter; `recorded_at` is stamped by the server. This query is only worth running because of the column-level grant in step 5 — under a table-level `GRANT INSERT` the writer supplies both values, they agree by construction, and a compromised emitter passes this check while lying about every timestamp in the window. Given the grant, any row in `from_the_future`, or a `worst_lag` measured in minutes, means the timeline you were about to build is not sound. Discover that here, not in cross-examination.

### 9. Decide retention on purpose

Retention is the field everyone leaves blank and then defaults to forever, which is a decision made by not making one. Two pressures oppose each other, and your number is where they meet.

**Pressure toward keeping longer.** Investigations are retrospective by definition, and you do not control when one starts. Some agent misbehaviour is only visible as a pattern across a long window — a slow drift in what it approves, a resource it touched once in March that turns out to matter in September — so a window shorter than the pattern hides it completely. Disputes, contractual claims, and regulatory questions arrive on their own schedule rather than yours. A trail that begins after the incident is not a trail.

**Pressure toward keeping less.** Every record is personal data about the human it names, and GDPR Article 5(1)(e) requires personal data to be kept in a form permitting identification of data subjects "for no longer than is necessary for the purposes for which the personal data are processed". Every record you hold is a record that can be breached, subpoenaed, or read by someone who did not need it. Storage costs money for the full period whether or not anyone reads it. And if you chose compliance-mode immutable storage, you cannot shorten the period after the fact — the choice is made once.

How to reason about it rather than picking a round number: start from the longest realistic detection-to-investigation gap for the systems these agents touch, since a retention period shorter than that guarantees your worst incident is the one you cannot reconstruct. Then check that number against any sector rule that genuinely applies to you — financial recordkeeping and health records carry real statutory periods; general-purpose SaaS usually does not. Then tier it: full records including `arguments_shape` for the shorter window, and a reduced record — timestamps, both principals, resource, action, decision, outcome — for the longer one, which is enough to answer the four clauses at a fraction of the exposure.

Do not expect the number to come from a certification. NIST SP 800-53 writes the control as retaining audit records for "[Assignment: organization-defined time period **consistent with records retention policy**]". The blank is the point, and so is what it points at: your own retention policy. Where a framework does hand you a number it will be a sector rule rather than a general security standard — check whether one applies to you, and cite it if it does. The failure mode below covers what to do when somebody insists otherwise.

One thing to settle with counsel before it comes up rather than during: an erasure request does not automatically empty your audit trail. GDPR Article 17(3) lists grounds on which the right to erasure does not apply, including compliance with a legal obligation and "the establishment, exercise or defence of legal claims". Whether your particular records fall under one of those is a legal question about your specific circumstances, not a technical one, and this guide is not the place it gets answered. What *is* your job is making the question answerable: know which fields in a record identify a person, and be able to produce every record naming a given principal — which the index in step 7 already gives you.

## Decision table

| Option | Completeness | Tamper resistance | Effort | When it wins |
| --- | --- | --- | --- | --- |
| Application logs | Poor. Only what a developer remembered to log, only on paths that ran, usually only successes. Shape drifts silently as lines are edited. | Poor. Normally writable and deletable by the service being audited, under a retention policy set for cost. | None — you already have them. | Debugging, and nothing else. As an audit trail this is the option that looks free and is not. |
| Dedicated append-only audit store | Good, but it is on you. Completeness equals emitter discipline: every agent, every path, every denial, forever, including the one a contractor ships next quarter. | Good. Privileges, triggers, hash chaining, and a retention lock are all available and all under your control. | High and permanent: schema, migrations, indexes, redaction rules, chain verification, retention tiering, separation of duties. | You have a small number of agents, strong engineering discipline, and a specific requirement that forbids records leaving your infrastructure. |
| Control layer emits the records | Best available. Records come from the layer the calls route through, so an agent cannot forget, and denials are recorded because the layer is what denies. | Good, and structurally better: the emitter is not the audited component, which is separation of duties by construction rather than by policy. | Low per agent, but you are adopting a dependency in the path of every call and inheriting its retention, export, and access model. | Many agents, many tools, or any environment where you cannot personally review every agent's logging code before it ships. |

The honest summary: the first column is not a real option, and the choice between the second and third is only ever about who maintains the emitters.

## Checklist

- [ ] You can state, in one sentence, which human caused an action, through which agent, under what authority, and whether a control evaluated it.
- [ ] Every field in the record has a written investigation it enables; anything without one has been deleted.
- [ ] Denied actions produce records. Verify by triggering a denial in staging and finding the row.
- [ ] The record is written around the call, so an exception mid-call still produces a record with `outcome = 'error'`.
- [ ] `decision` and `outcome` are separate fields, and a `deny` with a successful outcome is rejected by a constraint.
- [ ] `decision_reason` is populated on allows, not only on denials.
- [ ] `principal_id` is nullable and nothing writes `"unknown"` or `"system"` into it.
- [ ] No credential, in any form, appears in any field — checked by grepping a real export, not by reading the code.
- [ ] `outcome_detail` carries the exception class, never `str(error)`.
- [ ] Argument values are digested or fingerprinted, and low-entropy fields use a key held outside the audit store.
- [ ] The writer's `INSERT` is granted column by column and omits `recorded_at`; an insert naming it fails with `permission denied`.
- [ ] The service account running agents cannot `UPDATE`, `DELETE`, or `TRUNCATE` the audit table — confirmed against `pg_class.relacl`, not against a `REVOKE` statement that may be a no-op.
- [ ] The exact bytes that were hashed are stored, and the verifier reads those bytes rather than re-deriving them from typed columns.
- [ ] The verifier cross-checks **every** column against those bytes, not a convenient subset of the plain-text ones — confirmed by editing a column it does not obviously cover, such as `decision_reason` or `occurred_at`, and watching it fail.
- [ ] Any `SECURITY DEFINER` function has `EXECUTE` revoked from `PUBLIC`; verified with `SELECT proname, proacl FROM pg_proc`, since PostgreSQL grants it to `PUBLIC` on creation and a later `GRANT` to your writer changes nothing.
- [ ] The chain is verified against an export from the **store**, not only against the emitter's stdout — those are different code paths and only one of them is what you will have during an incident.
- [ ] Writers on a single chain are serialised, or each emitter owns a chain and each chain's head is anchored separately.
- [ ] The hash chain is verified on a schedule, and the head hash is anchored somewhere the store's operator does not control.
- [ ] Your control documentation states that the chain cannot detect a false record appended by the writer, and names what does address that instead.
- [ ] Audit writes commit in their own transaction, not the business transaction.
- [ ] Timestamps are UTC and RFC 3339, and you run the emitter-versus-server skew query before trusting any timeline.
- [ ] The correlation id is propagated to every downstream call, including calls into other services.
- [ ] `prompt_version`, `tool_version`, and `model_id` are recorded, and `untrusted_input` defaults to true.
- [ ] The investigation query is an index scan, confirmed with `EXPLAIN (ANALYZE, BUFFERS)` against production-scale data.
- [ ] A retention period is written down, with the two pressures that set it and a named owner.
- [ ] Reads of the audit store are themselves logged, and the people who operate the agents are not the people who can alter it.
- [ ] Every compliance claim in your own documentation names the criterion or regulation it comes from, or is stated in general terms.

## Failure modes

### The trail says what happened but not who it was for

**Symptom:** you can list every action an agent took and cannot attribute a single one to a person. The postmortem stalls on "on whose behalf".

**Cause:** the agent authenticates as itself — one service account, one token — and the human who triggered the run is a detail of the request that was never carried into the tool call. This is the default outcome of every framework that hands an agent a credential and lets it call tools directly.

**Fix:** make the delegated principal a required argument on the tool-call path, so an unattributed call is a type error rather than a `NULL`. Carry it from the request that started the run through every hop, alongside the correlation id. A single-principal agent will still produce a trail; it just cannot produce evidence.

### Only successes are in the log

**Symptom:** the trail has no denials in it. Someone reads that as a clean record, when it is an empty one.

**Cause:** the record is emitted after the call returns, so refusals and errors never reach the store. See "The problem" for what this costs at the moment it matters.

**Fix:** move the write to wrap policy evaluation, per step 4. Then check the fix the only way that counts — trigger a real denial and query for the row. And add the monitor: a window with zero denials means either a well-behaved agent or a policy that is not evaluating anything, and only the count of denials over time tells you which.

### The redacted field reappears somewhere else in the record

**Symptom:** a scan of the audit store finds the customer's phone number, a bearer token, or a full document body — in `outcome_detail`, or inside a nested `arguments_shape` value, or in a field added six months after the redaction rules were written.

**Cause:** three usual routes. Exception messages: drivers and HTTP clients put the offending value in the message, and `str(error)` copies it in. Nested structures: an allowlist applied to top-level keys does nothing about a permitted key whose value is a dict containing the document. And new fields: the allowlist was written against the tool's arguments as they existed then.

**Fix:** store the exception class only. Apply the allowlist recursively, or refuse to store any allowlisted value that is not a scalar. Add a test that fails when a tool gains an argument key the allowlist does not mention, so new fields are opt-in rather than opt-out. And because immutable storage means you cannot clean this up afterwards, run the grep against a real export before you enable the retention lock, not after.

### The application can delete its own audit records

**Symptom:** nothing, until the day it matters. Then a record you expected is missing and nobody can say when it went.

**Cause:** the audit table lives in the application database, the application connects as owner or superuser, and "append-only" was a convention in a README.

**Fix:** a separate role at minimum — `INSERT` for the writer, `SELECT` for the auditor, ownership held by neither — and ideally a separate database with separate credentials. Then test it: connect as the application's own role and try `UPDATE` and `DELETE`. If they succeed, you do not have an audit trail, whatever the schema is called.

### You cannot sort the records

**Symptom:** two records from different services appear in an order that contradicts the causal story, or a record is timestamped after the incident it caused.

**Cause:** timestamps taken from local clocks, in local time, on hosts that drift. Clock synchronisation appears as a control in its own right alongside logging and monitoring in ISO/IEC 27001:2022 Annex A, and the reason is this one: every other logging control is worth less than you think if the times are not comparable.

**Fix:** UTC everywhere, RFC 3339 everywhere, microsecond precision, `clock_timestamp()` on the server for `recorded_at` — with the column-level grant from step 5, or the server-side value is only a suggestion the writer can override — and the skew query from step 8 run as the first step of every investigation. When skew is real, order by `recorded_at` and say in the report that you did and why.

### The correlation id stops at the first hop

**Symptom:** you can reconstruct what the orchestrator did and lose the thread the moment it calls another service. Records exist on both sides and nothing joins them.

**Cause:** the id is generated per process rather than per task, or it is propagated over HTTP and dropped at a queue boundary, where the header does not survive serialisation.

**Fix:** generate once at the task boundary, reuse the W3C Trace Context trace-id you are already propagating, and treat every asynchronous hop as a place the id must be an explicit field in the message body rather than a header you hope survives. Test it by asserting that a task producing nine records produces nine records with one `correlation_id`.

### "SOC 2 requires an immutable audit log retained for one year"

**Symptom:** a sentence like that one in your own security documentation, a customer questionnaire, or a design doc. Nobody can name where it comes from.

**Cause:** compliance claims propagate by repetition. That sentence has the shape of a fact and is not one. SOC 2 is an attestation engagement against the AICPA's Trust Services Criteria — published as the *2017 Trust Services Criteria (With Revised Points of Focus — 2022)*, which the AICPA describes as "control criteria... for use in attestation or consulting engagements to evaluate and report on controls over the security, availability, processing integrity, confidentiality, or privacy of information and systems". The categories in scope are selected for the engagement, and the criteria are control objectives: your auditor tests the controls *you* describe against them. Which means the specifics — the field list, the storage technology, the retention period — are the ones you wrote. A retention number appearing in a SOC 2 report got there because someone in your company chose it.

The cost of getting this wrong is not pedantic. A reader who repeats an invented requirement in front of an auditor has to retract it, and every other claim in the same document then gets read with suspicion.

**Fix:** for every compliance sentence you write, either name the criterion, control number, or article it comes from and link it, or state the requirement in general terms. If someone quotes a requirement at you, ask which criterion it is and go read that criterion — most of these sentences do not survive the question. "Our auditor tests our logging controls against the criteria in scope, and our own policy sets retention at N months" is defensible, sourced, and no less useful. If you cannot get to the primary text — the Trust Services Criteria are behind an AICPA account, and ISO/IEC 27001's Annex A text is behind ISO's paywall — that is a reason to describe the requirement generally, not a reason to guess at it.

### The delete succeeded and the record is still there — or vice versa

**Symptom:** a cleanup script reports success against an Object-Lock-protected bucket and the objects vanish from listings, or an erasure workflow reports success and the data is still under retention.

**Cause:** S3 Object Lock requires versioning, and a `DELETE` that names no version id returns `200 OK` and inserts a delete marker rather than removing anything. Only a permanent `DELETE` naming a version id returns `403 Forbidden`. Both halves of the confusion follow: the object is intact but invisible to a lister, and the caller believes it is gone.

**Fix:** test the protection with a version-less delete, and assert on what a versioned listing shows rather than on the delete's status code. If a real erasure obligation may reach these records, prefer governance mode — overridable by a caller holding `s3:BypassGovernanceRetention` — over compliance mode, where no user including the account root can remove an object before its retention expires.

## Doing this at scale

Everything above is one agent's worth of work, and it stays correct at one agent. What changes at ten is that completeness stops being a property of the store and becomes a property of every emitter — and emitters decay. The first agent is instrumented by whoever read this guide. The fifth is instrumented by someone who copied the fourth and dropped the denial path because it was harder to test. The eighth is a vendor SDK you do not control. Nothing in your audit store will tell you this happened; a missing record and a record of a thing that did not happen look identical.

That is the structural argument for emitting from the layer the calls route through rather than from each agent. If every tool call crosses one boundary, and that boundary is what evaluates policy, then the record is a by-product of the call rather than a thing someone remembered to write. Denials are recorded because the layer is what denies. Both principals are present because the layer authenticated both. And the emitter is not the audited component, which turns separation of duties from a rota problem into a property of the architecture.

[Agentic Fabriq](https://agenticfabriq.com) is built that way: agents hold a token for the control layer rather than credentials for the systems themselves, policy is evaluated per request, and each action is attributed to an agent and the user it acted for. The investigation from step 8 becomes a query against the layer instead of against your own store — same two principals, same window:

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        agents = await af.list_agents()
        print(f"{len(agents)} registered agents")

        result = await af.invoke_connection(
            "audit_store",
            method="query_actions",
            parameters={
                "agent_id": "agent:billing-triage@v7",
                "principal_id": "user:ada@example.com",
                "from": "2026-08-18T00:00:00Z",
                "to": "2026-08-19T00:00:00Z",
            },
        )
        records = result.get("records", [])
        denied = [r for r in records if r.get("decision") == "deny"]
        print(f"{len(records)} actions, {len(denied)} denied")


asyncio.run(main())
```

The runnable version is `examples/govern-audit/fabriq_investigation.py`. Connection names, method names, and response shapes are per deployment, so run `afctl tools list` against your own gateway rather than trusting `audit_store`, `query_actions`, or the `records` key on faith.

Be clear-eyed about what a control layer does and does not settle. It settles emitter completeness, which is the failure that actually happens. It does not settle retention, redaction rules, or what your compliance documentation is allowed to claim — those are yours either way, and everything in steps 3, 5, and 9 applies unchanged. What you are trading is a maintenance burden for a dependency in the path of every call, and that dependency's own retention and export model becomes a question you have to ask before you adopt it, not after. If you would rather own the emitters, [Agentic Fabriq](https://agenticfabriq.com) is not required for any of this — the schema, the store, and the queries above are the whole thing, and they are yours.

## Further reading

The [governance pillar](/govern/) covers the two guides this one depends on: delegated identity, which is what makes `principal_id` a real value rather than a hopeful string, and least privilege, which is what makes a denial happen at all. An audit record of an agent that could do anything is a very detailed way of learning that it did. Working through a specific integration first often makes the schema concrete — [connecting an agent to Gmail](/connect/gmail/) shows where the two principals come from in an OAuth flow, and [connecting an agent to a database](/connect/databases/) shows the same problem where the resource is a row. The [failure pillar](/fail/) collects what happens when the credential itself leaks, which is the incident your audit trail has to survive being read during.

Primary sources for everything asserted above:

- [NIST SP 800-53 Rev. 5](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final) — AU-3 on the required content of an audit record, AU-9 on protecting audit information and restricting management of the logging function to a subset of privileged users, AU-11 on retention as an organisation-defined period.
- [NIST SP 800-92, Guide to Computer Security Log Management](https://csrc.nist.gov/pubs/sp/800/92/final) — log management as a lifecycle covering generation, storage, protection, analysis, and disposal.
- [2017 Trust Services Criteria (With Revised Points of Focus — 2022)](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022) — the AICPA criteria a SOC 2 engagement is performed against, and the AICPA's own description of what they are for. Requires a free AICPA account.
- [ISO/IEC 27001:2022](https://www.iso.org/standard/27001) — the ISMS standard whose Annex A includes the logging, monitoring, and clock-synchronisation controls referenced above. ISO does not publish the Annex A text freely; buy the standard rather than trusting a summary, this one included.
- [Regulation (EU) 2016/679 (GDPR)](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679) — Article 5(1)(c) data minimisation, Article 5(1)(e) storage limitation, and Article 17(3) on grounds where the right to erasure does not apply.
- [RFC 9162, Certificate Transparency Version 2.0](https://www.rfc-editor.org/rfc/rfc9162.html) — Merkle trees as the mechanism for an append-only log, and signed tree heads as the anchor that makes truncation detectable.
- [RFC 8785, JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html) — a deterministic JSON serialisation so a digest taken in one runtime reproduces in another.
- [W3C Trace Context](https://www.w3.org/TR/trace-context/) — the `traceparent` header and the 32-hex-digit trace-id reused here as the correlation id.
- [Locking objects with S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html) — compliance versus governance retention modes, the versioning requirement, and the delete-marker behaviour of a version-less `DELETE`.
- [PostgreSQL 17 CREATE INDEX](https://www.postgresql.org/docs/17/sql-createindex.html) — partial indexes, and the index definition syntax used throughout.
- [PostgreSQL 17 operator classes](https://www.postgresql.org/docs/17/indexes-opclass.html) — `text_pattern_ops`, and why prefix matching needs it under a non-C collation.
