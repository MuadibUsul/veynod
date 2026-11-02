-- A Kernel task receives one immutable WorkScope. Each granted lease keeps
-- its own snapshot/hash so submit_patch validates the exact granted boundary.
ALTER TABLE dag_tasks ADD COLUMN IF NOT EXISTS work_scope JSONB;
ALTER TABLE dag_tasks ADD COLUMN IF NOT EXISTS scope_hash CHAR(64);
ALTER TABLE dag_tasks ADD COLUMN IF NOT EXISTS lease_scope JSONB;
ALTER TABLE dag_tasks ADD COLUMN IF NOT EXISTS lease_scope_hash CHAR(64);

ALTER TABLE dag_tasks DROP CONSTRAINT IF EXISTS ck_dag_tasks_scope_pair;
ALTER TABLE dag_tasks ADD CONSTRAINT ck_dag_tasks_scope_pair
    CHECK ((work_scope IS NULL) = (scope_hash IS NULL));
ALTER TABLE dag_tasks DROP CONSTRAINT IF EXISTS ck_dag_tasks_lease_scope_pair;
ALTER TABLE dag_tasks ADD CONSTRAINT ck_dag_tasks_lease_scope_pair
    CHECK ((lease_scope IS NULL) = (lease_scope_hash IS NULL));

CREATE OR REPLACE FUNCTION reject_dag_work_scope_change() RETURNS trigger AS $$
BEGIN
    IF OLD.work_scope IS NOT NULL AND
       (NEW.work_scope IS DISTINCT FROM OLD.work_scope OR
        NEW.scope_hash IS DISTINCT FROM OLD.scope_hash) THEN
        RAISE EXCEPTION 'WorkScope is immutable; create a new task/attempt';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_dag_work_scope_immutable ON dag_tasks;
CREATE TRIGGER trg_dag_work_scope_immutable
    BEFORE UPDATE OF work_scope, scope_hash ON dag_tasks
    FOR EACH ROW EXECUTE FUNCTION reject_dag_work_scope_change();
