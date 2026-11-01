-- Agent Runtime 008: durable external waits, kernel wakeups and tool audit.
--
-- A tool call that becomes asynchronous in the Kernel (submit_patch -> 202) is
-- persisted here, the Run moves to WAITING_EXTERNAL and the worker releases its
-- lease.  Kernel events are fanned into runtime_wakeups by the watcher; the next
-- claim applies them idempotently.

ALTER TABLE {{schema}}.agent_runs
    ADD COLUMN IF NOT EXISTS project_id TEXT,
    ADD COLUMN IF NOT EXISTS repo_path TEXT,
    ADD COLUMN IF NOT EXISTS trace_id TEXT,
    ADD COLUMN IF NOT EXISTS kernel JSONB NOT NULL DEFAULT '{}'::JSONB;

CREATE TABLE IF NOT EXISTS {{schema}}.external_invocations (
    external_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES {{schema}}.agent_runs(run_id)
        ON DELETE CASCADE,
    step_id UUID,
    tool TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    submission_id TEXT,
    kernel_workspace_id TEXT,
    kernel_task_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    request JSONB NOT NULL DEFAULT '{}'::JSONB,
    result JSONB NOT NULL DEFAULT '{}'::JSONB,
    error TEXT,
    deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_runtime_external_pending
    ON {{schema}}.external_invocations (deadline)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_runtime_external_submission
    ON {{schema}}.external_invocations (submission_id);

CREATE TABLE IF NOT EXISTS {{schema}}.runtime_wakeups (
    wakeup_id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    kernel_event_seq BIGINT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (kind, ref_id, kernel_event_seq)
);

CREATE INDEX IF NOT EXISTS idx_runtime_wakeups_unconsumed
    ON {{schema}}.runtime_wakeups (wakeup_id) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS {{schema}}.runtime_wakeup_cursors (
    source_instance TEXT PRIMARY KEY,
    last_event_seq BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {{schema}}.runtime_tool_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    run_id UUID,
    tool TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms INT NOT NULL DEFAULT 0,
    error_class TEXT,
    kernel_status INT,
    correlation JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runtime_tool_audit_run
    ON {{schema}}.runtime_tool_audit (run_id, created_at DESC);

-- Kernel lease tokens never enter model context, step payloads or logs: they
-- live here, scoped to a Run, and expire with the Kernel lease.
CREATE TABLE IF NOT EXISTS {{schema}}.kernel_leases (
    run_id UUID NOT NULL REFERENCES {{schema}}.agent_runs(run_id)
        ON DELETE CASCADE,
    kernel_task_id TEXT NOT NULL,
    kernel_workspace_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, kernel_task_id)
);
