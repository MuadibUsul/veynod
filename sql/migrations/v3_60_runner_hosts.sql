-- v3.60 Runner hosts (full-managed Loop, step 2: runner-2-host-handshake).
--
-- A runner is the program that runs agents on some computer: a VPS, a laptop,
-- a Raspberry Pi, the Windows client. It connects out to the platform and
-- says what the machine can do: system, runtimes (Claude Code, Codex) with
-- their models and quota windows, how many tasks at once, which repositories.
-- One runner token is one host. The scheduler (step 3) picks hosts from here;
-- the owner can revoke a host, which revokes its token and frees its tasks.
-- Nothing secret is stored: the hostname is kept only as a digest.

CREATE TABLE IF NOT EXISTS runner_hosts (
    host_id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('hst'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    token_id VARCHAR(40) NOT NULL,
    agent_id VARCHAR(40),
    os VARCHAR(16) NOT NULL,
    arch VARCHAR(16) NOT NULL,
    hostname_digest VARCHAR(16),
    runner_version VARCHAR(32),
    runtimes JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_concurrency INT NOT NULL DEFAULT 1 CHECK (max_concurrency BETWEEN 1 AND 64),
    repos TEXT[] NOT NULL DEFAULT '{}',
    running TEXT[] NOT NULL DEFAULT '{}',
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'revoked')),
    revoked_by VARCHAR(128),
    revoked_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, token_id)
);

CREATE INDEX IF NOT EXISTS idx_runner_hosts_ws ON runner_hosts (workspace_id, status, last_seen_at);

DO $$
BEGIN
    EXECUTE 'DROP TRIGGER IF EXISTS trg_derive_tenant ON runner_hosts';
    EXECUTE 'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
            'workspace_id, tenant_id ON runner_hosts FOR EACH ROW '
            'EXECUTE FUNCTION agentgraph_derive_tenant()';
    EXECUTE 'ALTER TABLE runner_hosts ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE runner_hosts FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON runner_hosts';
    EXECUTE 'CREATE POLICY tenant_isolation ON runner_hosts '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON runner_hosts TO agentgraph_app';
END $$;
