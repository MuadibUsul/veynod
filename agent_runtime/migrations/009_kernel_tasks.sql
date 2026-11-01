-- Agent Runtime 009: the Kernel DAG node each Run created for its product task.
--
-- A Run must lease the node it created, never "any orthogonal task": two
-- product tasks in one repository would otherwise be able to take each
-- other's nodes.  One row per Run (a split creates children the Run leases
-- explicitly by id).

CREATE TABLE IF NOT EXISTS {{schema}}.kernel_tasks (
    run_id UUID PRIMARY KEY REFERENCES {{schema}}.agent_runs(run_id)
        ON DELETE CASCADE,
    kernel_task_id TEXT NOT NULL,
    kernel_workspace_id TEXT NOT NULL,
    request_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runtime_kernel_tasks_task
    ON {{schema}}.kernel_tasks (kernel_task_id);
