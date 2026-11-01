-- Agent Runtime 010: lease epoch for write fencing.
--
-- A Run can be claimed by a second worker while the first is still inside a
-- slow model or tool call.  Claiming now increments lease_epoch, and every
-- worker-side write (transition, step, checkpoint, usage, deferred invocation)
-- carries the epoch it was claimed with.  A stale worker's write finds a
-- different epoch and is rejected with LeaseLost instead of corrupting the
-- Run the live worker owns.

ALTER TABLE {{schema}}.agent_runs
    ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_runtime_runs_lease_epoch
    ON {{schema}}.agent_runs (run_id, lease_epoch);
