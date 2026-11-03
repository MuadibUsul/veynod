-- The product roles were never granted the newer tables.
--
-- RLS-era migrations grant `agentgraph_app` per table as they create it; these
-- tables came in without it, and the omission only shows when the app role
-- first touches one: `permission denied for table kernel_delivery_leases` on
-- the first real lease, `kernel_bindings` on the first lease after a binding
-- existed, and the same for the projection tables the promotion path writes.
--
-- Existence-guarded, so this also runs on a database that predates a table.

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
