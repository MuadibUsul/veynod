-- v3_79: server-side Kernel worktree leases for product tasks.
--
-- The lease token never lives on collab_tasks.correlation (that document is
-- returned to the console). One row per product task; a retried lease replaces
-- the token. tenant_id is derived from the workspace, same as kernel_bindings.

CREATE TABLE IF NOT EXISTS kernel_delivery_leases (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    task_key TEXT NOT NULL,
    kernel_task_id UUID NOT NULL,
    lease_token TEXT NOT NULL,
    write_set JSONB NOT NULL DEFAULT '[]'::jsonb,
    base_commit_sha TEXT,
    expires_at TIMESTAMPTZ,
    tenant_id VARCHAR(40),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, task_key)
);

UPDATE kernel_delivery_leases l
SET tenant_id = w.tenant_id
FROM workspaces w
WHERE l.workspace_id = w.id AND l.tenant_id IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM kernel_delivery_leases WHERE tenant_id IS NULL) THEN
        RAISE NOTICE 'kernel_delivery_leases: % rows keep a NULL tenant',
            (SELECT count(*) FROM kernel_delivery_leases WHERE tenant_id IS NULL);
    ELSE
        ALTER TABLE kernel_delivery_leases ALTER COLUMN tenant_id SET NOT NULL;
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_derive_tenant ON kernel_delivery_leases;
CREATE TRIGGER trg_derive_tenant
    BEFORE INSERT OR UPDATE OF workspace_id, tenant_id ON kernel_delivery_leases
    FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
