-- v3_69: durable cursor for Kernel event projection
--
-- AgentGraph consumes Kernel kernel_events incrementally.  The cursor is
-- keyed by the Kernel workspace (one Kernel workspace per AgentGraph code
-- plane in the current deployment).

CREATE TABLE IF NOT EXISTS code_projection_cursors (
    kernel_workspace_id UUID PRIMARY KEY,
    last_event_seq BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
