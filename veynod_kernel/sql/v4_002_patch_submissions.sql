-- ============================================================================
-- Veynod Kernel v4.0 - v4_002 versioned migration
-- Durable asynchronous patch submissions.
--
-- The HTTP accept path only validates the lease/write-set and inserts an
-- immutable submission row.  pytest/Git run later in a background worker.
-- Idempotency is bound to (task_id, lease_token_hash, patch_sha256) and to a
-- unique idempotency key, so a duplicate request can never create a second
-- visible result and a different patch can never reuse a key.
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS patch_submissions (
    submission_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES dag_tasks(task_id) ON DELETE CASCADE,
    runner_id VARCHAR(128) NOT NULL,
    lease_token_hash VARCHAR(64) NOT NULL,
    idempotency_key TEXT NOT NULL,
    patch_sha256 CHAR(64) NOT NULL,
    git_diff TEXT NOT NULL,
    touched_symbols TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    local_test_passed BOOLEAN NOT NULL,
    lease_expires_at TIMESTAMPTZ,
    status VARCHAR(16) NOT NULL DEFAULT 'QUEUED',
    attempts INT NOT NULL DEFAULT 0,
    task_status VARCHAR(32),
    result_commit_sha VARCHAR(40),
    merge_mode VARCHAR(32),
    gate_results JSONB NOT NULL DEFAULT '[]'::JSONB,
    merge_timing JSONB NOT NULL DEFAULT '{}'::JSONB,
    unlocked_downstream_task_ids UUID[] NOT NULL DEFAULT '{}'::UUID[],
    error_code VARCHAR(64),
    error_detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT patch_submissions_status_check
        CHECK (status IN ('QUEUED', 'RUNNING', 'COMMITTED', 'FAILED')),
    CONSTRAINT patch_submissions_patch_sha_check
        CHECK (patch_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT patch_submissions_idempotency_unique
        UNIQUE (idempotency_key),
    -- A retry with a different opaque key but the same task/lease/patch is
    -- still the same submission.  A new lease gets a new row.
    CONSTRAINT patch_submissions_binding_unique
        UNIQUE (task_id, lease_token_hash, patch_sha256)
);

CREATE INDEX IF NOT EXISTS idx_patch_submissions_queue
    ON patch_submissions (created_at, submission_id)
    WHERE status = 'QUEUED';
CREATE INDEX IF NOT EXISTS idx_patch_submissions_task
    ON patch_submissions (task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_patch_submissions_status
    ON patch_submissions (status, updated_at);

-- The patch payload and identity are immutable after INSERT.  Status/result
-- columns remain mutable so the worker can finalize the row.
CREATE OR REPLACE FUNCTION patch_submissions_immutable_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.runner_id IS DISTINCT FROM OLD.runner_id
       OR NEW.lease_token_hash IS DISTINCT FROM OLD.lease_token_hash
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.patch_sha256 IS DISTINCT FROM OLD.patch_sha256
       OR NEW.git_diff IS DISTINCT FROM OLD.git_diff
       OR NEW.touched_symbols IS DISTINCT FROM OLD.touched_symbols
       OR NEW.local_test_passed IS DISTINCT FROM OLD.local_test_passed
       OR NEW.lease_expires_at IS DISTINCT FROM OLD.lease_expires_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'patch_submissions immutable identity/patch fields cannot be updated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS patch_submissions_immutable ON patch_submissions;
CREATE TRIGGER patch_submissions_immutable
    BEFORE UPDATE ON patch_submissions
    FOR EACH ROW EXECUTE FUNCTION patch_submissions_immutable_guard();

INSERT INTO schema_migrations (version)
VALUES ('v4_002_patch_submissions')
ON CONFLICT (version) DO NOTHING;
