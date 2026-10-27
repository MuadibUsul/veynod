-- Presence states (blueprint L2; spec-realtime-messaging M-05a / M-01a).
-- The last state the platform announced for each directory entry, so a change
-- is published once (AGENT_PRESENCE_CHANGED) instead of being re-derived by
-- every reader. NULL until the first reconciliation sets it silently.
ALTER TABLE agent_directory ADD COLUMN IF NOT EXISTS presence_state VARCHAR(16)
    CHECK (presence_state IS NULL
           OR presence_state IN ('online', 'offline', 'degraded', 'unwakeable'));
ALTER TABLE agent_directory ADD COLUMN IF NOT EXISTS presence_changed_at TIMESTAMPTZ;

-- Loop heartbeats (POST /v1/presence/heartbeat) share agent_presence with the
-- inbox stream; transport tells them apart.
CREATE INDEX IF NOT EXISTS idx_agent_presence_token ON agent_presence (token_id, seen_at DESC);
