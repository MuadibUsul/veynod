-- Agent Runtime 005: delegation idempotency key.

ALTER TABLE {{schema}}.agent_runs
    ADD COLUMN IF NOT EXISTS delegation_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_runs_delegation_key
    ON {{schema}}.agent_runs (parent_run_id, delegation_key)
    WHERE delegation_key IS NOT NULL;
