-- Why a view is where per-user access quietly dies, in three configurations.
--
--   psql -v ON_ERROR_STOP=1 -d app -U postgres -f view_ownership.sql
--
-- Run as a superuser AFTER setup_agent_role.sql. It uses SET ROLE rather than
-- separate connections; row-level security applies to the effective role, so
-- SET ROLE agent_readonly is subject to policies exactly as a login would be.
-- The script cleans up after itself and leaves the database as it found it.
--
-- The claim being tested: a view without security_invoker returns whatever the
-- VIEW OWNER can see, not what the caller can see. What that means for the
-- agent depends entirely on what the owner can see.
--
-- Verified against PostgreSQL 17.11.

SET ROLE app_owner;
CREATE VIEW public.v_definer AS
    SELECT account_id, count(*) AS n FROM public.orders GROUP BY account_id;
CREATE VIEW public.v_invoker WITH (security_invoker = true) AS
    SELECT account_id, count(*) AS n FROM public.orders GROUP BY account_id;
GRANT SELECT ON public.v_definer, public.v_invoker TO agent_readonly;
RESET ROLE;

\echo '=== A. owner is a non-superuser WITH the app_owner_all policy'
\echo '===    (this is what setup_agent_role.sql builds)'
SET ROLE agent_readonly;
SELECT set_config('app.acting_user_id', '1', false);
\echo '--- definer view: leaks account 200 to a user who belongs to 100 only'
SELECT * FROM public.v_definer ORDER BY account_id;
\echo '--- invoker view: correct'
SELECT * FROM public.v_invoker ORDER BY account_id;
RESET ROLE;

\echo '=== B. same owner, but its permissive policy removed'
SET ROLE app_owner;
DROP POLICY app_owner_all ON public.orders;
RESET ROLE;
SET ROLE agent_readonly;
SELECT set_config('app.acting_user_id', '1', false);
\echo '--- definer view: now returns NOTHING, because the owner itself is'
\echo '--- subject to FORCE with no policy. The application is down, not safe.'
SELECT * FROM public.v_definer ORDER BY account_id;
RESET ROLE;
SET ROLE app_owner;
CREATE POLICY app_owner_all ON public.orders
    FOR ALL TO app_owner USING (true) WITH CHECK (true);
RESET ROLE;

\echo '=== C. owner is a superuser'
CREATE VIEW public.v_definer_super AS
    SELECT account_id, count(*) AS n FROM public.orders GROUP BY account_id;
GRANT SELECT ON public.v_definer_super TO agent_readonly;
SET ROLE agent_readonly;
SELECT set_config('app.acting_user_id', '1', false);
\echo '--- leaks every account: a superuser owner bypasses RLS entirely,'
\echo '--- and FORCE does not constrain a superuser'
SELECT * FROM public.v_definer_super ORDER BY account_id;
RESET ROLE;

DROP VIEW public.v_definer_super;
SET ROLE app_owner;
DROP VIEW public.v_definer;
DROP VIEW public.v_invoker;
RESET ROLE;
