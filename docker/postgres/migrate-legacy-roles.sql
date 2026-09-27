\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meetmind_owner') THEN
        CREATE ROLE meetmind_owner LOGIN PASSWORD 'meetmind-owner-dev' SUPERUSER;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meetmind_app') THEN
        CREATE ROLE meetmind_app LOGIN PASSWORD 'meetmind-app-dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

-- Do not use `REASSIGN OWNED BY meetmind` here. In legacy development
-- volumes, `meetmind` is the PostgreSQL bootstrap superuser and therefore
-- owns system objects (for example the plpgsql extension). PostgreSQL
-- correctly refuses to transfer objects that are required by the system.
-- Restrict the ownership migration to application relations in `public`.
DO $$
DECLARE
    relation record;
    relation_type text;
BEGIN
    FOR relation IN
        SELECT c.relkind, n.nspname, c.relname
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_roles AS r ON r.oid = c.relowner
        WHERE r.rolname = 'meetmind'
          AND n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
    LOOP
        relation_type := CASE relation.relkind
            WHEN 'S' THEN 'SEQUENCE'
            WHEN 'v' THEN 'VIEW'
            WHEN 'm' THEN 'MATERIALIZED VIEW'
            WHEN 'f' THEN 'FOREIGN TABLE'
            ELSE 'TABLE'
        END;
        EXECUTE format(
            'ALTER %s %I.%I OWNER TO meetmind_owner',
            relation_type,
            relation.nspname,
            relation.relname
        );
    END LOOP;
END
$$;

ALTER DATABASE meetmind OWNER TO meetmind_owner;
GRANT CONNECT ON DATABASE meetmind TO meetmind_app;
GRANT USAGE ON SCHEMA public TO meetmind_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO meetmind_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO meetmind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE meetmind_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO meetmind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE meetmind_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO meetmind_app;
