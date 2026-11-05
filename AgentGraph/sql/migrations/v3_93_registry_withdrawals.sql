-- v3.93 Skill registry: withdrawal and notices (task P3-4,
-- docs/federation/ICD_P3_REGISTRY.md §11).
--
-- A version is withdrawn by its publisher (yank) or by the platform's
-- operators (revoke). Either way the record and digest stay; every workspace
-- that has it pinned gets a notice, and so does every publisher whose own
-- skill depends on it.

ALTER TABLE registry_versions
    ADD COLUMN IF NOT EXISTS yank_source VARCHAR(12);
ALTER TABLE registry_versions DROP CONSTRAINT IF EXISTS registry_versions_yank_source_check;
ALTER TABLE registry_versions ADD CONSTRAINT registry_versions_yank_source_check
    CHECK (yank_source IS NULL OR yank_source IN ('publisher', 'platform'));
UPDATE registry_versions SET yank_source = 'publisher'
 WHERE state = 'yanked' AND yank_source IS NULL;

CREATE TABLE IF NOT EXISTS registry_notices (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    kind VARCHAR(20) NOT NULL
        CHECK (kind IN ('withdrawn', 'dependency_withdrawn')),
    source VARCHAR(12) NOT NULL CHECK (source IN ('publisher', 'platform')),
    name VARCHAR(64) NOT NULL,
    version VARCHAR(32) NOT NULL,
    digest VARCHAR(71) NOT NULL REFERENCES registry_versions(digest),
    reason TEXT NOT NULL,
    -- How this workspace is affected: its pin, or its own published skill.
    affected JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT,
    UNIQUE (workspace_id, digest, kind, source)
);
CREATE INDEX IF NOT EXISTS idx_registry_notices_open
    ON registry_notices (workspace_id, created_at DESC) WHERE acknowledged_at IS NULL;

DROP TRIGGER IF EXISTS trg_derive_tenant ON registry_notices;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON registry_notices FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE registry_notices ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry_notices FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON registry_notices;
CREATE POLICY tenant_isolation ON registry_notices
    USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON registry_notices TO agentgraph_app;
GRANT USAGE ON SEQUENCE registry_notices_id_seq TO agentgraph_app;
