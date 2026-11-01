-- v4_014: immutable-ish delivery receipt for a committed task (Phase 6).
--
-- The Git commit is the artifact; this row is the auditable product record of
-- which task/submission/gate evidence produced it.  One task delivers once.

CREATE TABLE IF NOT EXISTS delivery_records (
    delivery_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id)
        ON DELETE CASCADE,
    task_id UUID NOT NULL UNIQUE REFERENCES dag_tasks(task_id)
        ON DELETE CASCADE,
    submission_id UUID,
    merge_id UUID,
    commit_sha VARCHAR(40) NOT NULL,
    base_sha VARCHAR(40),
    merge_mode VARCHAR(32) NOT NULL,
    snapshot_id UUID,
    gate_results JSONB NOT NULL DEFAULT '[]'::JSONB,
    evidence_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_delivery_workspace
    ON delivery_records (workspace_id, created_at DESC);

INSERT INTO schema_migrations (version)
VALUES ('v4_014_delivery_records')
ON CONFLICT (version) DO NOTHING;
