-- Agent Runtime 003: run execution leases (one worker per run).

ALTER TABLE {{schema}}.agent_runs
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_token_hash TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_runtime_runs_claimable
    ON {{schema}}.agent_runs (updated_at)
    WHERE status = 'RUNNING';
