-- v4_013: task snapshots, symbol-graph versions and merge conflicts (Phase 5).
--
-- A task is created against a snapshot of its artifacts and base commit.  A
-- submission records which snapshot it was verified against.  When the main
-- branch moves on a sensitive file, the conflict is persisted (not just
-- logged) so an operator can see what changed and why revalidation happened.

CREATE TABLE IF NOT EXISTS task_snapshots (
    snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id)
        ON DELETE CASCADE,
    base_sha VARCHAR(40) NOT NULL,
    graph_version BIGINT NOT NULL DEFAULT 0,
    manifest JSONB NOT NULL DEFAULT '{}'::JSONB,
    manifest_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_snapshots_workspace
    ON task_snapshots (workspace_id, created_at DESC);

ALTER TABLE dag_tasks
    ADD COLUMN IF NOT EXISTS snapshot_id UUID
        REFERENCES task_snapshots(snapshot_id) ON DELETE SET NULL;

ALTER TABLE patch_submissions
    ADD COLUMN IF NOT EXISTS base_sha VARCHAR(40),
    ADD COLUMN IF NOT EXISTS snapshot_id UUID
        REFERENCES task_snapshots(snapshot_id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS symbol_graph_versions (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(workspace_id)
        ON DELETE CASCADE,
    version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merge_conflicts (
    conflict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id)
        ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES dag_tasks(task_id) ON DELETE CASCADE,
    submission_id UUID,
    merge_id UUID,
    kind VARCHAR(64) NOT NULL,
    paths TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_merge_conflicts_task
    ON merge_conflicts (task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_merge_conflicts_open
    ON merge_conflicts (workspace_id, created_at DESC)
    WHERE status = 'OPEN';

INSERT INTO schema_migrations (version)
VALUES ('v4_013_code_intelligence')
ON CONFLICT (version) DO NOTHING;
