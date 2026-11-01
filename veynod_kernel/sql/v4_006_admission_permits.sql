-- v4_006: admission permits
--
-- Admission control needs a reservation that is visible before a claim is
-- committed. Counting only patch_submissions/VERIFYING lets a burst of
-- concurrent pulls all observe an empty queue and lease the whole backlog.
--
-- A permit is inserted under a short advisory lock and committed before the
-- (heavy) claim decision. Terminal task transitions release it through the
-- trigger; expired or orphaned permits are cleaned on the next admission.

CREATE TABLE IF NOT EXISTS admission_permits (
    permit_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    task_id UUID REFERENCES dag_tasks(task_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admission_permits_workspace_expiry
    ON admission_permits (workspace_id, expires_at);

CREATE INDEX IF NOT EXISTS idx_admission_permits_task
    ON admission_permits (task_id);

CREATE OR REPLACE FUNCTION release_admission_permits_for_task()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status NOT IN ('LEASED', 'VERIFYING') THEN
        DELETE FROM admission_permits WHERE task_id = NEW.task_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_release_admission_permits ON dag_tasks;
CREATE TRIGGER trg_release_admission_permits
    AFTER UPDATE OF status ON dag_tasks
    FOR EACH ROW
    EXECUTE FUNCTION release_admission_permits_for_task();

INSERT INTO schema_migrations (version)
VALUES ('v4_006_admission_permits')
ON CONFLICT (version) DO NOTHING;
