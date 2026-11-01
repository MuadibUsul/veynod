-- v3_76: derive event_inbox.tenant_id when the workspace is known.
--
-- v3_75 is already applied in live databases, so this is repeated here
-- (idempotently). The column stays NULLABLE on purpose: the inbox is an
-- operational delivery ledger and can hold events that name no workspace
-- (platform events, synthetic task ids in tests). A known workspace always
-- derives its tenant; a writer can never forge it.

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

UPDATE event_inbox e
SET tenant_id = w.tenant_id
FROM workspaces w
WHERE e.workspace_id = w.id::text AND e.tenant_id IS NULL;

DROP TRIGGER IF EXISTS trg_derive_tenant ON event_inbox;
CREATE TRIGGER trg_derive_tenant
    BEFORE INSERT OR UPDATE OF workspace_id, tenant_id ON event_inbox
    FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant_text();

ALTER TABLE event_inbox ALTER COLUMN tenant_id DROP NOT NULL;
