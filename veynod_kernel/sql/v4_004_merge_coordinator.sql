-- ============================================================================
-- Veynod Kernel v4.0 - v4_004 versioned migration
-- Per-repository merge coordinator with persistent intents/results and fencing.
--
-- Gate workers run pytest/Git staging in parallel and persist a verified task
-- commit as a merge intent.  One elected coordinator per workspace performs
-- the Git update-ref CAS and the DB finalization in order, recording enough
-- state to reconcile either crash window on startup.
-- ============================================================================

CREATE TABLE IF NOT EXISTS merge_coordinators (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    coordinator_id VARCHAR(160) NOT NULL,
    fencing_token BIGINT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merge_intents (
    merge_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES dag_tasks(task_id) ON DELETE CASCADE,
    submission_id UUID NOT NULL REFERENCES patch_submissions(submission_id) ON DELETE CASCADE,
    base_sha VARCHAR(40) NOT NULL,
    task_commit_sha VARCHAR(40) NOT NULL,
    lease_token_hash VARCHAR(64) NOT NULL,
    changed_files TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    gate_results JSONB NOT NULL DEFAULT '[]'::JSONB,
    pool_wait_ms INT NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    coordinator_id VARCHAR(160),
    coordinator_token BIGINT,
    merged_commit_sha VARCHAR(40),
    merge_mode VARCHAR(32),
    merge_timing JSONB NOT NULL DEFAULT '{}'::JSONB,
    unlocked_downstream_task_ids UUID[] NOT NULL DEFAULT '{}'::UUID[],
    task_status VARCHAR(32),
    error_code VARCHAR(64),
    error_detail TEXT,
    revalidations INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT merge_intents_status_check
        CHECK (status IN ('PENDING', 'RUNNING', 'MERGED', 'REJECTED', 'CONFLICT', 'REVALIDATE')),
    CONSTRAINT merge_intents_submission_unique UNIQUE (submission_id)
);

CREATE INDEX IF NOT EXISTS idx_merge_intents_queue
    ON merge_intents (workspace_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_merge_intents_task
    ON merge_intents (task_id, created_at DESC);

INSERT INTO schema_migrations (version)
VALUES ('v4_004_merge_coordinator')
ON CONFLICT (version) DO NOTHING;
