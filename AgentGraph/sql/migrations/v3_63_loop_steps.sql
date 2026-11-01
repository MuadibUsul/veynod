-- v3.63 Loop steps (loop-review-merge-placement, loop-supervisor).
--
-- A placement is no longer only "do this task". The scheduler also places the
-- review of delivered work (gate and review on a reviewer computer that did
-- not build it), the merge of approved diagram chunks, and the supervisor's
-- rounds over what is stuck. A supervise round belongs to no task. A runner
-- reports how a step ended (finished or failed); the scheduler moves a step
-- whose report says it failed, or that ran out of time.
--
-- Diagrams can name the repository their chunks change; chunk tasks carry it,
-- so a runner knows what to check out.

ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS step VARCHAR(12) NOT NULL DEFAULT 'work';
ALTER TABLE runner_placements DROP CONSTRAINT IF EXISTS runner_placements_step_check;
ALTER TABLE runner_placements ADD CONSTRAINT runner_placements_step_check
    CHECK (step IN ('work', 'review', 'merge', 'supervise'));
ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS diagram_key VARCHAR(64);
ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS chunk_key VARCHAR(128);
ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS detail JSONB;
ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS reported VARCHAR(16)
    CHECK (reported IN ('finished', 'failed'));
ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS reported_at TIMESTAMPTZ;
ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS report_note VARCHAR(500);
ALTER TABLE runner_placements ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE runner_placements ALTER COLUMN task_key DROP NOT NULL;

-- At most one open supervise round per workspace.
CREATE UNIQUE INDEX IF NOT EXISTS uq_runner_placements_open_round
    ON runner_placements (workspace_id)
    WHERE step = 'supervise' AND status IN ('suggested', 'offered', 'claimed', 'unplaced');
CREATE INDEX IF NOT EXISTS idx_runner_placements_step
    ON runner_placements (workspace_id, step, created_at DESC);

ALTER TABLE diagrams ADD COLUMN IF NOT EXISTS repo VARCHAR(256);
