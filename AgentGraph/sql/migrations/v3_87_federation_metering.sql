-- v3.87 Federation metering (task F-09, docs/federation/ICD_I_BRIDGE.md §8).
--
-- Usage events on the provider's side of a delegation: a reservation before
-- a run starts (refused past an enforced cap) and each recorded use, with who
-- measured it. These are usage records, never money: no balance, wallet,
-- card or escrow exists anywhere in federation (ADR-005).

CREATE TABLE IF NOT EXISTS federation_usage_events (
    event_id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    peer_workspace_fid VARCHAR(64) NOT NULL,
    federation_job_id VARCHAR(40) NOT NULL,
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('reserve', 'record')),
    unit VARCHAR(16) NOT NULL CHECK (unit IN ('model_tokens', 'tool_calls', 'minutes')),
    amount BIGINT NOT NULL CHECK (amount > 0 AND amount <= 1000000000000),
    -- Who measured it. A reservation measures nothing.
    metering_source VARCHAR(16)
        CHECK (metering_source IN ('platform', 'adapter', 'self_reported')),
    proof_ref TEXT CHECK (proof_ref IS NULL OR char_length(proof_ref) <= 500),
    price_snapshot_id TEXT CHECK (price_snapshot_id IS NULL OR char_length(price_snapshot_id) <= 200),
    result_version INTEGER,
    recorded_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((kind = 'record') = (metering_source IS NOT NULL)),
    FOREIGN KEY (workspace_id, peer_workspace_fid, federation_job_id)
        REFERENCES federation_delegations (workspace_id, peer_workspace_fid, federation_job_id)
);
CREATE INDEX IF NOT EXISTS idx_federation_usage_events_job
    ON federation_usage_events (workspace_id, peer_workspace_fid, federation_job_id, event_id);

DROP TRIGGER IF EXISTS trg_derive_tenant ON federation_usage_events;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON federation_usage_events FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE federation_usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE federation_usage_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON federation_usage_events;
CREATE POLICY tenant_isolation ON federation_usage_events
    USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid);
-- Append-only: a usage record is never edited or removed.
GRANT SELECT, INSERT ON federation_usage_events TO agentgraph_app;
REVOKE UPDATE, DELETE ON federation_usage_events FROM agentgraph_app;
GRANT USAGE, SELECT ON SEQUENCE federation_usage_events_event_id_seq TO agentgraph_app;
