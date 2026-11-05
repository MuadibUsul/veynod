-- v3.91 Skill registry: search and discovery (task P3-2, docs/federation/ICD_P3_REGISTRY.md §7).
--
-- A package carries the category and capability tags of its latest version
-- (the versions themselves stay immutable). Downloads count the distinct
-- workspaces that pulled a version, so re-installing never inflates them.
-- A rating comes from a person in a workspace that installed the package,
-- one per workspace and package, never from the publisher itself.

ALTER TABLE registry_packages
    ADD COLUMN IF NOT EXISTS category VARCHAR(24) NOT NULL DEFAULT 'other';
ALTER TABLE registry_packages
    ADD COLUMN IF NOT EXISTS capabilities TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE registry_packages ADD COLUMN IF NOT EXISTS latest_version VARCHAR(32);
ALTER TABLE registry_packages
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE registry_packages DROP CONSTRAINT IF EXISTS registry_packages_category_check;
ALTER TABLE registry_packages ADD CONSTRAINT registry_packages_category_check CHECK (
    category IN ('research', 'writing', 'coding', 'data', 'design', 'operations', 'other'));
ALTER TABLE registry_packages DROP CONSTRAINT IF EXISTS registry_packages_capabilities_check;
ALTER TABLE registry_packages ADD CONSTRAINT registry_packages_capabilities_check
    CHECK (cardinality(capabilities) <= 10);

UPDATE registry_packages p SET latest_version = (
    SELECT v.version FROM registry_versions v WHERE v.name = p.name
     ORDER BY v.major DESC, v.minor DESC, v.patch DESC LIMIT 1)
 WHERE latest_version IS NULL;

CREATE INDEX IF NOT EXISTS idx_registry_packages_capabilities
    ON registry_packages USING GIN (capabilities);
CREATE INDEX IF NOT EXISTS idx_registry_packages_category ON registry_packages (category);

-- One row per workspace and version pulled (installs, dependency installs).
CREATE TABLE IF NOT EXISTS registry_downloads (
    name VARCHAR(64) NOT NULL,
    version VARCHAR(32) NOT NULL,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    first_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (name, version, workspace_id),
    FOREIGN KEY (name, version) REFERENCES registry_versions(name, version)
);
CREATE INDEX IF NOT EXISTS idx_registry_downloads_name ON registry_downloads (name);
INSERT INTO registry_downloads (name, version, workspace_id, first_at)
SELECT name, version, workspace_id, installed_at FROM registry_installs
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS registry_ratings (
    name VARCHAR(64) NOT NULL REFERENCES registry_packages(name),
    rater_fid VARCHAR(64) NOT NULL REFERENCES federation_identities(federation_id),
    version VARCHAR(32) NOT NULL,
    stars SMALLINT NOT NULL CHECK (stars BETWEEN 1 AND 5),
    review TEXT CHECK (review IS NULL OR char_length(review) <= 1000),
    rated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (name, rater_fid),
    FOREIGN KEY (name, version) REFERENCES registry_versions(name, version)
);

-- Deployment-wide like the rest of the registry: platform session only.
REVOKE ALL ON registry_downloads FROM agentgraph_app;
REVOKE ALL ON registry_ratings FROM agentgraph_app;
