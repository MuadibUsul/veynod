-- v3.50 VPS deploy gate (agentgraph/spec-vps-auto-deploy-gate v1).
--
-- A gate on the production VPS deploys merged main commits without GitHub
-- Actions: a signed GitHub webhook or the VPS timer queues a run, the runner
-- checks it (G-01..G-04), deploys with scripts/deploy_tlines.sh and smoke-tests
-- the result, rolling the image back when the smoke test fails.
--
-- All three tables are operator-only: RLS is forced with no policy for the
-- application role, so tenant-scoped sessions read and write nothing; the
-- service layer uses unscoped sessions for them.
--
-- * deploy_gate_state  one row: paused, auto-deploy authorization, the runner's
--                      last heartbeat and the commit it last deployed.
-- * deploy_gate_runs   one row per trigger: commit, check results, deploy and
--                      rollback outcome, and the plain-language owner report.
-- * deploy_gate_audit  append-only record of every trigger, control change and
--                      run outcome.

CREATE TABLE IF NOT EXISTS deploy_gate_state (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    paused_reason TEXT,
    auto_deploy_authorized BOOLEAN NOT NULL DEFAULT FALSE,
    authorized_by VARCHAR(128),
    authorized_at TIMESTAMPTZ,
    deployed_commit VARCHAR(40),
    deployed_at TIMESTAMPTZ,
    runner_host VARCHAR(128),
    runner_version VARCHAR(32),
    runner_last_seen_at TIMESTAMPTZ,
    updated_by VARCHAR(128),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO deploy_gate_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS deploy_gate_runs (
    id VARCHAR(40) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trigger_kind VARCHAR(16) NOT NULL
        CHECK (trigger_kind IN ('webhook', 'poll', 'manual')),
    triggered_by VARCHAR(128) NOT NULL,
    delivery_id VARCHAR(80) UNIQUE,
    commit_sha VARCHAR(40),
    pr_number INTEGER,
    status VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'skipped', 'blocked', 'deployed',
                          'rolled_back', 'failed')),
    status_reason TEXT,
    checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    deploy JSONB NOT NULL DEFAULT '{}'::jsonb,
    rolled_back BOOLEAN NOT NULL DEFAULT FALSE,
    report JSONB NOT NULL DEFAULT '{}'::jsonb,
    log_ref TEXT,
    claimed_by VARCHAR(128),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_deploy_gate_runs_created ON deploy_gate_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deploy_gate_runs_queue
    ON deploy_gate_runs (created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_deploy_gate_runs_commit ON deploy_gate_runs (commit_sha);

CREATE TABLE IF NOT EXISTS deploy_gate_audit (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor VARCHAR(128) NOT NULL,
    action VARCHAR(32) NOT NULL,
    run_id VARCHAR(40),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_deploy_gate_audit_at ON deploy_gate_audit (at DESC);
CREATE INDEX IF NOT EXISTS idx_deploy_gate_audit_run ON deploy_gate_audit (run_id)
    WHERE run_id IS NOT NULL;

CREATE OR REPLACE FUNCTION agentgraph_deploy_gate_audit_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'deploy gate audit is append-only' USING ERRCODE = 'insufficient_privilege';
END;
$$;
DROP TRIGGER IF EXISTS trg_deploy_gate_audit_append_only ON deploy_gate_audit;
CREATE TRIGGER trg_deploy_gate_audit_append_only BEFORE UPDATE OR DELETE ON deploy_gate_audit
    FOR EACH ROW EXECUTE FUNCTION agentgraph_deploy_gate_audit_append_only();

ALTER TABLE deploy_gate_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE deploy_gate_state FORCE ROW LEVEL SECURITY;
ALTER TABLE deploy_gate_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE deploy_gate_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE deploy_gate_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE deploy_gate_audit FORCE ROW LEVEL SECURITY;
