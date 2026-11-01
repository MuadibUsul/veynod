-- ============================================================================
-- Veynod Kernel v4.0 - v4_003 versioned migration
-- Independent gate workers: time-limited work leases, reclaim, backoff.
--
-- patch_submissions remains the durable queue.  A worker claims a row with a
-- random work_lease_id; result writes are fenced by status='RUNNING' AND the
-- same work_lease_id, so a stale worker can never overwrite a newer result.
-- ============================================================================

ALTER TABLE patch_submissions
    ADD COLUMN IF NOT EXISTS work_lease_id UUID,
    ADD COLUMN IF NOT EXISTS work_owner VARCHAR(128),
    ADD COLUMN IF NOT EXISTS work_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS last_worker_error TEXT;

CREATE INDEX IF NOT EXISTS idx_patch_submissions_work_queue
    ON patch_submissions (available_at, created_at, submission_id)
    WHERE status = 'QUEUED';

CREATE INDEX IF NOT EXISTS idx_patch_submissions_expired_work
    ON patch_submissions (work_lease_expires_at)
    WHERE status = 'RUNNING';

INSERT INTO schema_migrations (version)
VALUES ('v4_003_gate_work_leases')
ON CONFLICT (version) DO NOTHING;
