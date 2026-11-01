-- v4_012: Cell registry and repository placement (Phase 4).
--
-- A Kernel deployment can run several repositories (one workspace = one
-- repository).  A Cell is an execution boundary that owns a subset of those
-- repositories; workers with VN_CELL_ID only claim work for repositories
-- placed in that cell.  Moving a repository increments placement_epoch, so a
-- worker that claimed under the old placement cannot finalize a merge.

CREATE TABLE IF NOT EXISTS cells (
    cell_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    endpoint TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    capacity JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO cells (name, endpoint)
VALUES ('default', NULL)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS repository_placements (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(workspace_id)
        ON DELETE CASCADE,
    repository_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    cell_id UUID NOT NULL REFERENCES cells(cell_id),
    placement_epoch BIGINT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO repository_placements (workspace_id, cell_id)
SELECT w.workspace_id, c.cell_id
FROM workspaces w
JOIN cells c ON c.name = 'default'
ON CONFLICT (workspace_id) DO NOTHING;

ALTER TABLE patch_submissions
    ADD COLUMN IF NOT EXISTS claimed_cell_id UUID,
    ADD COLUMN IF NOT EXISTS claimed_placement_epoch BIGINT;

CREATE INDEX IF NOT EXISTS idx_repository_placements_cell
    ON repository_placements (cell_id, status);

INSERT INTO schema_migrations (version)
VALUES ('v4_012_cells_repositories')
ON CONFLICT (version) DO NOTHING;
