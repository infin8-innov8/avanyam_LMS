\set QUIET on

-- ── databases ───────────────────────────────────────────────────────────────
-- avanyam holds all business data and stays at default RLS.
-- The audit and reporting databases get RLS *enabled* at creation and are never
-- exempted. See the "RLS on the audit database" decision in memory.md.
SELECT 'CREATE DATABASE avanyam'          WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='avanyam')          \gexec
SELECT 'CREATE DATABASE avanyam_audit'    WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='avanyam_audit')    \gexec
SELECT 'CREATE DATABASE avanyam_reporting' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='avanyam_reporting') \gexec

-- keycloak is Keycloak's OWN schema. Keycloak migrates it itself via kc.sh at
-- startup and NEVER via Django's migrate; the two never coordinate.
-- avanyam_intro.txt §14.1: "avanyam_intro.txt §14.1 POSTGRES DATABASES ON VM2 -
-- THREE, NOT ONE". Its absence made Keycloak unprovisionable (K3).
SELECT 'CREATE DATABASE keycloak' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='keycloak') \gexec

-- keycloak_test holds realm fixtures for CI. The spec says "Never on production",
-- so it is OFF by default and must be opted into explicitly:
--   psql -v create_ci_db=true -f bootstrap.sql
-- A careless production run therefore cannot create it.
\if :{?create_ci_db}
\else
  \set create_ci_db false
\endif
SELECT 'CREATE DATABASE keycloak_test' WHERE :create_ci_db AND NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='keycloak_test') \gexec


-- ── roles ───────────────────────────────────────────────────────────────────
-- avanyam_app         DML only. No DDL, no BYPASSRLS.
-- avanyam_migrate     owns the schema. Owns avanyam_db.
-- avanyam_audit       owns avanyam_audit. Append-only, cannot delete.
-- avanyam_reporting   owns avanyam_reporting. Owns no business tables.
-- keycloak            owns keycloak ONLY. Must never reach an avanyam_* database:
--                     it is a different trust domain, and the spec keeps Keycloak
--                     on its own connection pool outside PgBouncer's transaction
--                     pool precisely because it is handled separately.
-- keycloak_test       owns keycloak_test ONLY. CI realms, never production.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='avanyam_app') THEN
    CREATE ROLE avanyam_app LOGIN PASSWORD 'CHANGE_ME_app';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='avanyam_migrate') THEN
    CREATE ROLE avanyam_migrate LOGIN PASSWORD 'CHANGE_ME_migrate';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='avanyam_audit') THEN
    CREATE ROLE avanyam_audit LOGIN PASSWORD 'CHANGE_ME_audit';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='avanyam_reporting') THEN
    CREATE ROLE avanyam_reporting LOGIN PASSWORD 'CHANGE_ME_reporting';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='keycloak') THEN
    CREATE ROLE keycloak LOGIN PASSWORD 'CHANGE_ME_keycloak';
  END IF;
END $$;

-- keycloak_test exists only when the CI database was requested above.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_database WHERE datname='keycloak_test')
     AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='keycloak_test') THEN
    CREATE ROLE keycloak_test LOGIN PASSWORD 'CHANGE_ME_keycloak_test';
  END IF;
END $$;

-- ── extensions ──────────────────────────────────────────────────────────────
\connect avanyam
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- ILIKE search on ContentItem.title
CREATE EXTENSION IF NOT EXISTS btree_gin;  -- GIN + btree composite indexes
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ── ownership ───────────────────────────────────────────────────────────────
\connect avanyam
ALTER DATABASE avanyam OWNER TO avanyam_migrate;
-- Deterministic grants. REVOKE ... FROM PUBLIC cannot remove an explicit grant
-- left behind by an earlier version of this script, so every managed role is
-- revoked from every managed database first. Without this the script is not
-- idempotent in the direction that matters: it cannot shrink privileges.
REVOKE ALL ON DATABASE avanyam FROM avanyam_app, avanyam_audit, avanyam_reporting;
-- Keycloak is a separate trust domain and must never read the business database,
-- even if it is later co-located on the same cluster (it will be).
REVOKE ALL ON DATABASE avanyam FROM keycloak;
-- avanyam_reporting reaches the reporting database ONLY. Not being able to open
-- the business database at all is a second control behind "it cannot read the
-- tables", and unlike table grants it cannot be re-enabled by a stray grant.
GRANT CONNECT ON DATABASE avanyam TO avanyam_app, avanyam_audit;
REVOKE ALL ON DATABASE avanyam FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO avanyam_migrate;
GRANT USAGE ON SCHEMA public TO avanyam_app;
ALTER DEFAULT PRIVILEGES FOR ROLE avanyam_migrate IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO avanyam_app;
ALTER DEFAULT PRIVILEGES FOR ROLE avanyam_migrate IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO avanyam_app;

