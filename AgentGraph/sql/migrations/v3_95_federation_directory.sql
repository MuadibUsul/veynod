-- v3.95 Federation directory (task P4-2, docs/federation/ICD_P4_REPUTATION.md §8).
--
-- A workspace appears in the directory only when its owner lists it. The
-- listing holds what the owner chooses to say (headline, capability tags,
-- availability); everything else on an entry is computed from signed
-- proofs and platform measurements, never from task or chat content.

CREATE TABLE IF NOT EXISTS federation_directory_listings (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    federation_id VARCHAR(64) NOT NULL UNIQUE REFERENCES federation_identities(federation_id),
    listed BOOLEAN NOT NULL DEFAULT FALSE,
    headline TEXT NOT NULL DEFAULT '' CHECK (char_length(headline) <= 200),
    capabilities TEXT[] NOT NULL DEFAULT '{}' CHECK (cardinality(capabilities) <= 10),
    availability VARCHAR(8) NOT NULL DEFAULT 'open'
        CHECK (availability IN ('open', 'busy', 'closed')),
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_directory_listed
    ON federation_directory_listings (listed, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_federation_directory_caps
    ON federation_directory_listings USING GIN (capabilities);

DROP TRIGGER IF EXISTS trg_derive_tenant ON federation_directory_listings;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON federation_directory_listings FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE federation_directory_listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE federation_directory_listings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON federation_directory_listings;
CREATE POLICY tenant_isolation ON federation_directory_listings
    USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON federation_directory_listings TO agentgraph_app;
