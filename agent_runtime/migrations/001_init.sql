-- Agent Runtime 001: base durable run/step/checkpoint/event/outbox/idempotency.

CREATE TABLE IF NOT EXISTS {{schema}}.agent_runs (
    run_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    product_task_id TEXT,
    workflow_id UUID,
    workflow_version INT,
    status TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    budget JSONB NOT NULL DEFAULT '{}'::JSONB,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS {{schema}}.agent_steps (
    step_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES {{schema}}.agent_runs(run_id)
        ON DELETE CASCADE,
    step_no INT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, step_no),
    UNIQUE (run_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS {{schema}}.run_checkpoints (
    checkpoint_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES {{schema}}.agent_runs(run_id)
        ON DELETE CASCADE,
    version INT NOT NULL,
    state JSONB NOT NULL,
    idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, version),
    UNIQUE (run_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS {{schema}}.run_events (
    event_seq BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES {{schema}}.agent_runs(run_id)
        ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {{schema}}.runtime_outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    event_seq BIGINT NOT NULL UNIQUE
        REFERENCES {{schema}}.run_events(event_seq) ON DELETE CASCADE,
    run_id UUID NOT NULL,
    payload JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {{schema}}.runtime_idempotency (
    key TEXT PRIMARY KEY,
    run_id UUID NOT NULL,
    command TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runtime_runs_workspace
    ON {{schema}}.agent_runs (workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_events_run
    ON {{schema}}.run_events (run_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_runtime_outbox_unpublished
    ON {{schema}}.runtime_outbox (outbox_id) WHERE published_at IS NULL;
