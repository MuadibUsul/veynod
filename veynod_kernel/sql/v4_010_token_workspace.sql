-- v4_010: bind runner tokens to a workspace
--
-- Legacy tokens may keep workspace_id NULL. New tokens issued through the
-- admin API are bound to the requested workspace, or to the configured
-- default workspace when one exists. Read-only runner surfaces enforce the
-- binding; the single-workspace syscall plane continues to use its pinned
-- workspace and rejects a bound token aimed at a different one.

ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS workspace_id UUID
    REFERENCES workspaces(workspace_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_agent_tokens_workspace
    ON agent_tokens (workspace_id);

INSERT INTO schema_migrations (version)
VALUES ('v4_010_token_workspace')
ON CONFLICT (version) DO NOTHING;
