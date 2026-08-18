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
-- emitted at ...:41.979180+00:00 comes back as ...:41.97918+00:00, rehashes to
-- a different value, and the verifier reports tampering on roughly one record
-- in ten. An integrity control that cries wolf gets switched off, and then you
-- have neither the control nor the alert.
--
-- The `columns` object is the cross-check: it carries the queryable columns so
-- the verifier can confirm they still agree with the bytes that were hashed.
-- Editing a column an investigator reads, while leaving canonical_form alone,
-- is otherwise a silent change.

SET ROLE auditor;

SELECT jsonb_build_object(
           'canonical_form', canonical_form,
           'record_hash',    encode(record_hash, 'hex'),
           'columns', jsonb_build_object(
               'record_id',    record_id,
               'agent_id',     agent_id,
               'principal_id', principal_id,
               'resource',     resource,
               'action',       action,
               'decision',     decision::text,
               'outcome',      outcome::text,
               'chain_id',     chain_id,
               'chain_seq',    chain_seq
           )
       )::text
  FROM audit.agent_action
 WHERE chain_id = :chain
 -- chain_seq, never a timestamp. The chain's order is the order it was written
 -- in, which is the only order its hashes are valid under.
 ORDER BY chain_seq;
