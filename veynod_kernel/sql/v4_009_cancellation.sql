-- v4_009: task cancellation
--
-- A cancelled task is not a failed task: it must never be retried or merged.
-- The enum value is added here; application code only uses it after this
-- migration has committed.  Do not use the new value in this file (PostgreSQL
-- forbids using a freshly added enum value in the same transaction).

ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'CANCELLED';

ALTER TABLE dag_tasks ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE dag_tasks ADD COLUMN IF NOT EXISTS cancel_reason TEXT;

INSERT INTO schema_migrations (version)
VALUES ('v4_009_cancellation')
ON CONFLICT (version) DO NOTHING;
