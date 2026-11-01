-- Kernel v4 016: idempotency key for tasks created by the Agent Runtime.
--
-- A retried Run must not create a second DAG node for the same product task.
-- Nullable: admin-created tasks have no runtime key.

ALTER TABLE dag_tasks ADD COLUMN IF NOT EXISTS runtime_request_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_dag_tasks_runtime_key
    ON dag_tasks (workspace_id, runtime_request_key)
    WHERE runtime_request_key IS NOT NULL;
