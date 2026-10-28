-- v3.56 Owner acceptance (Loop v1, second slice).
--
-- The owner tries delivered work in the console and answers in plain words:
-- it works, it does not, or what to change. A "does not" or "change" verdict
-- creates a follow-up task carrying the owner's words, so the owner never has
-- to give commit, table or test details. One row per verdict; the newest row
-- of a task is its current verdict.

CREATE TABLE IF NOT EXISTS owner_acceptance (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID NOT NULL REFERENCES collab_tasks(id) ON DELETE CASCADE,
    task_key VARCHAR(128) NOT NULL,
    verdict VARCHAR(16) NOT NULL CHECK (verdict IN ('works', 'broken', 'change')),
    note TEXT,
    follow_up_task_key VARCHAR(128),
    decided_by VARCHAR(128) NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_owner_acceptance_task
    ON owner_acceptance (workspace_id, task_id, decided_at DESC);

DO $$
BEGIN
    EXECUTE 'DROP TRIGGER IF EXISTS trg_derive_tenant ON owner_acceptance';
    EXECUTE 'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
            'workspace_id, tenant_id ON owner_acceptance FOR EACH ROW '
            'EXECUTE FUNCTION agentgraph_derive_tenant()';
    EXECUTE 'ALTER TABLE owner_acceptance ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE owner_acceptance FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON owner_acceptance';
    EXECUTE 'CREATE POLICY tenant_isolation ON owner_acceptance '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON owner_acceptance TO agentgraph_app';
    EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE owner_acceptance_id_seq TO agentgraph_app';
END $$;
