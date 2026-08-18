-- Proof that the policy in setup_agent_role.sql actually keys on the acting
-- user, run as the constrained role rather than as the owner.
--
--   psql -v ON_ERROR_STOP=1 -d app -U agent_readonly -f prove_rls.sql
--
-- Verified against PostgreSQL 17.11.

\echo '--- no identity set: the session is a stranger and sees nothing'
SELECT count(*) AS visible_orders FROM public.orders;

\echo '--- acting as user 1 (a member of account 100)'
BEGIN READ ONLY;
SELECT set_config('app.acting_user_id', '1', true);
SELECT id, account_id, status FROM public.orders ORDER BY id;
COMMIT;

\echo '--- acting as user 2 (a member of account 200)'
BEGIN READ ONLY;
SELECT set_config('app.acting_user_id', '2', true);
SELECT id, account_id, status FROM public.orders ORDER BY id;
COMMIT;

\echo '--- the identity does not leak past COMMIT, because is_local was true'
SELECT count(*) AS visible_orders FROM public.orders;

\echo '--- the ceilings the role carries into every session'
SELECT name, setting
FROM pg_settings
WHERE name IN ('statement_timeout', 'lock_timeout',
               'idle_in_transaction_session_timeout', 'transaction_timeout',
               'default_transaction_read_only')
ORDER BY name;

\echo '--- statement_timeout is a USERSET parameter: the client can raise it'
SELECT name, context FROM pg_settings WHERE name = 'statement_timeout';
SET statement_timeout = 0;
SHOW statement_timeout;

\echo '--- writes still fail, because no write privilege was ever granted'
SET default_transaction_read_only = off;
INSERT INTO public.orders (id, account_id, status, total_cents)
VALUES (9999, 100, 'shipped', 1);
