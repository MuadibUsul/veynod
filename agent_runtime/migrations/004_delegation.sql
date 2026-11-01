-- Agent Runtime 004: parent/child runs for delegation.

ALTER TABLE {{schema}}.agent_runs
    ADD COLUMN IF NOT EXISTS parent_run_id UUID
    REFERENCES {{schema}}.agent_runs(run_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_runtime_runs_parent
    ON {{schema}}.agent_runs (parent_run_id, created_at);
