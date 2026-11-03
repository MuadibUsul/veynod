-- The product role was never granted the tables another role created.
--
-- v3_2 set `ALTER DEFAULT PRIVILEGES` so new tables reach `agentgraph_app`
-- automatically — but default privileges are recorded per creating role, and
-- it recorded them for `agentgraph` only. Migrations apply as `veynod`, so
-- every table they create (kernel_bindings, kernel_delivery_leases, the
-- projection tables, projects, event_inbox …) is owned by `veynod` and got no
-- grant. Nothing notices until the app role first touches one:
-- `permission denied for table kernel_delivery_leases` on the first real
-- lease, `kernel_bindings` on the one before it.
--
-- So this does both halves. The default privileges are registered for the
-- role that actually creates tables, which fixes it for every future one; the
-- loop repairs what already exists. Both are guarded, so the file also runs on
-- a database that predates a table or the role.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'veynod')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentgraph_app') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE veynod IN SCHEMA public '
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentgraph_app';
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE veynod IN SCHEMA public '
                'GRANT USAGE, SELECT ON SEQUENCES TO agentgraph_app';
    END IF;
END
$$;

DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'admin_access_sessions', 'budget_limits', 'code_projection_cursors',
        'diagram_chunk_projections', 'event_inbox', 'export_archives',
        'exports', 'imports', 'kernel_bindings', 'kernel_delivery_leases',
        'lifecycle_projections', 'platform_releases', 'project_repositories',
        'projects', 'retrieval_eval_runs', 'retrieval_probes',
        'runtime_projection_cursors', 'runtime_usage_metered',
        'tenant_confirmations', 'tenant_policies', 'tenant_runtime_limits',
        'tenant_shares'
    ] LOOP
        IF EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = target
        ) AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentgraph_app') THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO agentgraph_app',
                           target);
        END IF;
    END LOOP;
END
$$;
