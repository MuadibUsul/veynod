-- v3.94 Fulfilment proofs (task P4-1, docs/federation/ICD_P4_REPUTATION.md).
--
-- When delegated work that a provider took on ends, the requester's
-- federation key signs a proof of how it ended: no content, only ids,
-- hashes, times and the outcome. Reputation is computed from these proofs
-- and anyone can re-verify each one. Proofs are never edited or deleted.

CREATE TABLE IF NOT EXISTS federation_proofs (
    proof_id VARCHAR(40) PRIMARY KEY,
    requester_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    provider_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    federation_job_id VARCHAR(40) NOT NULL,
    outcome VARCHAR(24) NOT NULL CHECK (outcome IN (
        'accepted', 'disputed', 'cancelled', 'stop_not_confirmed', 'failed')),
    closed_at TIMESTAMPTZ NOT NULL,
    proof JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (requester_fid, provider_fid, federation_job_id),
    CHECK (requester_fid <> provider_fid)
);
CREATE INDEX IF NOT EXISTS idx_federation_proofs_provider
    ON federation_proofs (provider_fid, closed_at DESC);

CREATE OR REPLACE FUNCTION federation_proofs_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'fulfilment proofs are never changed or deleted'
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_federation_proofs_guard ON federation_proofs;
CREATE TRIGGER trg_federation_proofs_guard
    BEFORE UPDATE OR DELETE ON federation_proofs
    FOR EACH ROW EXECUTE FUNCTION federation_proofs_guard();

REVOKE ALL ON federation_proofs FROM agentgraph_app;
