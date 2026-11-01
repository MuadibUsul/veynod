-- v3_71: product projects, runtime usage metering and per-tenant limits.
--
-- Project is a product grouping above tasks (tasks already carry a
-- ``project`` label).  Runtime usage is mirrored into the existing
-- usage_ledger exactly once per Run, and tenant limits are enforced by the
-- AgentGraph adapter before it creates a Run.

CREATE TABLE IF NOT EXISTS projects (
    project_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    repo VARCHAR(256),
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_projects_workspace
    ON projects (workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS runtime_usage_metered (
    run_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    model_calls NUMERIC NOT NULL DEFAULT 0,
    prompt_tokens NUMERIC NOT NULL DEFAULT 0,
    completion_tokens NUMERIC NOT NULL DEFAULT 0,
    cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_runtime_limits (
    tenant_id TEXT PRIMARY KEY,
    max_model_calls NUMERIC,
    max_cost_usd NUMERIC(12, 6),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
