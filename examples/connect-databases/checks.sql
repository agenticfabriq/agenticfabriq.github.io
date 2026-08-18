-- CI assertions for the containment built by setup_agent_role.sql.
--
--   psql -v ON_ERROR_STOP=1 -d app -U agent_readonly -f checks.sql
--
-- Run this AS THE AGENT'S OWN ROLE. A superuser bypasses row-level security,
-- so the behavioural assertions below would pass vacuously for one.
-- Exits 0 when every assertion holds and 3 on the first failure.
--
-- These are the checklist items from the guide, made executable. Assertions 1
-- and 2 are the two that catch the mistakes most likely to reach production:
-- a table forced into RLS with no policy for its own owner, and a view that
-- resolves its base tables as the owner rather than the caller.
--
-- Verified against PostgreSQL 17.11.

\echo '1. every table with FORCE has a policy that covers its owner'
DO $$
DECLARE bad text;
BEGIN
    SELECT string_agg(c.relname, ', ') INTO bad
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relforcerowsecurity
      AND NOT EXISTS (
          SELECT 1 FROM pg_policy p
          WHERE p.polrelid = c.oid
            AND (p.polroles = '{0}'::oid[] OR c.relowner = ANY (p.polroles))
      );
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'FORCE ROW LEVEL SECURITY with no owner policy on: %. The owning role reads zero rows and every write fails.', bad;
    END IF;
END $$;

\echo '2. every view the agent can read is security_invoker'
DO $$
DECLARE bad text;
BEGIN
    SELECT string_agg(c.relname, ', ') INTO bad
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'v'
      AND has_table_privilege('agent_readonly', c.oid, 'SELECT')
      AND coalesce(array_to_string(c.reloptions, ','), '')
          NOT LIKE '%security_invoker=true%';
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'view(s) % resolve base tables as the view owner, not the caller', bad;
    END IF;
END $$;

\echo '3. the agent role holds no escalation attribute'
DO $$
DECLARE r record;
BEGIN
    SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication
    INTO r FROM pg_roles WHERE rolname = 'agent_readonly';
    IF r IS NULL THEN
        RAISE EXCEPTION 'role agent_readonly does not exist';
    END IF;
    IF r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
       OR r.rolbypassrls OR r.rolreplication THEN
        RAISE EXCEPTION 'agent_readonly holds an escalation attribute: %', r;
    END IF;
END $$;

\echo '4. every table carrying per-user data has RLS enabled and forced'
DO $$
DECLARE bad text;
BEGIN
    SELECT string_agg(t, ', ') INTO bad
    FROM unnest(ARRAY['orders', 'accounts', 'account_members']) AS t
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = t
          AND c.relrowsecurity AND c.relforcerowsecurity
    );
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'missing ENABLE or FORCE ROW LEVEL SECURITY on: %', bad;
    END IF;
END $$;

\echo '5. an unidentified session sees nothing'
DO $$
DECLARE n bigint;
BEGIN
    PERFORM set_config('app.acting_user_id', NULL, false);
    SELECT count(*) INTO n FROM public.orders;
    IF n <> 0 THEN
        RAISE EXCEPTION 'unidentified session saw % orders, expected 0', n;
    END IF;
    SELECT count(*) INTO n FROM public.accounts;
    IF n <> 0 THEN
        RAISE EXCEPTION 'unidentified session saw % accounts, expected 0', n;
    END IF;
END $$;

\echo '6. each user sees exactly their own rows and never the other user rows'
DO $$
DECLARE got bigint[];
BEGIN
    PERFORM set_config('app.acting_user_id', '1', false);
    SELECT array_agg(id ORDER BY id) INTO got FROM public.orders;
    IF got IS DISTINCT FROM ARRAY[5001, 5002]::bigint[] THEN
        RAISE EXCEPTION 'user 1 saw orders %, expected {5001,5002}', got;
    END IF;
    SELECT array_agg(id ORDER BY id) INTO got FROM public.accounts;
    IF got IS DISTINCT FROM ARRAY[100]::bigint[] THEN
        RAISE EXCEPTION 'user 1 saw accounts %, expected {100}', got;
    END IF;

    PERFORM set_config('app.acting_user_id', '2', false);
    SELECT array_agg(id ORDER BY id) INTO got FROM public.orders;
    IF got IS DISTINCT FROM ARRAY[5003]::bigint[] THEN
        RAISE EXCEPTION 'user 2 saw orders %, expected {5003}', got;
    END IF;
    SELECT array_agg(id ORDER BY id) INTO got FROM public.accounts;
    IF got IS DISTINCT FROM ARRAY[200]::bigint[] THEN
        RAISE EXCEPTION 'user 2 saw accounts %, expected {200}', got;
    END IF;

    PERFORM set_config('app.acting_user_id', NULL, false);
END $$;

\echo '7. the excluded column is not readable, even to an identified session'
DO $$
BEGIN
    PERFORM set_config('app.acting_user_id', '1', false);
    PERFORM billing_email FROM public.accounts;
    RAISE EXCEPTION 'billing_email was readable; the column grant is too wide';
EXCEPTION
    WHEN insufficient_privilege THEN
        NULL;  -- expected: permission denied for table accounts
END $$;

\echo 'all assertions passed'
