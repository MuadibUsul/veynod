-- v3_75: unified Runtime lifecycle, Kernel bindings and repository registry.
--
-- Expand-only migration (ADR-013..018).  Nothing is dropped; every column is
-- nullable or defaulted so the previous release keeps working during rollout.
--
-- 1. collab_tasks gains one correlation document and one active Run pointer.
-- 2. kernel_bindings routes (workspace, project, repo) to a Kernel workspace.
-- 3. project_repositories lets one project own several repositories.
-- 4. lifecycle_projections makes execution-driven task writes idempotent.
-- 5. projects gains tenant_id (every owned row carries its derived tenant).

-- ---------------------------------------------------------------- collab_tasks
ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS correlation JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS active_run_id UUID;

-- One live Run per product task.  The application enforces it; the partial
-- index is the backstop that a race cannot defeat.
CREATE UNIQUE INDEX IF NOT EXISTS uq_collab_tasks_active_run
    ON collab_tasks (active_run_id) WHERE active_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_collab_tasks_correlation_run
    ON collab_tasks ((correlation->>'run_id'))
    WHERE correlation ? 'run_id';
CREATE INDEX IF NOT EXISTS idx_collab_tasks_correlation_trace
    ON collab_tasks ((correlation->>'trace_id'))
    WHERE correlation ? 'trace_id';

-- ------------------------------------------------------------ kernel_bindings
CREATE TABLE IF NOT EXISTS kernel_bindings (
    binding_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project TEXT NOT NULL DEFAULT '',
    repo_path TEXT NOT NULL,
    kernel_base_url TEXT NOT NULL,
    kernel_workspace_id UUID NOT NULL,
    credential_ref TEXT NOT NULL DEFAULT 'KERNEL_RUNTIME_TOKEN',
    worker_hub_id TEXT NOT NULL DEFAULT 'veynod-runtime',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, project, repo_path)
);

CREATE INDEX IF NOT EXISTS idx_kernel_bindings_lookup
    ON kernel_bindings (workspace_id, project, repo_path)
    WHERE status = 'ACTIVE';

-- ------------------------------------------------------- project_repositories
CREATE TABLE IF NOT EXISTS project_repositories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    repo_path TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    kernel_workspace_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, repo_path)
);

CREATE INDEX IF NOT EXISTS idx_project_repositories_workspace
    ON project_repositories (workspace_id, repo_path);

-- ------------------------------------------------------ lifecycle_projections
CREATE TABLE IF NOT EXISTS lifecycle_projections (
    dedupe_key TEXT PRIMARY KEY,
    task_key TEXT NOT NULL,
    action TEXT NOT NULL,
    run_id UUID,
    event_seq BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_projections_task
    ON lifecycle_projections (task_key, created_at DESC);

-- ------------------------------------------------------------ projects tenant
ALTER TABLE projects ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40);
UPDATE projects p
SET tenant_id = w.tenant_id
FROM workspaces w
WHERE p.workspace_id = w.id AND p.tenant_id IS NULL;
ALTER TABLE projects ALTER COLUMN tenant_id SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'projects_tenant_id_fkey'
    ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT projects_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
    END IF;
END $$;
DROP TRIGGER IF EXISTS trg_derive_tenant ON projects;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON projects FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();

-- event_inbox already keys on (source, source_instance, source_seq); the
-- tenant column lands with the workspace's tenant so every owned row carries
-- its derived tenant (its workspace_id is TEXT, so it gets its own derive fn).
ALTER TABLE event_inbox ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40);
UPDATE event_inbox e
SET tenant_id = w.tenant_id
FROM workspaces w
WHERE e.workspace_id = w.id::text AND e.tenant_id IS NULL;

CREATE OR REPLACE FUNCTION agentgraph_derive_tenant_text() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    NEW.tenant_id := (
        SELECT tenant_id FROM workspaces
        WHERE id::text = NEW.workspace_id
    );
    IF NEW.tenant_id IS NULL THEN
        RAISE EXCEPTION 'workspace % has no tenant', NEW.workspace_id
            USING ERRCODE = 'not_null_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_derive_tenant ON event_inbox;
CREATE TRIGGER trg_derive_tenant
    BEFORE INSERT OR UPDATE OF workspace_id, tenant_id ON event_inbox
    FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant_text();

-- Legacy inbox rows that name no workspace cannot derive a tenant; the
-- constraint is only applied when every row can carry one (idempotent, and it
-- never deletes or rewrites history).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM event_inbox WHERE tenant_id IS NULL) THEN
        ALTER TABLE event_inbox ALTER COLUMN tenant_id SET NOT NULL;
    ELSE
        RAISE NOTICE 'event_inbox keeps nullable tenant_id: % legacy rows name '
                     'no workspace', (SELECT count(*) FROM event_inbox
                                      WHERE tenant_id IS NULL);
    END IF;
END $$;
