-- v3.66 A diagram's own rules (console flow redesign, step 4).
--
-- The diagram is the plan, so how far the Loop may go on its own is set per
-- diagram: policy.mode overrides the workspace's automation mode (manual,
-- semi, full) for that diagram's chunk tasks; no mode means the workspace's.
-- Only people set it (console or REST); agents read it.

ALTER TABLE diagrams ADD COLUMN IF NOT EXISTS policy JSONB NOT NULL DEFAULT '{}'::jsonb;
