-- v3.49: the v3.40 tables (blobs, document attachments, uploads, cleanup
-- grants, share links) join the tenant dimension (PRD AG-ID-07): a derived
-- tenant_id, the derive trigger and the standard tenant_isolation policy.
-- Unscoped sessions (public share resolution, sweeps) keep the connection
-- role, exactly as they do for shared_documents.

DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['blobs', 'document_blobs', 'blob_uploads',
                                        'cleanup_grants', 'share_links'] LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(40)',
                       target_table);
        EXECUTE format(
            'UPDATE %I t SET tenant_id = w.tenant_id FROM workspaces w '
            'WHERE w.id = t.workspace_id AND t.tenant_id IS DISTINCT FROM w.tenant_id',
            target_table);
        EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET NOT NULL', target_table);
        EXECUTE format('DROP TRIGGER IF EXISTS trg_derive_tenant ON %I', target_table);
        EXECUTE format(
            'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id '
            'ON %I FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant()', target_table);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target_table);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target_table);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) '
            'AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) '
            'AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)',
            target_table);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO agentgraph_app',
                       target_table);
    END LOOP;
END
$$;
