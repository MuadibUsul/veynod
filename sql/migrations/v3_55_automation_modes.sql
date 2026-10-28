-- v3.55 Automation mode: manual / semi / full, per workspace (Loop v1).
--
-- The owner picks how far an agent may go without asking. The server exposes
-- the value to humans and to agents (orientation), and every change is audited
-- and announced. auto_deploy_allowed is the owner's separate permission for
-- the deploy gate to ship without a person.

CREATE TABLE IF NOT EXISTS automation_modes (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'manual'
        CHECK (mode IN ('manual', 'semi', 'full')),
    auto_deploy_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by VARCHAR(128) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1
);

DO $$
BEGIN
    EXECUTE 'DROP TRIGGER IF EXISTS trg_derive_tenant ON automation_modes';
    EXECUTE 'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
            'workspace_id, tenant_id ON automation_modes FOR EACH ROW '
            'EXECUTE FUNCTION agentgraph_derive_tenant()';
    EXECUTE 'ALTER TABLE automation_modes ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE automation_modes FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON automation_modes';
    EXECUTE 'CREATE POLICY tenant_isolation ON automation_modes '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON automation_modes TO agentgraph_app';
END $$;
