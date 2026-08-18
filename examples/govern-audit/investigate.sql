-- The investigation. Executed against PostgreSQL 17 with schema.sql loaded.
--
-- Run as the read-only auditor role:
--   psql -U auditor -d auditdb \
--     -v agent="'agent:billing-triage@v7'" \
--     -v principal="'user:ada@example.com'" \
--     -v from="'2026-08-18T00:00:00Z'" \
--     -v to="'2026-08-19T00:00:00Z'" \
--     -f investigate.sql
--
-- Query 1 is the one that gets run at 2am. Queries 2 and 3 are what you run
-- next, and they are here because an audit trail nobody can query is a backup.

\pset null '-'

-- 1. Everything agent X did for user Y in this window.
--    Matches agent_action_agent_principal_time: two equality columns, then the
--    range column, in that order.
SELECT occurred_at,
       correlation_id,
       resource,
       action,
       decision,
       outcome,
       decision_reason,
       grant_id,
       untrusted_input,
       encode(arguments_sha256, 'hex') AS args_digest
  FROM audit.agent_action
 WHERE agent_id     = :agent
   AND principal_id = :principal
   AND occurred_at >= :from::timestamptz
   AND occurred_at  < :to::timestamptz
 ORDER BY occurred_at;

-- 2. The same window, summarised. This is the shape you paste into an incident
--    doc, and the one that tells you whether to keep reading.
SELECT resource,
       action,
       decision,
       outcome,
       count(*)              AS calls,
       min(occurred_at)      AS first_seen,
       max(occurred_at)      AS last_seen,
       bool_or(untrusted_input) AS any_untrusted_input
  FROM audit.agent_action
 WHERE agent_id     = :agent
   AND principal_id = :principal
   AND occurred_at >= :from::timestamptz
   AND occurred_at  < :to::timestamptz
 GROUP BY resource, action, decision, outcome
 ORDER BY calls DESC;

-- 3. Emitter-clock sanity. occurred_at is claimed by the emitter; recorded_at
--    is stamped by this server. A large or negative skew means you cannot
--    trust the ordering in query 1, and you should say so in the report rather
--    than discover it during cross-examination.
SELECT agent_id,
       count(*)                                                   AS records,
       max(recorded_at - occurred_at)                              AS worst_lag,
       count(*) FILTER (WHERE occurred_at > recorded_at)           AS from_the_future
  FROM audit.agent_action
 WHERE occurred_at >= :from::timestamptz
   AND occurred_at  < :to::timestamptz
 GROUP BY agent_id
 ORDER BY worst_lag DESC;
