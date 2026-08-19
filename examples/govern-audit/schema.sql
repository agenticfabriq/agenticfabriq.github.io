-- Audit store for agent actions. Executed against PostgreSQL 17.
--
-- Run roles.sql FIRST, as a DBA -- it needs CREATEROLE and this file does not:
--   psql -v ON_ERROR_STOP=1 -f roles.sql
--   psql -v ON_ERROR_STOP=1 -f schema.sql
--
-- Every column below exists because some investigation needs it. If you add a
-- column, write down the question it answers first; if you cannot, drop it.

CREATE SCHEMA IF NOT EXISTS audit;

CREATE TYPE audit.decision AS ENUM ('allow', 'deny');
CREATE TYPE audit.outcome  AS ENUM ('success', 'error', 'blocked');

CREATE TABLE audit.agent_action (
    -- Identity of the record itself, so an incident file, a legal hold, or a
    -- later record can name this exact event. Answers "which row are we
    -- talking about" when three refunds on one invoice look alike.
    record_id        uuid        PRIMARY KEY,

    -- WHEN. Two timestamps, deliberately.
    -- occurred_at is what the emitter claims. recorded_at is stamped by this
    -- server and CANNOT be supplied by the writer: the INSERT privilege below
    -- is granted column by column and omits it. A DEFAULT alone would not do
    -- that -- a default is what happens when a column is not supplied, not a
    -- rule about who may supply it. That distinction is the whole reason the
    -- skew check in investigate.sql is worth running: without it, both
    -- timestamps come from the same untrusted party and agree by construction.
    occurred_at      timestamptz NOT NULL,
    recorded_at      timestamptz NOT NULL DEFAULT clock_timestamp(),

    -- WHO ACTED. The non-human principal.
    agent_id         text        NOT NULL,

    -- WHO IT ACTED FOR. The human whose authority was borrowed. NULL is legal
    -- and means "no delegated human" -- a scheduled job. It has to be an
    -- explicit NULL rather than an absent column, because "there was no human"
    -- and "we did not capture the human" are different answers to an auditor.
    principal_id     text,
    principal_type   text        NOT NULL,

    -- UNDER WHAT AUTHORITY. The grant that made the action permissible, so an
    -- investigator can pull the consent record instead of inferring it.
    grant_id         text,

    -- WHAT. Resource first, action second: investigations start from the
    -- damaged thing far more often than from the verb.
    resource         text        NOT NULL,
    -- Answers "was this a read or a write?" -- the first triage question on any
    -- resource, and the one that decides whether you are writing an incident
    -- report or an exposure notice.
    action           text        NOT NULL,

    -- WITH WHAT ARGUMENTS. The digest is the evidence; the shape is the
    -- convenience. See emit_record.py for how the split is made.
    arguments_sha256 bytea       NOT NULL,
    arguments_shape  jsonb       NOT NULL,

    -- WAS IT ALLOWED, AND WHY. Written at the decision point, before the call,
    -- so that denials exist in the log at all.
    decision         audit.decision NOT NULL,
    decision_reason  text        NOT NULL,
    policy_version   text        NOT NULL,

    -- WHAT HAPPENED. Separate from the decision: an allowed call can still
    -- fail, and a denied call has no downstream outcome to report.
    outcome          audit.outcome NOT NULL,
    -- Answers "is this the same failure as the other forty, or a new one?"
    -- Exception class or error code only -- never the exception message.
    outcome_detail   text,

    -- THE CHAIN OF CAUSATION. One id per user-visible task, propagated to every
    -- call that task makes. This is what turns nine unrelated rows into one
    -- story.
    correlation_id   text        NOT NULL,
    parent_span_id   text,

    -- MODEL-SPECIFIC. Each of these answers "was this a bug or an attack".
    model_id         text,
    prompt_version   text,
    tool_version     text,
    -- Defaults to true, not false. This column is your own taint classification
    -- and it is only as good as your propagation. An emitter that forgets to
    -- set it should produce a record that over-reports exposure and gets
    -- reviewed, not one that quietly asserts the input was clean.
    untrusted_input  boolean     NOT NULL DEFAULT true,

    -- INTEGRITY.
    --
    -- canonical_form holds the exact byte string that was hashed. It duplicates
    -- the columns above, and that redundancy is the point: verification must not
    -- depend on every column surviving a round trip through PostgreSQL's type
    -- rendering. It does not, and not marginally: `timestamptz` output uses a
    -- space separator and a two-digit offset, and strips trailing zeros, so
    --   2026-08-18T22:18:41.979183+00:00  reads back as
    --   2026-08-18 22:18:41.979183+00
    -- Re-deriving the hashed string from the columns therefore fails on EVERY
    -- record, not on the subset whose microseconds end in a zero. Hash the
    -- bytes, store the bytes, verify the bytes.
    --
    -- The typed columns above are for querying. This column is the evidence.
    -- export.sql re-renders every other column into its canonical spelling so
    -- verify_chain.py can compare all of them against these bytes, so a
    -- store whose queryable columns were edited while canonical_form was left
    -- alone is still caught.
    canonical_form   text        NOT NULL,
    prev_hash        bytea,
    record_hash      bytea       NOT NULL,

    -- WHICH CHAIN. A hash chain has exactly one head, so it admits exactly one
    -- writer at a time. Rather than serialising every emitter in the fleet
    -- behind a single lock, each emitter owns a chain and chains advance
    -- independently. chain_seq is the position within that chain, which gives
    -- the verifier a total order that does not depend on any clock.
    chain_id         text        NOT NULL,
    chain_seq        bigint      NOT NULL,

    CONSTRAINT agent_action_hash_len CHECK (octet_length(record_hash) = 32),
    CONSTRAINT agent_action_prev_len CHECK (prev_hash IS NULL OR octet_length(prev_hash) = 32),
    CONSTRAINT agent_action_args_len CHECK (octet_length(arguments_sha256) = 32),
    CONSTRAINT agent_action_seq_positive CHECK (chain_seq > 0),

    -- A denied call cannot have succeeded. Enforce it here rather than trusting
    -- every emitter, because this is the pair an auditor reads first.
    CONSTRAINT agent_action_denied_blocked
        CHECK (decision <> 'deny' OR outcome = 'blocked'),

    -- No two records may claim the same position in the same chain. The backstop
    -- that turns a chain fork into a failed INSERT rather than into a verifier
    -- error discovered weeks later.
    CONSTRAINT agent_action_chain_position UNIQUE (chain_id, chain_seq)
);

