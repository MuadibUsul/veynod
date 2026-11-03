-- v3_81: the nerve channel — two-receipt dispatch and host/agent split.
--
-- The dispatch path used to poke through a one-way SSE stream and then trust
-- TCP. A half-open socket absorbs writes, so "online" was a guess and the task
-- waited. This migration adds what the alternating path needs:
--
-- * ``collab_tasks.dispatch_state``/``ack_deadline_at``: the dispatch is its
--   own little state machine (IDLE -> AWAITING_NET_ACK -> AWAITING_EXEC_ACK
--   -> ACTIVE_LEASE) with a wall-clock deadline the reaper can act on.
-- * ``poke_deliveries``: the two receipts with their measured RTTs, keyed by
--   the frame id, because a nerve poke has no event seq to hang off.
-- * ``agent_presence``: ``host_id`` beside ``agent_id``. They were conflated:
--   a computer being up said nothing about the agent on it listening.
--
-- Expand-only and idempotent; the SSE path keeps working untouched.

ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS dispatch_state VARCHAR(32) NOT NULL DEFAULT 'IDLE',
    ADD COLUMN IF NOT EXISTS ack_deadline_at TIMESTAMPTZ;

ALTER TABLE collab_tasks DROP CONSTRAINT IF EXISTS ck_collab_tasks_dispatch_state;
ALTER TABLE collab_tasks ADD CONSTRAINT ck_collab_tasks_dispatch_state
    CHECK (dispatch_state IN ('IDLE', 'AWAITING_NET_ACK', 'AWAITING_EXEC_ACK',
                              'ACTIVE_LEASE'));

CREATE INDEX IF NOT EXISTS idx_collab_tasks_ack_deadline
    ON collab_tasks (dispatch_state, ack_deadline_at)
    WHERE dispatch_state IN ('AWAITING_NET_ACK', 'AWAITING_EXEC_ACK');

ALTER TABLE poke_deliveries DROP CONSTRAINT IF EXISTS poke_deliveries_channel_check;
ALTER TABLE poke_deliveries ADD CONSTRAINT poke_deliveries_channel_check
    CHECK (channel IN ('stream', 'webhook', 'nerve'));

ALTER TABLE poke_deliveries
    ADD COLUMN IF NOT EXISTS lease_epoch BIGINT,
    ADD COLUMN IF NOT EXISTS qos_status VARCHAR(32) NOT NULL DEFAULT 'SENT',
    ADD COLUMN IF NOT EXISTS task_key VARCHAR(128),
    ADD COLUMN IF NOT EXISTS frame_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS net_ack_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS exec_ack_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS net_rtt_ms INTEGER,
    ADD COLUMN IF NOT EXISTS exec_startup_ms INTEGER;

-- A nerve poke is identified by its frame (the placement id). The old
-- uniqueness is on the event seq and cannot carry a frame, so a second,
-- partial index covers the nerve rows; ordinary deliveries keep theirs.
CREATE UNIQUE INDEX IF NOT EXISTS uq_poke_deliveries_frame
    ON poke_deliveries (workspace_id, frame_id)
    WHERE frame_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_poke_deliveries_task
    ON poke_deliveries (workspace_id, task_key, at DESC)
    WHERE task_key IS NOT NULL;

ALTER TABLE agent_presence
    ADD COLUMN IF NOT EXISTS host_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS agent_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS active_conn_id UUID,
    ADD COLUMN IF NOT EXISTS transport_kind VARCHAR(16) NOT NULL DEFAULT 'sse',
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'online',
    ADD COLUMN IF NOT EXISTS worker_busy BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS active_task_key VARCHAR(128),
    ADD COLUMN IF NOT EXISTS last_rtt_ms INTEGER;

CREATE INDEX IF NOT EXISTS idx_agent_presence_conn
    ON agent_presence (active_conn_id)
    WHERE active_conn_id IS NOT NULL;

-- A nerve socket's own process is the only thing that can vouch for it: the
-- route touches ``seen_at`` on every frame, and the in-process reaper closes a
-- socket that goes quiet. A crashed process leaves a row behind, so the reaper
-- also flips those to offline by transport kind instead of deleting them.
CREATE INDEX IF NOT EXISTS idx_agent_presence_nerve_seen
    ON agent_presence (seen_at)
    WHERE transport_kind = 'ws_nerve';

-- No ledger row here: the runner records the version from its own table, and a
-- second row under this file's name made every boot look unapplied.
