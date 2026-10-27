-- v3.48 CommanderGrant: explicit, scoped, expiring authority for a COMMANDER
-- agent (Veynod v0.2 role rules §2.5, §5.2, Q6).
--
-- A COMMANDER never inherits write authority from its role: every write must
-- name a grant issued by a human OWNER, and the grant pins the allowed
-- actions, the namespace, optional task keys/kind, a reason and an expiry.
-- Revocation is immediate and keeps the row for audit.

CREATE TABLE IF NOT EXISTS commander_grants (
    grant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    issued_by TEXT NOT NULL CHECK (issued_by LIKE 'user:%'),
    grantee TEXT NOT NULL CHECK (grantee LIKE 'agent:%'),
    allowed_actions TEXT[] NOT NULL CHECK (cardinality(allowed_actions) >= 1),
    scope_namespace TEXT NOT NULL,
    scope_task_keys TEXT[],
    scope_task_kind TEXT,
    reason TEXT NOT NULL CHECK (char_length(reason) >= 5),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_commander_grants_workspace
    ON commander_grants (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commander_grants_grantee
    ON commander_grants (workspace_id, grantee)
    WHERE revoked_at IS NULL;

-- COMMANDER becomes a mintable agent role (it was only an internal role).
ALTER TABLE agent_tokens DROP CONSTRAINT IF EXISTS agent_tokens_role_scope_check;
ALTER TABLE agent_tokens ADD CONSTRAINT agent_tokens_role_scope_check
    CHECK (role_scope IN ('SCOUT', 'ANALYST', 'AUDITOR', 'EXECUTOR',
                          'COMMANDER', 'SUPER_ADMIN'));

DO $$
BEGIN
    EXECUTE 'DROP TRIGGER IF EXISTS trg_derive_tenant ON commander_grants';
    EXECUTE 'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
            'workspace_id, tenant_id ON commander_grants FOR EACH ROW '
            'EXECUTE FUNCTION agentgraph_derive_tenant()';
    EXECUTE 'ALTER TABLE commander_grants ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE commander_grants FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON commander_grants';
    EXECUTE 'CREATE POLICY tenant_isolation ON commander_grants '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON commander_grants TO agentgraph_app';
END $$;
