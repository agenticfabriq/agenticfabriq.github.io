-- Export a chain for verification. Executed against PostgreSQL 17.
--
--   psql -U alice -d auditdb -q -t -A \
--     -v chain="'chain:emitter-1'" -f export.sql > records.jsonl
--   python3 verify_chain.py records.jsonl
--
-- -q -t -A all three matter: quiet, tuples only, unaligned. Without them psql
-- emits command tags, column headers, and padding, and the output is not JSONL.
--
-- Connect as a real login user who is a MEMBER of auditor, then adopt the role.
-- `auditor` is NOLOGIN in roles.sql on purpose -- it is a privilege set, not a
-- shared account -- so `psql -U auditor` fails with "role is not permitted to
-- log in". Membership plus SET ROLE keeps the connection attributable to a
-- named human while the privileges stay in one place.
--
-- The important line here is `canonical_form`. It is emitted verbatim, exactly
-- as the emitter hashed it, and NOT reconstructed from the typed columns. That
-- reconstruction is the obvious thing to write and it does not work:
-- PostgreSQL renders `timestamptz` with trailing zeros stripped, so a record
-- emitted at ...:41.979183+00:00 comes back as ...:41.979183+00 -- a space
-- instead of the T, a two-digit offset instead of +00:00, and trailing zeros
-- stripped on top of that. Re-derivation therefore fails on EVERY record, not
-- on some unlucky tenth of them, and the verifier reports tampering against a
-- healthy store forever. An integrity control that cries wolf gets switched
-- off, and then you have neither the control nor the alert.
--
-- The `columns` object is the cross-check: it carries the queryable columns so
-- the verifier can confirm they still agree with the bytes that were hashed.
-- Editing a column an investigator reads, while leaving canonical_form alone,
-- is otherwise a silent change.
--
-- Every column that appears in canonical_form is listed below, re-rendered into
-- the spelling the emitter used -- occurred_at through to_char, bytea through
-- encode, enums cast to text -- so the verifier compares like with like rather
-- than skipping the awkward types. A partial cross-check invites exactly the
-- claim this guide warns against: saying a control covers what an investigator
-- reads when it covers nine columns of the table's twenty-seven.
--
-- One column is necessarily absent: recorded_at. The emitter never sees it, so
-- it is not in canonical_form and cannot be cross-checked against it. That is
-- the point of recorded_at -- it is the store's word, not the emitter's -- and
-- it is why the writer has no INSERT privilege on it.

SET ROLE auditor;

SELECT jsonb_build_object(
           'canonical_form', canonical_form,
           'record_hash',    encode(record_hash, 'hex'),
           'columns', jsonb_build_object(
               'record_id',        record_id,
               -- Rebuild the emitter's exact spelling: RFC 3339, T separator,
               -- six fractional digits, +00:00. `::text` gives none of those.
               'occurred_at',      to_char(occurred_at AT TIME ZONE 'UTC',
                                           'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
               'agent_id',         agent_id,
               'principal_id',     principal_id,
               'principal_type',   principal_type,
               'grant_id',         grant_id,
               'resource',         resource,
               'action',           action,
               'arguments_sha256', encode(arguments_sha256, 'hex'),
               'arguments_shape',  arguments_shape,
               'decision',         decision::text,
               'decision_reason',  decision_reason,
               'policy_version',   policy_version,
               'outcome',          outcome::text,
               'outcome_detail',   outcome_detail,
               'correlation_id',   correlation_id,
               'parent_span_id',   parent_span_id,
               'model_id',         model_id,
               'prompt_version',   prompt_version,
               'tool_version',     tool_version,
               'untrusted_input',  untrusted_input,
               'chain_id',         chain_id,
               'chain_seq',        chain_seq,
               'prev_hash',        CASE WHEN prev_hash IS NULL
                                        THEN NULL ELSE encode(prev_hash, 'hex') END
           )
       )::text
  FROM audit.agent_action
 WHERE chain_id = :chain
 -- chain_seq, never a timestamp. The chain's order is the order it was written
 -- in, which is the only order its hashes are valid under.
 ORDER BY chain_seq;
