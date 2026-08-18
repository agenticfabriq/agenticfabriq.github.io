-- Audit store for agent actions. Executed against PostgreSQL 17.
--
-- Every column below exists because some investigation needs it. If you add a
-- column, write down the question it answers first; if you cannot, drop it.
--
-- Run:  psql -v ON_ERROR_STOP=1 -f schema.sql
--
-- This file creates two roles. `agent_writer` is the identity the control layer
-- uses to append records. `auditor` is the identity a human investigator uses.
-- Neither is the owner of the table, and that separation is the point.

CREATE SCHEMA IF NOT EXISTS audit;

CREATE TYPE audit.decision AS ENUM ('allow', 'deny');
CREATE TYPE audit.outcome  AS ENUM ('success', 'error', 'blocked');

CREATE TABLE audit.agent_action (
    -- Identity of the record itself, so a later record can point back at it.
    record_id        uuid        PRIMARY KEY,

    -- WHEN. Two timestamps, deliberately.
    -- occurred_at is what the emitter claims. recorded_at is what the store
    -- observed, set by the server and never accepted from the client. A large
    -- gap between them is the signal that a clock is wrong or that a batch of
    -- records was replayed late.
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
    outcome_detail   text,

    -- THE CHAIN. One id per user-visible task, propagated to every tool call
    -- that task makes. This is what turns nine unrelated rows into one story.
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

    -- INTEGRITY. record_hash covers every field above, plus prev_hash.
    prev_hash        bytea,
    record_hash      bytea       NOT NULL,

    CONSTRAINT agent_action_hash_len CHECK (octet_length(record_hash) = 32),
    CONSTRAINT agent_action_prev_len CHECK (prev_hash IS NULL OR octet_length(prev_hash) = 32),
    CONSTRAINT agent_action_args_len CHECK (octet_length(arguments_sha256) = 32),

    -- A denied call cannot have succeeded. Enforce it here rather than trusting
    -- every emitter, because this is the pair an auditor reads first.
    CONSTRAINT agent_action_denied_blocked
        CHECK (decision <> 'deny' OR outcome = 'blocked')
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

-- Append-only, enforced twice.
--
-- Layer 1: privileges. The writer may only INSERT; the auditor may only SELECT.
-- This is the layer that actually holds, because it is checked before the
-- statement runs.
CREATE ROLE agent_writer NOLOGIN;
CREATE ROLE auditor NOLOGIN;

GRANT USAGE ON SCHEMA audit TO agent_writer, auditor;
GRANT INSERT ON audit.agent_action TO agent_writer;
GRANT SELECT ON audit.agent_action TO auditor;
REVOKE UPDATE, DELETE, TRUNCATE ON audit.agent_action FROM agent_writer, auditor;

-- Layer 2: a trigger, so a mistake made as the owner still fails loudly.
-- Be honest about what this buys. The table owner can DROP the trigger and a
-- superuser can ALTER TABLE ... DISABLE TRIGGER, so this catches accidents and
-- careless scripts, not an attacker who already holds the owner role. To
-- survive that, the audit store belongs in a separate database under separate
-- credentials, or behind object storage with a retention lock.
CREATE FUNCTION audit.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.agent_action is append-only (attempted %)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER agent_action_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.agent_action
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
