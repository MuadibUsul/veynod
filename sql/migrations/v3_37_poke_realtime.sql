-- v3.37 Realtime pokes (TASK-72A59BD9): presence, delivery receipts, read receipts.
--
-- * agent_presence: one row per open inbox stream (token x server instance).
--   An agent is online while a row was seen in the last 90 seconds; rows are
--   removed on disconnect and swept by the reaper. Registration is no longer
--   mistaken for liveness.
-- * poke_deliveries: when an addressed event reached a recipient, and how
--   (stream push, webhook wake, or a failed wake with its reason).
-- * inbox_acks: each ack moves the read watermark; the row keeps when, so a
--   poke's "read" time is the first ack that covered its seq.

CREATE TABLE IF NOT EXISTS agent_presence (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    token_id VARCHAR(40) NOT NULL,
    agent_ref VARCHAR(256) NOT NULL,
    instance VARCHAR(64) NOT NULL,
    transport VARCHAR(16) NOT NULL DEFAULT 'sse',
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (token_id, instance)
);
CREATE INDEX IF NOT EXISTS idx_agent_presence_workspace
    ON agent_presence (workspace_id, seen_at DESC);

CREATE TABLE IF NOT EXISTS poke_deliveries (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    event_seq BIGINT NOT NULL,
    recipient VARCHAR(256) NOT NULL,
    channel VARCHAR(16) NOT NULL CHECK (channel IN ('stream', 'webhook')),
    status VARCHAR(16) NOT NULL CHECK (status IN ('delivered', 'failed')),
    detail TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, event_seq, recipient, channel)
);
CREATE INDEX IF NOT EXISTS idx_poke_deliveries_seq
    ON poke_deliveries (workspace_id, event_seq);

CREATE TABLE IF NOT EXISTS inbox_acks (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    owner VARCHAR(256) NOT NULL,
    from_seq BIGINT NOT NULL,
    to_seq BIGINT NOT NULL,
    acked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_inbox_acks_owner
    ON inbox_acks (workspace_id, owner, to_seq);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['agent_presence', 'poke_deliveries', 'inbox_acks'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', table_name);
        EXECUTE format('CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
                       'workspace_id, tenant_id ON %I FOR EACH ROW EXECUTE FUNCTION '
                       'agentgraph_derive_tenant()', table_name);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I '
                       'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
                       'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
                       'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
                       'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
                       table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO agentgraph_app',
                       table_name);
    END LOOP;
END $$;
GRANT USAGE ON SEQUENCE agent_presence_id_seq, poke_deliveries_id_seq, inbox_acks_id_seq
    TO agentgraph_app;

-- Wake webhooks: a webhook rule may carry the refs it is for (targets) and
-- fire only while that agent is offline (wake_only). An agent sets its own
-- with set_wake_url; delivery, signing, retries and DEAD are the webhook ones.
ALTER TABLE webhook_rules ADD COLUMN IF NOT EXISTS targets TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE webhook_rules ADD COLUMN IF NOT EXISTS wake_only BOOLEAN NOT NULL DEFAULT FALSE;

-- Token hot swap (2026-10-24 #21 / decision 5): a rotated secret keeps the
-- token, its agent_id and its tree; the previous secret keeps working for a
-- short grace window so a long session can switch without a 401 halfway.
ALTER TABLE agent_tokens ADD COLUMN IF NOT EXISTS previous_key_hash VARCHAR(256);
ALTER TABLE agent_tokens ADD COLUMN IF NOT EXISTS previous_key_valid_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_agent_tokens_previous_key
    ON agent_tokens (previous_key_hash) WHERE previous_key_hash IS NOT NULL;