-- The head of each chain. Mutable by design: this is a pointer, not evidence.
-- Nothing here needs to be trusted -- if it is wrong, the chain in agent_action
-- still verifies or fails to verify on its own terms.
CREATE TABLE audit.chain_head (
    chain_id   text   PRIMARY KEY,
    head_hash  bytea,
    seq        bigint NOT NULL DEFAULT 0,
    -- The login identity that opened this chain. session_user, not current_user:
    -- inside a SECURITY DEFINER function current_user is the function's owner,
    -- while session_user stays the authenticated login even across SET ROLE.
    -- Chains are owned so that one writer cannot read another writer's head,
    -- and cannot stall another writer by holding its row lock.
    owner      text   NOT NULL DEFAULT session_user,
    -- Chain ids are created on demand by writers, so bound them. Without this a
    -- writer can grow this table without limit by inventing ids; with it, the
    -- damage is bounded per id and `SELECT count(*) FROM audit.chain_head`
    -- is worth alerting on.
    CONSTRAINT chain_head_id_sane CHECK (chain_id <> '' AND length(chain_id) <= 128)
);

-- Index for the query you will actually run. The real investigation is
-- "everything agent X did for user Y between T1 and T2", so the index leads
-- with the two equality columns and ends with the range column.
CREATE INDEX agent_action_agent_principal_time
    ON audit.agent_action (agent_id, principal_id, occurred_at DESC);

-- The second real query: "reconstruct this one task". Cheap, and without it a
-- chain walk degrades into a sequential scan of the whole table.
CREATE INDEX agent_action_correlation
    ON audit.agent_action (correlation_id, occurred_at);

-- The third: "what touched this resource". Prefix matching on a resource path
-- needs text_pattern_ops to use the index under a non-C collation.
CREATE INDEX agent_action_resource_time
    ON audit.agent_action (resource text_pattern_ops, occurred_at DESC);

-- The fourth, and the one people forget: "show me the denials". Partial,
-- because denials are the small minority and the index stays tiny.
CREATE INDEX agent_action_denials
    ON audit.agent_action (occurred_at DESC)
    WHERE decision = 'deny';

-- ---------------------------------------------------------------------------
-- Chaining
-- ---------------------------------------------------------------------------

