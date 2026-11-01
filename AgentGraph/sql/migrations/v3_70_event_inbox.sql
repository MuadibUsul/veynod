-- v3_70: durable cross-domain event inbox, retries, dead-letter and replay.
--
-- Kernel and Agent Runtime are separate services.  Projection must not lose
-- an event when a single sync attempt fails, and operators need a bounded,
-- inspectable retry path instead of "the next poll will fix it".

CREATE TABLE IF NOT EXISTS event_inbox (
    inbox_id BIGSERIAL PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    source_instance TEXT NOT NULL,
    source_seq BIGINT NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    workspace_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    UNIQUE (source, source_instance, source_seq)
);

CREATE INDEX IF NOT EXISTS idx_event_inbox_due
    ON event_inbox (next_attempt_at, inbox_id)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_event_inbox_dead
    ON event_inbox (dead_lettered_at)
    WHERE status = 'DEAD';
CREATE INDEX IF NOT EXISTS idx_event_inbox_type
    ON event_inbox (source, event_type, received_at DESC);

-- Cursor for the Runtime-wide event stream.  Kernel uses the older
-- code_projection_cursors table (one row per Kernel workspace).
CREATE TABLE IF NOT EXISTS runtime_projection_cursors (
    runtime_base_url TEXT PRIMARY KEY,
    last_event_seq BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
