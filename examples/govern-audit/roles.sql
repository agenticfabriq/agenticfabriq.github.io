-- Role provisioning for the audit store. Run this FIRST, and separately.
--
--   psql -v ON_ERROR_STOP=1 -f roles.sql      # as a DBA
--   psql -v ON_ERROR_STOP=1 -f schema.sql     # as the audit schema owner
--
-- This file is split out because CREATE ROLE needs CREATEROLE or superuser,
-- which the role that owns the audit schema should not have. Running it as a
-- plain owner fails with "permission denied to create role", which is the
-- correct outcome -- it means your owner is not over-privileged.
--
-- Both roles are NOLOGIN on purpose. They are privilege sets to be granted to
-- real login users, not shared accounts with shared passwords:
--
--   GRANT agent_writer TO gateway_svc;   -- the control layer's login user
--   GRANT auditor      TO alice;         -- a named human investigator
--
-- A member then adopts the privilege set with SET ROLE (see investigate.sql).

CREATE ROLE agent_writer NOLOGIN;
CREATE ROLE auditor NOLOGIN;
