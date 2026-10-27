-- v3.43 Conversations and messages (blueprint-L3-messaging-open).
--
-- Generalize the 3.6.0 poke/receipt foundation to friend-style messaging
-- (spec-realtime-messaging v2). Messages are a first-class, permanently
-- stored object; delivery reuses the workspace_events outbox (stream is the
-- primary path, inbox is the fallback and record), and receipts extend the
-- sent/delivered/read model with an explicit per-conversation read watermark.
--
-- * conversations: one row per 1:1 (kind='direct') or group (kind='group')
--   conversation. A direct conversation is addressed by its unique conv_key.
-- * conversation_members: who is in a conversation, their role, mute/block
--   flags (M-02) and the explicit read watermark last_read_seq (M-07).
-- * messages: permanent message storage (M-11). seq is gap-free per
--   conversation; created_at is authoritative ordering.
-- * message_deliveries: sent/delivered receipts per recipient and channel,
--   the message analogue of poke_deliveries.

CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    conv_key VARCHAR(64) NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('direct', 'group')),
    title VARCHAR(256),
    created_by VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, conv_key)
);
CREATE INDEX IF NOT EXISTS idx_conversations_workspace
    ON conversations (workspace_id, kind);

CREATE TABLE IF NOT EXISTS conversation_members (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    member_ref VARCHAR(256) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'admin')),
    muted BOOLEAN NOT NULL DEFAULT FALSE,
    blocked BOOLEAN NOT NULL DEFAULT FALSE,
    last_read_seq BIGINT NOT NULL DEFAULT 0,
    last_read_at TIMESTAMPTZ,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (conversation_id, member_ref)
);
CREATE INDEX IF NOT EXISTS idx_conversation_members_ref
    ON conversation_members (workspace_id, member_ref);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq BIGINT NOT NULL,
    sender_ref VARCHAR(256) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (workspace_id, conversation_id, seq);

CREATE TABLE IF NOT EXISTS message_deliveries (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    recipient VARCHAR(256) NOT NULL,
    channel VARCHAR(16) NOT NULL CHECK (channel IN ('stream', 'inbox', 'webhook')),
    status VARCHAR(16) NOT NULL CHECK (status IN ('delivered', 'failed')),
    detail TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (message_id, recipient, channel)
);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_message
    ON message_deliveries (workspace_id, message_id);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['conversations', 'conversation_members',
                                       'messages', 'message_deliveries'] LOOP
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
GRANT USAGE ON SEQUENCE conversations_id_seq, conversation_members_id_seq,
    messages_id_seq, message_deliveries_id_seq TO agentgraph_app;
