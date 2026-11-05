-- v3.92 Skill registry: sandbox check, updates and rollback (task P3-3,
-- docs/federation/ICD_P3_REGISTRY.md §10).
--
-- Every version gets a sandbox report from the platform's static checker; a
-- failed one cannot be published or installed. An installing workspace says
-- whether it follows upstream updates or stays locked, and every change to a
-- pin is kept in an append-only history so a version can be restored.

CREATE TABLE IF NOT EXISTS registry_sandbox_reports (
    digest VARCHAR(71) PRIMARY KEY REFERENCES registry_versions(digest),
    checker VARCHAR(16) NOT NULL,
    verdict VARCHAR(8) NOT NULL CHECK (verdict IN ('passed', 'failed')),
    report JSONB NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
REVOKE ALL ON registry_sandbox_reports FROM agentgraph_app;

ALTER TABLE registry_installs
    ADD COLUMN IF NOT EXISTS update_policy VARCHAR(16) NOT NULL DEFAULT 'locked';
ALTER TABLE registry_installs DROP CONSTRAINT IF EXISTS registry_installs_update_policy_check;
ALTER TABLE registry_installs ADD CONSTRAINT registry_installs_update_policy_check
    CHECK (update_policy IN ('locked', 'follow_patch', 'follow_minor'));

CREATE TABLE IF NOT EXISTS registry_install_history (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    name VARCHAR(64) NOT NULL,
    action VARCHAR(12) NOT NULL
        CHECK (action IN ('install', 'update', 'follow', 'rollback', 'uninstall', 'policy')),
    from_version VARCHAR(32),
    version VARCHAR(32),
    digest VARCHAR(71) REFERENCES registry_versions(digest),
    accepted_permissions TEXT[] NOT NULL DEFAULT '{}',
    update_policy VARCHAR(16),
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_registry_install_history
    ON registry_install_history (workspace_id, name, id DESC);

INSERT INTO registry_install_history
    (workspace_id, tenant_id, name, action, version, digest, accepted_permissions,
     update_policy, actor, created_at)
SELECT i.workspace_id, i.tenant_id, i.name, 'install', i.version, i.digest,
       i.accepted_permissions, i.update_policy, i.installed_by, i.installed_at
  FROM registry_installs i
 WHERE NOT EXISTS (SELECT 1 FROM registry_install_history h
                    WHERE h.workspace_id = i.workspace_id AND h.name = i.name);

CREATE OR REPLACE FUNCTION registry_install_history_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'install history is append-only' USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_registry_install_history_guard ON registry_install_history;
CREATE TRIGGER trg_registry_install_history_guard
    BEFORE UPDATE ON registry_install_history
    FOR EACH ROW EXECUTE FUNCTION registry_install_history_guard();

DROP TRIGGER IF EXISTS trg_derive_tenant ON registry_install_history;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON registry_install_history FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE registry_install_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry_install_history FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON registry_install_history;
CREATE POLICY tenant_isolation ON registry_install_history
    USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT ON registry_install_history TO agentgraph_app;
GRANT USAGE ON SEQUENCE registry_install_history_id_seq TO agentgraph_app;
