-- Agent Runtime 002: usage accounting and model/tool invocations.

ALTER TABLE {{schema}}.agent_runs
    ADD COLUMN IF NOT EXISTS usage JSONB NOT NULL DEFAULT '{}'::JSONB;

CREATE TABLE IF NOT EXISTS {{schema}}.invocations (
    invocation_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES {{schema}}.agent_runs(run_id)
        ON DELETE CASCADE,
    step_id UUID REFERENCES {{schema}}.agent_steps(step_id)
        ON DELETE SET NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request JSONB NOT NULL DEFAULT '{}'::JSONB,
    status TEXT NOT NULL DEFAULT 'PENDING',
    result JSONB NOT NULL DEFAULT '{}'::JSONB,
    error TEXT,
    usage JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_runtime_invocations_run
    ON {{schema}}.invocations (run_id, created_at DESC);
