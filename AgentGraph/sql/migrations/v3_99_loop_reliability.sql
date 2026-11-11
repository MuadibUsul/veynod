-- v3_99: the three stages of a Loop dispatch, each with its own record.
--
-- Delivered is not started, and started is not finished. Three things were
-- missing:
--
-- * ``collab_tasks.dispatch_state`` went to ACTIVE_LEASE when a computer took a
--   task and never came back. Once that task returned to READY (released, lease
--   lost, rejected), every later dispatch was refused as "already dispatching",
--   the offer timed out as not claimed, and after three rounds the task stopped
--   in HANDOFF. A trigger now keeps the state true to the lease, and the rows
--   already stuck are reset here.
-- * a poke had two receipts (the computer read it, the worker process is up)
--   but nothing for "this placement was checked and work on it began". That is
--   ``exec_started_at``: the worker asks to start, the board answers whether
--   the placement is still open.
-- * a worker that arrives after its placement moved on used to exit 0 with a
--   local "skipped" receipt, which read as a round that ran. ``skipped`` is now
--   a report of its own, never counted as finished.
--
-- Expand-only and idempotent.

CREATE OR REPLACE FUNCTION agentgraph_dispatch_follows_lease() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.dispatch_state = 'ACTIVE_LEASE'
       AND (NEW.status::text <> 'IN_PROGRESS' OR NEW.lease_owner IS NULL) THEN
        NEW.dispatch_state := 'IDLE';
        NEW.ack_deadline_at := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dispatch_follows_lease ON collab_tasks;
CREATE TRIGGER trg_dispatch_follows_lease
    BEFORE UPDATE ON collab_tasks
    FOR EACH ROW
    EXECUTE FUNCTION agentgraph_dispatch_follows_lease();

UPDATE collab_tasks SET dispatch_state = 'IDLE', ack_deadline_at = NULL
WHERE dispatch_state = 'ACTIVE_LEASE'
  AND (status::text <> 'IN_PROGRESS' OR lease_owner IS NULL);

ALTER TABLE poke_deliveries
    ADD COLUMN IF NOT EXISTS exec_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS exec_started_ms INTEGER,
    ADD COLUMN IF NOT EXISTS warm BOOLEAN;

ALTER TABLE runner_placements ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE runner_placements DROP CONSTRAINT IF EXISTS runner_placements_reported_check;
ALTER TABLE runner_placements ADD CONSTRAINT runner_placements_reported_check
    CHECK (reported IN ('finished', 'failed', 'skipped'));

-- Timings and outcomes are read per workspace over a recent window.
CREATE INDEX IF NOT EXISTS idx_poke_deliveries_nerve_at
    ON poke_deliveries (workspace_id, at DESC)
    WHERE channel = 'nerve';

INSERT INTO schema_migrations (version, applied_at)
VALUES ('v3_99_loop_reliability', NOW())
ON CONFLICT (version) DO NOTHING;
