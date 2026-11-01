-- v3_77: the two new workspace-owned tables carry their derived tenant.
--
-- Kept separate from v3_75 because v3_75 may already be applied; re-running
-- with IF NOT EXISTS is safe in either order.

ALTER TABLE kernel_bindings ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40);
ALTER TABLE project_repositories ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40);

UPDATE kernel_bindings b
SET tenant_id = w.tenant_id
FROM workspaces w
WHERE b.workspace_id = w.id AND b.tenant_id IS NULL;

UPDATE project_repositories r
SET tenant_id = w.tenant_id
FROM workspaces w
WHERE r.workspace_id = w.id AND r.tenant_id IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM kernel_bindings WHERE tenant_id IS NULL) THEN
        RAISE NOTICE 'kernel_bindings: % rows keep a NULL tenant (no workspace)',
            (SELECT count(*) FROM kernel_bindings WHERE tenant_id IS NULL);
    ELSE
        ALTER TABLE kernel_bindings ALTER COLUMN tenant_id SET NOT NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM project_repositories WHERE tenant_id IS NULL) THEN
        RAISE NOTICE 'project_repositories: % rows keep a NULL tenant',
            (SELECT count(*) FROM project_repositories WHERE tenant_id IS NULL);
    ELSE
        ALTER TABLE project_repositories ALTER COLUMN tenant_id SET NOT NULL;
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_derive_tenant ON kernel_bindings;
CREATE TRIGGER trg_derive_tenant
    BEFORE INSERT OR UPDATE OF workspace_id, tenant_id ON kernel_bindings
    FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();

DROP TRIGGER IF EXISTS trg_derive_tenant ON project_repositories;
CREATE TRIGGER trg_derive_tenant
    BEFORE INSERT OR UPDATE OF workspace_id, tenant_id ON project_repositories
    FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
