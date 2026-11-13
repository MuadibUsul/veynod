-- v3.102 Acceptance checks (docs/LOOP_SIMPLE_MODEL.md, step 4).
--
-- An AUTO acceptance item may carry a check the platform can run instead of a
-- person: a document that must exist, a quality gate that must have passed, or
-- a command the computer that did the work runs after the agent's turn. The
-- results live on the task; a failed one flags the task for a look.
--
-- Expand-only and idempotent.

ALTER TABLE collab_tasks ADD COLUMN IF NOT EXISTS check_results JSONB;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('v3_102_acceptance_checks', NOW())
ON CONFLICT (version) DO NOTHING;
