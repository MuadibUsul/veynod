-- v4_008: active write-set overlap indexes
--
-- Claim validation runs three GIN overlap probes against the live
-- LEASED/VERIFYING set.  The partial GIN indexes keep those probes small as
-- the task table grows; the btree helps the active-status filter.

CREATE INDEX IF NOT EXISTS idx_dag_tasks_active_write_locks
    ON dag_tasks USING GIN (write_locks)
    WHERE status IN ('LEASED', 'VERIFYING');

CREATE INDEX IF NOT EXISTS idx_dag_tasks_active_ancestor_locks
    ON dag_tasks USING GIN (ancestor_locks)
    WHERE status IN ('LEASED', 'VERIFYING');

CREATE INDEX IF NOT EXISTS idx_dag_tasks_workspace_active
    ON dag_tasks (workspace_id, status)
    WHERE status IN ('LEASED', 'VERIFYING');

INSERT INTO schema_migrations (version)
VALUES ('v4_008_active_lock_indexes')
ON CONFLICT (version) DO NOTHING;
