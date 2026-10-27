-- v3.44 Group conversations and approval (blueprint-L3-messaging-open, M-12/A-09).
--
-- Group creation requires human approval: an agent's create_group leaves the
-- conversation 'pending'; a human approves it into 'active'. Only active
-- conversations accept messages. Direct conversations are born active.

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'pending')),
    ADD COLUMN IF NOT EXISTS approved_by VARCHAR(256),
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_conversations_status
    ON conversations (workspace_id, status);
