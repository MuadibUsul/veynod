-- v3.43 Explicit lease renewal (spec §4.2 / §1.6, veynod-fsm-lease-fencing-v1).
--
-- A lease may be renewed explicitly while less than 300 seconds remain, at most
-- 12 times per claim; every claim or hand-off resets the counter. The fence is
-- the task's lease_epoch (fencing_token): renewing does not change it.
BEGIN;

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS lease_renew_count INT NOT NULL DEFAULT 0;

COMMIT;
