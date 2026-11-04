-- v3_82: deploy gate v2 — every project can deploy its merged images.
--
-- A project turns deploys on by naming a target: the computer on the server
-- that runs them (it dials out over nerve; Veynod holds no SSH key), the branch
-- whose merges deploy, and when (on every release, in a schedule window, or
-- only when a person presses the button). CI posts a release per merged commit:
-- the image digest of every service. A deploy run moves one target to one
-- release, and is delivered as a runner placement with step 'deploy'.
--
-- Every row carries its workspace's tenant (derived by trigger), like every
-- other owned table. Expand-only and idempotent.

CREATE TABLE IF NOT EXISTS deploy_targets (
    target_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    name VARCHAR(64) NOT NULL DEFAULT 'production',
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    branch VARCHAR(128) NOT NULL DEFAULT 'main',
    -- auto: deploy every release; scheduled: deploy the newest release inside a
    -- window; manual: only a person starts a run.
    trigger_mode VARCHAR(16) NOT NULL DEFAULT 'manual'
        CHECK (trigger_mode IN ('auto', 'scheduled', 'manual')),
    -- {"days": [1..7], "start": "HH:MM", "end": "HH:MM", "tz": "Europe/Berlin"}
    schedule JSONB,
    host_id VARCHAR(64),
    health_url VARCHAR(1024),
    health_timeout_s INTEGER NOT NULL DEFAULT 120 CHECK (health_timeout_s BETWEEN 5 AND 1800),
    auto_rollback BOOLEAN NOT NULL DEFAULT TRUE,
    -- CI authenticates its release posts with this (only the hash is kept).
    release_token_hash VARCHAR(128),
    deployed_release_id UUID,
    deployed_commit VARCHAR(64),
    deployed_manifest JSONB,
    deployed_at TIMESTAMPTZ,
    created_by VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, name)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_deploy_targets_release_token
    ON deploy_targets (release_token_hash) WHERE release_token_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS deploy_releases (
    release_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    -- Monotonic per project: a release never deploys over a newer one.
    seq BIGINT NOT NULL,
    commit_sha VARCHAR(64) NOT NULL,
    branch VARCHAR(128) NOT NULL,
    manifest JSONB NOT NULL,
    built JSONB NOT NULL DEFAULT '[]',
    source VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, seq),
    UNIQUE (project_id, branch, commit_sha)
);

CREATE TABLE IF NOT EXISTS deploy_runs (
    run_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    target_id UUID NOT NULL REFERENCES deploy_targets(target_id) ON DELETE CASCADE,
    release_id UUID NOT NULL REFERENCES deploy_releases(release_id) ON DELETE CASCADE,
    trigger VARCHAR(16) NOT NULL CHECK (trigger IN ('auto', 'scheduled', 'manual', 'rollback')),
    status VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'offered', 'running', 'deployed', 'rolled_back',
                          'failed', 'skipped')),
    plan JSONB,
    from_commit VARCHAR(64),
    placement_id VARCHAR(64),
    reason VARCHAR(120),
    report JSONB,
    requested_by VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ
);

-- One run in flight per target: a second one waits as queued and is coalesced.
CREATE UNIQUE INDEX IF NOT EXISTS uq_deploy_runs_in_flight
    ON deploy_runs (target_id) WHERE status IN ('offered', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS uq_deploy_runs_queued
    ON deploy_runs (target_id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_deploy_runs_target
    ON deploy_runs (target_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_derive_tenant ON deploy_targets;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON deploy_targets FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
DROP TRIGGER IF EXISTS trg_derive_tenant ON deploy_releases;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON deploy_releases FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();
DROP TRIGGER IF EXISTS trg_derive_tenant ON deploy_runs;
CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
    ON deploy_runs FOR EACH ROW EXECUTE FUNCTION agentgraph_derive_tenant();

-- A deploy is a placement too: same nerve delivery, receipts and report.
ALTER TABLE runner_placements DROP CONSTRAINT IF EXISTS runner_placements_step_check;
ALTER TABLE runner_placements ADD CONSTRAINT runner_placements_step_check
    CHECK (step IN ('work', 'review', 'merge', 'supervise', 'deploy'));
