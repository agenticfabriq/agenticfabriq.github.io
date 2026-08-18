-- Proof that the policies in setup_agent_role.sql key on the acting user, run
-- as the constrained role rather than as the owner.
--
--   psql -d app -U agent_readonly -f prove_rls.sql
--
-- Do NOT pass -v ON_ERROR_STOP=1. Sections 1 and 6 deliberately run statements
-- that must fail. Without ON_ERROR_STOP psql prints each error inline, keeps
-- going, and exits 0; with it, psql aborts at the first intentional failure and
-- exits 3. checks.sql is the file to run in CI: it asserts the same properties
-- and exits 0 under ON_ERROR_STOP=1.
--
-- Verified against PostgreSQL 17.11.

\echo '=== 1. a pristine session, before any identity has been set'

\echo '--- ERROR: unrecognized configuration parameter "app.acting_user_id"'
\echo '---        current_setting without missing_ok raises instead of filtering.'
\echo '---        This only reproduces on a connection where the name has never'
\echo '---        been set: once it has, a RESET leaves the empty string behind'
\echo '---        and the same call returns that instead of raising.'
SELECT current_setting('app.acting_user_id');

\echo '--- the two-argument form returns NULL, so the policy filters and'
\echo '--- an unidentified session is a stranger that sees nothing'
SELECT current_setting('app.acting_user_id', true) IS NULL AS identity_is_null;
SELECT count(*) AS visible_orders FROM public.orders;

\echo '=== 2. acting as user 1 (a member of account 100)'
BEGIN READ ONLY;
SELECT set_config('app.acting_user_id', '1', true);
SELECT id, account_id, status FROM public.orders ORDER BY id;
SELECT id, name FROM public.accounts ORDER BY id;
COMMIT;

\echo '=== 3. acting as user 2 (a member of account 200)'
BEGIN READ ONLY;
SELECT set_config('app.acting_user_id', '2', true);
SELECT id, account_id, status FROM public.orders ORDER BY id;
SELECT id, name FROM public.accounts ORDER BY id;
COMMIT;

\echo '=== 4. the identity does not leak past COMMIT, because is_local was true'
SELECT count(*) AS visible_orders FROM public.orders;

\echo '=== 5. the ceilings the role carries into every session'
SELECT name, setting
FROM pg_settings
WHERE name IN ('statement_timeout', 'lock_timeout',
               'idle_in_transaction_session_timeout', 'transaction_timeout',
               'default_transaction_read_only', 'log_statement')
ORDER BY name;

\echo '--- statement_timeout is a USERSET parameter: the client can raise it'
SELECT name, context FROM pg_settings WHERE name = 'statement_timeout';
SET statement_timeout = 0;
SHOW statement_timeout;
RESET statement_timeout;

\echo '--- LIMIT is not a work cap: the bare limit stops early, the sort cannot'
EXPLAIN SELECT o.id FROM public.orders o CROSS JOIN public.orders o2 LIMIT 200;
EXPLAIN SELECT o.id FROM public.orders o CROSS JOIN public.orders o2
        ORDER BY o.total_cents LIMIT 200;

\echo '=== 6. everything below is SUPPOSED to fail. Expected error above each.'

\echo '--- ERROR: syntax error at or near "SET" -- SET cannot be prepared,'
\echo '---        which is why the identity goes through set_config instead'
PREPARE p_set AS SET app.acting_user_id = $1;

\echo '--- PREPARE -- the set_config form takes a bound parameter'
PREPARE p_setconfig AS SELECT set_config('app.acting_user_id', $1, true);
DEALLOCATE p_setconfig;

\echo '--- ERROR: unrecognized configuration parameter "acting_user_id"'
\echo '---        a custom setting name must carry a prefix'
SET acting_user_id = '1';

\echo '--- the empty string a RESET leaves behind, not an error, because'
\echo '--- sections 2 and 3 defined the name in this session. NULLIF handles it.'
SELECT quote_literal(current_setting('app.acting_user_id', true)) AS after_reset;

\echo '--- ERROR: invalid input syntax for type bigint: "" -- why NULLIF is there'
SELECT ''::bigint;

\echo '--- ERROR: permission denied for table accounts -- the column grant bites'
SELECT billing_email FROM public.accounts;

\echo '--- ERROR: permission denied for table orders -- no write privilege,'
\echo '---        independent of default_transaction_read_only'
SET default_transaction_read_only = off;
INSERT INTO public.orders (id, account_id, status, total_cents)
VALUES (9999, 100, 'shipped', 1);
