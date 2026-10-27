-- v3.45 Message timeout escalation (blueprint-L3-messaging-open, M-14).
--
-- A message that no recipient has read past within the escalation threshold
-- notifies its sender once. The reaper records the escalation here so the
-- notification fires exactly once per message.

CREATE TABLE IF NOT EXISTS message_escalations (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    sender_ref VARCHAR(256) NOT NULL,
    escalated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (message_id)
);
CREATE INDEX IF NOT EXISTS idx_message_escalations_workspace
    ON message_escalations (workspace_id, escalated_at);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['message_escalations'] LOOP
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
GRANT USAGE ON SEQUENCE message_escalations_id_seq TO agentgraph_app;