-- Hand the caller the current head of its chain and the sequence number its next
-- record must carry, holding a row lock until the caller's transaction ends.
-- That lock is what serialises two emitters sharing a chain id; without it both
-- read the same head, compute the same prev_hash, and fork the chain.
--
-- SECURITY DEFINER because the writer role holds no SELECT anywhere in this
-- schema. It gets the one value it needs and nothing else -- notably not the
-- ability to read other agents' records, which is precisely the privilege an
-- append-only writer should not have.
CREATE FUNCTION audit.next_link(p_chain_id text)
RETURNS TABLE (prev_hash bytea, next_seq bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, audit, pg_temp
AS $fn$
BEGIN
    -- Claim the chain on first use, recording who claimed it.
    INSERT INTO audit.chain_head (chain_id) VALUES (p_chain_id)
        ON CONFLICT (chain_id) DO NOTHING;

    -- Then refuse to hand out anything about a chain the caller does not own.
    -- Without this, p_chain_id is an unscoped parameter: any caller could read
    -- another emitter's head hash and sequence number despite holding no SELECT
    -- anywhere in this schema, and could stall that emitter indefinitely by
    -- taking the row lock below on a chain it has nothing to do with.
    IF NOT EXISTS (
        SELECT 1 FROM audit.chain_head h
         WHERE h.chain_id = p_chain_id AND h.owner = session_user
    ) THEN
        RAISE EXCEPTION 'chain % is not owned by %', p_chain_id, session_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN QUERY
        SELECT h.head_hash, h.seq + 1
          FROM audit.chain_head h
         WHERE h.chain_id = p_chain_id
           FOR UPDATE;
END;
$fn$;

-- Enforce the chain at INSERT time. A record that does not continue its chain is
-- rejected here, so a fork is a failed write rather than a verifier alert raised
-- weeks later against records nobody can still explain.
--
-- SECURITY DEFINER so the writer can advance chain_head through this function
-- while holding no direct privilege on that table: it may move the pointer
-- forward by appending a record, and by no other means.
CREATE FUNCTION audit.link_record() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, audit, pg_temp
AS $fn$
DECLARE
    current_head bytea;
    current_seq  bigint;
    found        boolean;
BEGIN
    -- Deliberately does NOT create the chain. Creation happens in next_link and
    -- nowhere else, so there is exactly one path by which this table grows, and
    -- a writer that skips next_link fails here instead of quietly opening a
    -- chain whose head it never locked.
    SELECT true, h.head_hash, h.seq INTO found, current_head, current_seq
      FROM audit.chain_head h
     WHERE h.chain_id = NEW.chain_id AND h.owner = session_user
       FOR UPDATE;

    IF NOT found THEN
        RAISE EXCEPTION 'chain % does not exist or is not owned by % -- call audit.next_link first',
            NEW.chain_id, session_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.prev_hash IS DISTINCT FROM current_head THEN
        RAISE EXCEPTION
            'chain % forked: record claims prev_hash %, head is %',
            NEW.chain_id, encode(NEW.prev_hash, 'hex'), encode(current_head, 'hex')
            USING ERRCODE = 'serialization_failure';
    END IF;

    IF NEW.chain_seq <> current_seq + 1 THEN
        RAISE EXCEPTION
            'chain % out of order: record claims seq %, expected %',
            NEW.chain_id, NEW.chain_seq, current_seq + 1
            USING ERRCODE = 'serialization_failure';
    END IF;

    UPDATE audit.chain_head
       SET head_hash = NEW.record_hash,
           seq       = NEW.chain_seq
     WHERE chain_id = NEW.chain_id;

    RETURN NEW;
END;
$fn$;

CREATE TRIGGER agent_action_link
    BEFORE INSERT ON audit.agent_action
    FOR EACH ROW EXECUTE FUNCTION audit.link_record();

-- ---------------------------------------------------------------------------
-- Append-only, enforced twice
-- ---------------------------------------------------------------------------

-- Layer 1: privileges. This is the layer that actually holds, because it is
-- checked before the statement runs.
--
-- Note that INSERT is granted COLUMN BY COLUMN, and recorded_at is absent from
-- the list. A table-level GRANT INSERT lets the writer supply its own
-- recorded_at, which turns the server-side timestamp into a suggestion and lets
-- a compromised emitter forge a timeline that passes the skew check in
-- investigate.sql. Adding a column to this table means adding it here too --
-- mildly annoying, and much better than the alternative.
GRANT USAGE ON SCHEMA audit TO agent_writer, auditor;

GRANT INSERT (
    record_id, occurred_at, agent_id, principal_id, principal_type, grant_id,
    resource, action, arguments_sha256, arguments_shape,
    decision, decision_reason, policy_version, outcome, outcome_detail,
    correlation_id, parent_span_id,
    model_id, prompt_version, tool_version, untrusted_input,
    canonical_form, prev_hash, record_hash, chain_id, chain_seq
) ON audit.agent_action TO agent_writer;

-- REVOKE FIRST. PostgreSQL grants EXECUTE on every new function to PUBLIC, so
-- without this line the GRANT below is decorative and `audit.next_link` is
-- callable by every role in the database -- including the read-only auditor,
-- which would then be able to create chains and take locks through a SECURITY
-- DEFINER function while its direct INSERT is refused. A definer function is a
-- hole punched through the privilege system on purpose; leaving it open to
-- PUBLIC hands that hole to everyone.
--
-- Check it, do not assume it:
--   SELECT proname, proacl FROM pg_proc WHERE proname = 'next_link';
REVOKE EXECUTE ON FUNCTION audit.next_link(text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION audit.next_link(text) TO agent_writer;

GRANT SELECT ON audit.agent_action TO auditor;
GRANT SELECT ON audit.chain_head   TO auditor;

-- No REVOKE of UPDATE/DELETE/TRUNCATE appears here. PostgreSQL grants no table
-- privileges to PUBLIC, so neither role ever held them, and a REVOKE would be a
-- no-op that reads like a control. Verify the real state instead:
--   SELECT relacl FROM pg_class WHERE relname = 'agent_action';

-- Layer 2: a trigger, so a mistake made as the owner still fails loudly.
-- Be honest about what this buys. The table owner can DROP the trigger and a
-- superuser can ALTER TABLE ... DISABLE TRIGGER, so this catches accidents and
-- careless scripts, not an attacker who already holds the owner role. To survive
-- that, the audit store belongs in a separate database under separate
-- credentials, or behind object storage with a retention lock.
CREATE FUNCTION audit.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'audit.agent_action is append-only (attempted %)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$fn$;

CREATE TRIGGER agent_action_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.agent_action
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
