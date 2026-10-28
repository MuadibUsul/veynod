-- v3.51 Diagrams and chunked dispatch, lightweight stage (agentgraph/spec-diagram-dispatch v3).
--
-- * diagrams: the planning layer. A diagram is drafted, confirmed by a person
--   (the gate), frozen to a baseline (version + hash + frozen layers), and only
--   then dispatched. Its text lives in a versioned shared document
--   (doc_key), rewritten on every version, so old versions stay readable.
-- * diagram_chunks: the unit of dispatch. Each chunk carries its interface
--   contract (icd), machine-checkable done criteria, dependencies and its
--   write scope. The task it was dispatched to is the execution record; the
--   chunk keeps what the task board does not: block reason, gate, merge.
-- * diagram_changes: every change after confirmation is a proposal with an
--   impact, decided by a person or by a super admin acting for the owner
--   (recorded as such), and revocable by a person (spec section 16).

CREATE TABLE IF NOT EXISTS diagrams (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    diagram_key VARCHAR(64) NOT NULL,
    title VARCHAR(256) NOT NULL,
    namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    doc_key VARCHAR(256) NOT NULL,
    description TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'confirmed', 'frozen')),
    version INT NOT NULL DEFAULT 1,
    baseline JSONB,
    self_check JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by VARCHAR(256) NOT NULL,
    confirmed_by VARCHAR(256),
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, diagram_key)
);

CREATE TABLE IF NOT EXISTS diagram_chunks (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    diagram_id BIGINT NOT NULL REFERENCES diagrams(id) ON DELETE CASCADE,
    chunk_key VARCHAR(64) NOT NULL,
    title VARCHAR(256) NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    layer VARCHAR(16) NOT NULL DEFAULT 'implementation'
        CHECK (layer IN ('interface', 'implementation')),
    icd JSONB NOT NULL DEFAULT '{}'::jsonb,
    done_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    depends_on TEXT[] NOT NULL DEFAULT '{}',
    allowed_write_paths TEXT[] NOT NULL DEFAULT '{}',
    read_only_interfaces TEXT[] NOT NULL DEFAULT '{}',
    state VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'dispatched', 'blocked', 'rework', 'merged', 'retired')),
    block_reason TEXT,
    task_key VARCHAR(128),
    gate JSONB,
    merge JSONB,
    origin VARCHAR(16) NOT NULL DEFAULT 'plan' CHECK (origin IN ('plan', 'increment')),
    increment_of VARCHAR(64),
    created_in_version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (diagram_id, chunk_key)
);
CREATE INDEX IF NOT EXISTS idx_diagram_chunks_task ON diagram_chunks (workspace_id, task_key);

CREATE TABLE IF NOT EXISTS diagram_changes (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    diagram_id BIGINT NOT NULL REFERENCES diagrams(id) ON DELETE CASCADE,
    change_key VARCHAR(40) NOT NULL,
    source VARCHAR(24) NOT NULL DEFAULT 'proposal'
        CHECK (source IN ('proposal', 'human_request')),
    request_text TEXT,
    summary TEXT NOT NULL,
    impact JSONB NOT NULL DEFAULT '{}'::jsonb,
    patch JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'revoked')),
    proposed_by VARCHAR(256) NOT NULL,
    decided_by VARCHAR(256),
    decision_kind VARCHAR(24) CHECK (decision_kind IS NULL OR decision_kind IN
        ('person', 'super_admin_for_owner')),
    decision_note TEXT,
    decided_at TIMESTAMPTZ,
    from_version INT,
    to_version INT,
    before JSONB,
    revoked_by VARCHAR(256),
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, change_key)
);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['diagrams', 'diagram_chunks', 'diagram_changes'] LOOP
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
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I TO agentgraph_app',
                       table_name || '_id_seq');
    END LOOP;
END $$;