\connect avanyam_audit
ALTER DATABASE avanyam_audit OWNER TO avanyam_audit;
REVOKE ALL ON DATABASE avanyam_audit FROM avanyam_reporting;
GRANT USAGE ON SCHEMA public TO avanyam_app, avanyam_migrate;
REVOKE ALL ON DATABASE avanyam_audit FROM PUBLIC;
GRANT CONNECT ON DATABASE avanyam_audit TO avanyam_app;

-- avanyam_reporting gets no CONNECT here and none above: it reaches only the
-- reporting database. See the note on the avanyam block.
\connect avanyam_reporting
ALTER DATABASE avanyam_reporting OWNER TO avanyam_reporting;
REVOKE ALL ON DATABASE avanyam_reporting FROM avanyam_audit;
REVOKE ALL ON DATABASE avanyam_reporting FROM PUBLIC;
GRANT CONNECT ON DATABASE avanyam_reporting TO avanyam_app;

-- ── keycloak isolation ──────────────────────────────────────────────────────
-- Keycloak is a SEPARATE trust domain. Its role gets its own database and
-- nothing else: no CONNECT to any avanyam_* database, and no access to the
-- business schema. This is the database-layer half of "Keycloak is never in the
-- PgBouncer transaction pool" (architecture.md §3/§4).
\connect keycloak
ALTER DATABASE keycloak OWNER TO keycloak;
REVOKE ALL ON DATABASE keycloak FROM PUBLIC;
REVOKE ALL ON DATABASE keycloak FROM avanyam_app, avanyam_migrate, avanyam_audit, avanyam_reporting;
GRANT CONNECT, TEMPORARY ON DATABASE keycloak TO keycloak;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO keycloak;

-- Postcondition. A new PostgreSQL database is created with CONNECT and
-- TEMPORARY granted to PUBLIC, so the REVOKEs above are load-bearing: without
-- them every role in the cluster, including avanyam_app, can open a connection
-- to the Keycloak database. On this host the REVOKE FROM PUBLIC had not taken
-- effect (the ACL still read '=Tc/keycloak'), so avanyam_app could connect even
-- though it had no schema privileges. No data was exposed -- the schema REVOKE
-- held -- but the isolation this file claims was not actually in force.
--
-- This block makes the script fail loudly instead of leaving that to chance.

-- keycloak_test mirrors the above for the CI realm database, and is created only
-- when -v create_ci_db=true was passed. It needs the same PUBLIC revoke, and
-- also must not be reachable by avanyam_app -- the CI realm holds test users.
SELECT 'REVOKE ALL ON DATABASE keycloak_test FROM PUBLIC'
 WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname='keycloak_test') \gexec
SELECT 'REVOKE ALL ON DATABASE keycloak_test FROM avanyam_app, avanyam_migrate, avanyam_audit, avanyam_reporting'
 WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname='keycloak_test') \gexec
SELECT 'GRANT CONNECT ON DATABASE keycloak_test TO keycloak_test'
 WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname='keycloak_test') \gexec
SELECT 'ALTER DATABASE keycloak_test OWNER TO keycloak_test'
 WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname='keycloak_test') \gexec

-- The postcondition goes LAST, and that ordering is load-bearing. It scans every
-- database matching 'keycloak%', which includes keycloak_test -- but the revokes
-- for keycloak_test are the statements immediately above. Placed any earlier, a
-- CI host with a freshly created keycloak_test (still carrying the default PUBLIC
-- CONNECT) aborts the script with "keycloak isolation broken" before reaching the
-- revokes that would have fixed it.
DO $$
DECLARE
    leaks text;
BEGIN
    SELECT string_agg(datname, ', ')
      INTO leaks
      FROM pg_database
     WHERE datname LIKE 'keycloak%'
       AND has_database_privilege('avanyam_app', datname, 'CONNECT');

    IF leaks IS NOT NULL THEN
        RAISE EXCEPTION
            'keycloak isolation broken: avanyam_app can CONNECT to %', leaks;
    END IF;
END $$;

-- ── RLS everywhere, no exceptions ───────────────────────────────────────────
\connect avanyam
ALTER DATABASE avanyam SET row_security = on;
\connect avanyam_audit
ALTER DATABASE avanyam_audit SET row_security = on;
\connect avanyam_reporting
ALTER DATABASE avanyam_reporting SET row_security = on;

-- ── reporting role must never read base tables ──────────────────────────────
-- Same process, same credentials: only the reporting *database* is reachable.
\connect avanyam_reporting
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- ── audit_log grants ─────────────────────────────────────────────────────────
-- Run AFTER the Django migration that creates audit_log, because the table must
-- exist before privileges can be granted on it. Kept here so the append-only
-- guarantee is defined in one place rather than remembered separately.
--
--   \connect avanyam_audit
--   GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO avanyam_app;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO avanyam_app;
--   REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM avanyam_app;
--
-- SELECT is granted so the UI can display history. UPDATE, DELETE and TRUNCATE
-- are revoked so the log is append-only for the application role: tampering
-- requires the avanyam_audit owner, which the web process never holds.
