-- v3.88 Federation governance basics (task F-10, docs/federation/ICD_I_GOVERNANCE.md).
--
-- A report opens a case and nothing else (F-019). Cases move only by a
-- deployment operator's hand. A sanction is a separate, recorded decision:
-- a temporary freeze can be applied by an operator on a decided case; a
-- permanent network-wide block needs a second, different operator and a
-- closed appeal window, and is never triggered by a count of reports.
-- Local block stays the workspace's own business (federation_grants).

CREATE TABLE IF NOT EXISTS federation_reports (
    report_id VARCHAR(40) PRIMARY KEY CHECK (report_id ~ '^frp_[a-z2-7]{26}$'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    reporter TEXT NOT NULL,
    accused_fid VARCHAR(64) NOT NULL
        CHECK (accused_fid ~ '^fed:(workspace|agent):[a-z2-7]{26}$'),
    category VARCHAR(24) NOT NULL CHECK (category IN (
        'spam', 'abuse', 'fraud', 'impersonation', 'data_leak', 'prompt_injection', 'other')),
    statement TEXT NOT NULL CHECK (char_length(statement) BETWEEN 10 AND 4000),
    -- Digests and references only: message ids, payload hashes, signature
    -- digests. Content stays where it is.
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    case_state VARCHAR(24) NOT NULL DEFAULT 'SUBMITTED' CHECK (case_state IN (
        'SUBMITTED', 'TRIAGED', 'INVESTIGATING', 'INSUFFICIENT_EVIDENCE', 'DISMISSED',
        'ACTION_PROPOSED', 'DECIDED', 'APPEAL_WINDOW', 'CLOSED', 'WITHDRAWN')),
    decision TEXT,
    appeal_until TIMESTAMPTZ,
    appeal_statement TEXT CHECK (appeal_statement IS NULL OR char_length(appeal_statement) <= 4000),
    also_blocked BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_reports_accused ON federation_reports (accused_fid);
CREATE INDEX IF NOT EXISTS idx_federation_reports_state ON federation_reports (case_state);

CREATE TABLE IF NOT EXISTS federation_report_history (
    history_id BIGSERIAL PRIMARY KEY,
    report_id VARCHAR(40) NOT NULL REFERENCES federation_reports(report_id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    from_state VARCHAR(24),
    to_state VARCHAR(24) NOT NULL,
    actor TEXT NOT NULL,
    note TEXT CHECK (note IS NULL OR char_length(note) <= 4000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_report_history ON federation_report_history (report_id, history_id);

-- Deployment-wide decisions about an identity: not workspace data, so not
-- readable by the application role (like the identity tables).
CREATE TABLE IF NOT EXISTS federation_sanctions (
    sanction_id VARCHAR(40) PRIMARY KEY CHECK (sanction_id ~ '^fsn_[a-z2-7]{26}$'),
    subject_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    report_id VARCHAR(40) NOT NULL REFERENCES federation_reports(report_id),
    kind VARCHAR(20) NOT NULL CHECK (kind IN ('temporary_freeze', 'permanent_block')),
    state VARCHAR(12) NOT NULL CHECK (state IN ('proposed', 'active', 'lifted')),
    reason TEXT NOT NULL CHECK (char_length(reason) BETWEEN 5 AND 2000),
    proposed_by TEXT NOT NULL,
    confirmed_by TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at TIMESTAMPTZ,
    lifted_at TIMESTAMPTZ,
    lifted_by TEXT,
    CHECK (kind <> 'temporary_freeze' OR expires_at IS NOT NULL),
    -- Four eyes: a permanent block is confirmed by someone else.
    CHECK (kind <> 'permanent_block' OR state = 'proposed' OR
           (confirmed_by IS NOT NULL AND confirmed_by <> proposed_by))
);
CREATE INDEX IF NOT EXISTS idx_federation_sanctions_subject ON federation_sanctions (subject_fid, state);

CREATE TABLE IF NOT EXISTS federation_security_events (
    event_id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    kind VARCHAR(40) NOT NULL,
    peer_fid VARCHAR(64),
    code VARCHAR(64),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_federation_security_events ON federation_security_events (workspace_id, event_id);

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'federation_reports', 'federation_report_history', 'federation_security_events'
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
    EXECUTE 'REVOKE UPDATE, DELETE ON federation_report_history FROM agentgraph_app';
    EXECUTE 'REVOKE UPDATE, DELETE ON federation_security_events FROM agentgraph_app';
END $$;
GRANT USAGE, SELECT ON SEQUENCE federation_report_history_history_id_seq TO agentgraph_app;
GRANT USAGE, SELECT ON SEQUENCE federation_security_events_event_id_seq TO agentgraph_app;
REVOKE ALL ON federation_sanctions FROM agentgraph_app;
