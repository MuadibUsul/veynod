-- One open placement per task per step. The index predates loop steps: with
-- review and merge placements a task legitimately holds an open review
-- placement while the scheduler places its work, so keying it on task_id alone
-- made the work insert violate the index every cycle.
DROP INDEX IF EXISTS uq_runner_placements_open;
CREATE UNIQUE INDEX IF NOT EXISTS uq_runner_placements_open
    ON runner_placements (task_id, step)
    WHERE status IN ('suggested', 'offered', 'claimed', 'unplaced');
