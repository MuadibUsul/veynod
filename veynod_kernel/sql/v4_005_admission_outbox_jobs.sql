-- ============================================================================
-- Veynod Kernel v4.0 - v4_005 versioned migration
-- Horizontal API scaling, admission control, persistent edge jobs and a
-- transactional notification outbox.
-- ============================================================================

-- One authoritative Rust scheduler core election per workspace.  Every claim
-- still uses the PostgreSQL conditional UPDATE as the durable fence.
CREATE TABLE IF NOT EXISTS scheduler_authorities (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    authority_id VARCHAR(160) NOT NULL,
    fencing_token BIGINT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Durable replacement for the old in-process asyncio.Queue.
CREATE TABLE IF NOT EXISTS edge_rebuild_jobs (
    job_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    repo_path TEXT NOT NULL,
    main_branch VARCHAR(64) NOT NULL,
    new_sha VARCHAR(40) NOT NULL,
    changed_files TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempts INT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_by VARCHAR(160),
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT edge_rebuild_jobs_status_check
        CHECK (status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_edge_rebuild_jobs_queue
    ON edge_rebuild_jobs (available_at, created_at, job_id)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_edge_rebuild_jobs_expired
    ON edge_rebuild_jobs (lease_expires_at)
    WHERE status = 'RUNNING';

-- Notification outbox.  kernel_events remains the durable SSE log; this row
-- makes the post-commit pg_notify delivery retryable and transactional.
CREATE TABLE IF NOT EXISTS event_outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    event_seq BIGINT NOT NULL UNIQUE REFERENCES kernel_events(event_seq) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_unpublished
    ON event_outbox (outbox_id)
    WHERE published_at IS NULL;

INSERT INTO schema_migrations (version)
VALUES ('v4_005_admission_outbox_jobs')
ON CONFLICT (version) DO NOTHING;
