-- v3.84 Federation delegations (federation diagram chunk F-01d, task bridge part).
--
-- Contract: docs/federation/ICD_I_BRIDGE.md. Each side keeps its own copy of a
-- delegation (requester or provider), an append-only history whose version
-- goes up by exactly one per transition, and the cost receipts the provider
-- reports (no settlement in P1). Tenant tables, RLS (ADR-011). The blocks,
-- reports and security-event tables of F-01d come with F-10 (governance).

CREATE TABLE IF NOT EXISTS federation_delegations (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    federation_job_id VARCHAR(40) NOT NULL CHECK (federation_job_id ~ '^fjob_[a-z2-7]{26}$'),
    peer_workspace_fid VARCHAR(64) NOT NULL
        CHECK (peer_workspace_fid ~ '^fed:workspace:[a-z2-7]{26}$'),
    connection_id VARCHAR(40) NOT NULL,
    role VARCHAR(12) NOT NULL CHECK (role IN ('requester', 'provider')),
    state VARCHAR(24) NOT NULL CHECK (state IN (
        'OFFERED', 'ACCEPTED', 'RUNNING', 'SUBMITTED', 'ACCEPTED_RESULT', 'REJECTED',
        'EXPIRED', 'CANCEL_REQUESTED', 'CANCELLED', 'STOP_NOT_CONFIRMED', 'FAILED',
        'DISPUTED')),
    version INTEGER NOT NULL CHECK (version >= 1),
    terms JSONB NOT NULL,
    acceptance_hash VARCHAR(64) NOT NULL,
    -- This side's own task for the work: the requester's optional tracking
    -- task, the provider's task created on acceptance. A local key only; it
    -- never travels in an envelope (BRIDGE-INV-2, ID-INV-8).
    local_task_key VARCHAR(128),
    result JSONB,
    revisions INTEGER NOT NULL DEFAULT 0 CHECK (revisions >= 0),
    -- Signed envelopes this side committed but has not delivered yet, in order
    -- (ICD_I_BRIDGE §5): re-sent by sync, never re-signed.
    outbox JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(outbox) = 'array'),
    offer_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- BRIDGE-INV-3: one delegation per peer and job id on each side.
    PRIMARY KEY (workspace_id, peer_workspace_fid, federation_job_id),
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES federation_grants (workspace_id, connection_id)
);
CREATE INDEX IF NOT EXISTS idx_federation_delegations_state
    ON federation_delegations (workspace_id, state, updated_at DESC);
-- A provider task is linked to at most one delegation.
CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_delegations_task
    ON federation_delegations (workspace_id, local_task_key) WHERE local_task_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS federation_delegation_history (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    peer_workspace_fid VARCHAR(64) NOT NULL,
    federation_job_id VARCHAR(40) NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    action VARCHAR(24) NOT NULL,
    from_state VARCHAR(24),
    to_state VARCHAR(24) NOT NULL,
    actor TEXT NOT NULL CHECK (actor ~ '^(user|agent|peer):.+$' OR actor = 'system'),
    envelope_sha256 VARCHAR(64),
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, peer_workspace_fid, federation_job_id, version),
    FOREIGN KEY (workspace_id, peer_workspace_fid, federation_job_id)
        REFERENCES federation_delegations (workspace_id, peer_workspace_fid, federation_job_id)
);

CREATE OR REPLACE FUNCTION federation_delegation_history_guard() RETURNS trigger AS $$
DECLARE
    latest INTEGER;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'federation delegation history is append-only'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT max(version) INTO latest FROM federation_delegation_history
     WHERE workspace_id = NEW.workspace_id AND peer_workspace_fid = NEW.peer_workspace_fid
       AND federation_job_id = NEW.federation_job_id;
    IF NEW.version <> COALESCE(latest, 0) + 1 THEN
        RAISE EXCEPTION 'delegation version must be % (got %)', COALESCE(latest, 0) + 1, NEW.version
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_federation_delegation_history_guard ON federation_delegation_history;
CREATE TRIGGER trg_federation_delegation_history_guard
    BEFORE INSERT OR UPDATE OR DELETE ON federation_delegation_history
    FOR EACH ROW EXECUTE FUNCTION federation_delegation_history_guard();

CREATE TABLE IF NOT EXISTS federation_cost_receipts (
    receipt_row_id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    peer_workspace_fid VARCHAR(64) NOT NULL,
    federation_job_id VARCHAR(40) NOT NULL,
    result_version INTEGER NOT NULL,
    -- ICD_I_BRIDGE §8: who measured it, and how sure; self-reported usage is
    -- never shown as measured.
    metering_source VARCHAR(16) NOT NULL
        CHECK (metering_source IN ('platform', 'adapter', 'self_reported')),
    confidence VARCHAR(8) NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    price_snapshot_id TEXT,
    proof_ref TEXT,
    disputed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, peer_workspace_fid, federation_job_id, result_version),
    FOREIGN KEY (workspace_id, peer_workspace_fid, federation_job_id)
        REFERENCES federation_delegations (workspace_id, peer_workspace_fid, federation_job_id)
);

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'federation_delegations', 'federation_delegation_history', 'federation_cost_receipts'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', t);
        EXECUTE format('CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
                       'workspace_id, tenant_id ON %I FOR EACH ROW '
                       'EXECUTE FUNCTION agentgraph_derive_tenant()', t);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                       'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
                       'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
                       'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
                       'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO agentgraph_app', t);
    END LOOP;
    EXECUTE 'REVOKE UPDATE, DELETE ON federation_delegation_history FROM agentgraph_app';
END $$;

GRANT USAGE, SELECT ON SEQUENCE federation_cost_receipts_receipt_row_id_seq TO agentgraph_app;
