-- v3.90 Skill registry: publish and versions (task P3-1, docs/federation/ICD_P3_REGISTRY.md).
--
-- A package name belongs to the workspace that first published it. Every
-- version is immutable once published: its manifest, files and digest never
-- change and are never deleted; a version can only be yanked, which stops
-- new installs and keeps the record (F-023). Installs pin a digest.

CREATE TABLE IF NOT EXISTS registry_packages (
    name VARCHAR(64) PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    publisher_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    description TEXT CHECK (description IS NULL OR char_length(description) <= 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS registry_versions (
    name VARCHAR(64) NOT NULL REFERENCES registry_packages(name),
    version VARCHAR(32) NOT NULL CHECK (version ~ '^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$'),
    major INTEGER NOT NULL,
    minor INTEGER NOT NULL,
    patch INTEGER NOT NULL,
    digest VARCHAR(71) NOT NULL UNIQUE CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('declarative', 'executable')),
    manifest JSONB NOT NULL,
    files JSONB NOT NULL,
    dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
    signature JSONB NOT NULL,
    published_by TEXT NOT NULL,
    state VARCHAR(12) NOT NULL DEFAULT 'published' CHECK (state IN ('published', 'yanked')),
    yanked_at TIMESTAMPTZ,
    yanked_by TEXT,
    yank_reason TEXT CHECK (yank_reason IS NULL OR char_length(yank_reason) <= 1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (name, version),
    CHECK ((state = 'yanked') = (yanked_at IS NOT NULL))
);

CREATE OR REPLACE FUNCTION registry_version_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'registry versions are never deleted (yank instead)'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.name <> OLD.name OR NEW.version <> OLD.version OR NEW.digest <> OLD.digest
       OR NEW.kind <> OLD.kind OR NEW.manifest <> OLD.manifest OR NEW.files <> OLD.files
       OR NEW.dependencies <> OLD.dependencies OR NEW.signature <> OLD.signature
       OR NEW.published_by <> OLD.published_by THEN
        RAISE EXCEPTION 'a published version is immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.state = 'yanked' AND NEW.state <> 'yanked' THEN
        RAISE EXCEPTION 'a yanked version stays yanked; publish a new version'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_registry_version_guard ON registry_versions;
CREATE TRIGGER trg_registry_version_guard
    BEFORE UPDATE OR DELETE ON registry_versions
    FOR EACH ROW EXECUTE FUNCTION registry_version_guard();

-- What each workspace installed, pinned by digest.
CREATE TABLE IF NOT EXISTS registry_installs (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    name VARCHAR(64) NOT NULL,
    version VARCHAR(32) NOT NULL,
    digest VARCHAR(71) NOT NULL REFERENCES registry_versions(digest),
    accepted_permissions TEXT[] NOT NULL DEFAULT '{}',
    as_dependency_of VARCHAR(64),
    installed_by TEXT NOT NULL,
    installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, name),
    FOREIGN KEY (name, version) REFERENCES registry_versions(name, version)
);

DROP TRIGGER IF EXISTS trg_derive_tenant ON registry_installs;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON registry_installs FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
ALTER TABLE registry_installs ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry_installs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON registry_installs;
CREATE POLICY tenant_isolation ON registry_installs
    USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id =
           NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON registry_installs TO agentgraph_app;
-- The registry itself is deployment-wide, like the identity tables.
REVOKE ALL ON registry_packages FROM agentgraph_app;
REVOKE ALL ON registry_versions FROM agentgraph_app;
