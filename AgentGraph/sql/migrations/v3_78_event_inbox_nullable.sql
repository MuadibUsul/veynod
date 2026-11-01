-- v3_78: keep event_inbox.tenant_id nullable (see v3_76).
--
-- The inbox is an operational ledger; a delivery can name no workspace. The
-- trigger still derives the tenant from a known workspace, so a writer cannot
-- forge it, and every real tenant event carries its derived tenant.

ALTER TABLE event_inbox ALTER COLUMN tenant_id DROP NOT NULL;

CREATE OR REPLACE FUNCTION agentgraph_derive_tenant_text() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    NEW.tenant_id := (
        SELECT tenant_id FROM workspaces
        WHERE id::text = NEW.workspace_id
    );
    RETURN NEW;
END;
$$;
