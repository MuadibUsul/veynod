-- v3.100 MCP connections (docs/MCP_CONNECTION_LIFECYCLE.md, rollout steps 1-3).
--
-- A token was both the identity of a client installation and its secret, so
-- running `npx veynod` again minted `cursor-2`, `cursor-3`... and a client
-- whose configuration was deleted kept a working key forever.
--
-- A connection is the installation: one per workspace and client instance id
-- (made by the CLI, kept in ~/.veynod/connection.json). Its credentials are
-- agent_tokens rows that point at it. Reconnecting rotates the credential in
-- place; a credential issued through the CLI has an expiry that slides while it
-- is used, and the reaper revokes (never deletes) what expired.
--
-- Expand-only and idempotent. Existing tokens keep connection_id NULL and are
-- adopted the next time a client with the same signature reconnects.

CREATE TABLE IF NOT EXISTS mcp_connections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    client_instance_id VARCHAR(64) NOT NULL,
    client_kind VARCHAR(32) NOT NULL DEFAULT 'other',
    label VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'dormant', 'revoked')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoke_reason VARCHAR(200),
    UNIQUE (workspace_id, client_instance_id)
);

ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS connection_id UUID REFERENCES mcp_connections(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_agent_tokens_connection
    ON agent_tokens (connection_id) WHERE connection_id IS NOT NULL;

ALTER TABLE cli_sessions ADD COLUMN IF NOT EXISTS client_instance_id VARCHAR(64);

DO $$
BEGIN
    EXECUTE 'DROP TRIGGER IF EXISTS trg_derive_tenant ON mcp_connections';
    EXECUTE 'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
            'workspace_id, tenant_id ON mcp_connections FOR EACH ROW '
            'EXECUTE FUNCTION agentgraph_derive_tenant()';
    EXECUTE 'ALTER TABLE mcp_connections ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE mcp_connections FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON mcp_connections';
    EXECUTE 'CREATE POLICY tenant_isolation ON mcp_connections '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON mcp_connections TO agentgraph_app';
END $$;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('v3_100_mcp_connections', NOW())
ON CONFLICT (version) DO NOTHING;
