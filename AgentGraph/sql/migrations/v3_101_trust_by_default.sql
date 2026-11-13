-- v3.101 Trust by default (docs/LOOP_SIMPLE_MODEL.md, steps 1-2).
--
-- A hand-off under full automation ends the task. What a person or an AUDITOR
-- should still look at is a flag on the finished task with the reasons, not a
-- state that holds everything behind it.
--
-- Expand-only and idempotent.

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS needs_attention BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS attention_reasons JSONB;

CREATE INDEX IF NOT EXISTS idx_collab_tasks_attention
    ON collab_tasks (workspace_id, updated_at DESC) WHERE needs_attention;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('v3_101_trust_by_default', NOW())
ON CONFLICT (version) DO NOTHING;
