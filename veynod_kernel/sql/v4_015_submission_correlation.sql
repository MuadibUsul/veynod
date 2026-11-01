-- Kernel v4 015: correlation metadata on patch submissions.
--
-- The Agent Runtime submits patches from inside a Run; the correlation carries
-- tenant/workspace/project/product_task/run/trace so a gate result can be
-- traced back to exactly one Agent Run.  Nullable/defaulted: old rows and old
-- clients are unaffected.

ALTER TABLE patch_submissions
    ADD COLUMN IF NOT EXISTS correlation JSONB NOT NULL DEFAULT '{}'::JSONB;

CREATE INDEX IF NOT EXISTS idx_patch_submissions_correlation_trace
    ON patch_submissions ((correlation->>'trace_id'))
    WHERE correlation ? 'trace_id';
