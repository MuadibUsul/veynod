-- v3.36 OAuth 2.1 for MCP clients (claude.ai connectors, Claude Desktop).
--
-- * oauth_clients: dynamically registered public clients (RFC 7591). No
--   secrets: PKCE (S256) protects the code exchange.
-- * oauth_requests: one authorization attempt. A signed-in owner approves it
--   in the console; the agent key is minted then and stored only encrypted
--   under a key derived from the authorization code, which the server keeps as
--   a SHA-256. The client exchanges the code once; the sealed copy is cleared.
--
-- Both are operator-only (RLS forced, no application-role policy): a request
-- has no workspace until someone approves it.

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id VARCHAR(64) PRIMARY KEY,
    client_name VARCHAR(128) NOT NULL DEFAULT '',
    redirect_uris JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);
ALTER TABLE oauth_clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_clients FORCE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS oauth_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id VARCHAR(64) NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    state TEXT,
    code_challenge VARCHAR(128) NOT NULL,
    scope TEXT,
    resource TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'consumed')),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID,
    agent_signature VARCHAR(128),
    token_id UUID,
    code_hash VARCHAR(128) UNIQUE,
    sealed_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_oauth_requests_expires ON oauth_requests (expires_at);
ALTER TABLE oauth_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_requests FORCE ROW LEVEL SECURITY;
