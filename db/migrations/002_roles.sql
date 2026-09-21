-- Invariant 7: ingestion writes, queries read. Roles are cluster-wide, so they
-- are created only if absent; passwords are set by the migrator from the DSNs.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ci_ingest') THEN
        CREATE ROLE ci_ingest LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ci_query') THEN
        CREATE ROLE ci_query LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA docs TO ci_ingest, ci_query;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA docs TO ci_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA docs TO ci_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA docs TO ci_query;

-- Migration bookkeeping belongs to the migrator alone.
REVOKE INSERT, UPDATE, DELETE ON docs.schema_migrations FROM ci_ingest;

-- Tables added by later migrations (run by the same admin role) get the same grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA docs
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ci_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs
    GRANT USAGE, SELECT ON SEQUENCES TO ci_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs
    GRANT SELECT ON TABLES TO ci_query;
