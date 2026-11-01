-- ============================================================================
-- Veynod Kernel v4.0 - Genesis Schema
-- PostgreSQL 16+. Idempotent: safe to run on every application start.
--
-- Symbol paths are stored in the canonical, hierarchical form
--   src/order/pricing.py::PricingEngine::calc_discount
-- The write-set lock algebra expands them into:
--   write_locks    = {the path itself}
--   ancestor_locks = {src, src/order, src/order/pricing.py,
--                     src/order/pricing.py::PricingEngine}
-- Two active tasks are disjoint iff none of the three GIN overlap tests hits.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

DO $$
BEGIN
    CREATE TYPE artifact_kind AS ENUM ('contract', 'stub', 'implementation', 'test');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE artifact_status AS ENUM ('DRAFT', 'FROZEN', 'STALE', 'COMMITTED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE task_type AS ENUM (
        'DESIGN_ARCHITECTURE',
        'DESIGN_CONTRACT',
        'WRITE_TEST',
        'IMPLEMENT_BODY',
        'REVISE_CONTRACT',
        'FIX_INTEGRATION'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE task_status AS ENUM (
        'BLOCKED',
        'READY',
        'LEASED',
        'VERIFYING',
        'SUSPENDED',
        'SUSPENDED_ESCALATED',
        'COMMITTED',
        'FAILED',
        'SPLIT_ARCHIVED'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- 1. Workspaces (one kernel deployment normally serves one project)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(128) NOT NULL,
    repo_path TEXT NOT NULL,
    main_branch VARCHAR(64) NOT NULL DEFAULT 'main',
    head_commit_sha VARCHAR(40) NOT NULL,
    language_toolchain VARCHAR(32) NOT NULL DEFAULT 'python',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 2. Artifacts: AST symbol contract nodes (bipartite node class A)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS code_artifacts (
    artifact_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    symbol_path TEXT NOT NULL,
    kind artifact_kind NOT NULL,
    status artifact_status NOT NULL DEFAULT 'DRAFT',
    ast_signature TEXT NOT NULL DEFAULT '',
    docstring TEXT NOT NULL DEFAULT '',
    signature_hash VARCHAR(64) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL DEFAULT '',
    version INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, symbol_path)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_workspace_symbol
    ON code_artifacts (workspace_id, symbol_path);
CREATE INDEX IF NOT EXISTS idx_artifacts_file_path
    ON code_artifacts (workspace_id, file_path);

-- ---------------------------------------------------------------------------
-- 3. DAG tasks (bipartite node class B)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dag_tasks (
    task_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    parent_task_id UUID REFERENCES dag_tasks(task_id),
    title VARCHAR(256) NOT NULL,
    spec_prompt TEXT NOT NULL,
    type task_type NOT NULL,
    status task_status NOT NULL DEFAULT 'BLOCKED',
    priority INT NOT NULL DEFAULT 100,

    -- Kahn topological in-degree. 0 => READY (after unblock handling).
    in_degree INT NOT NULL DEFAULT 0 CHECK (in_degree >= 0),

    -- Read / write contract sets.
    read_artifact_ids UUID[] NOT NULL DEFAULT '{}'::UUID[],
    target_artifact_ids UUID[] NOT NULL DEFAULT '{}'::UUID[],
    test_artifact_ids UUID[] NOT NULL DEFAULT '{}'::UUID[],

    -- Hierarchical write-set locks (see the header comment).
    write_locks TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    ancestor_locks TEXT[] NOT NULL DEFAULT '{}'::TEXT[],

    -- Lease state.
    leased_by_runner VARCHAR(128),
    lease_token_hash VARCHAR(64),
    lease_expires_at TIMESTAMPTZ,
    base_commit_sha VARCHAR(40),
    result_commit_sha VARCHAR(40),

    retry_count INT NOT NULL DEFAULT 0,
    max_retries INT NOT NULL DEFAULT 3,
    last_error_traceback TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dag_tasks_write_locks
    ON dag_tasks USING GIN (write_locks);
CREATE INDEX IF NOT EXISTS idx_dag_tasks_ancestor_locks
    ON dag_tasks USING GIN (ancestor_locks);
CREATE INDEX IF NOT EXISTS idx_dag_tasks_read_artifacts
    ON dag_tasks USING GIN (read_artifact_ids);
CREATE INDEX IF NOT EXISTS idx_dag_tasks_ready_queue
    ON dag_tasks (workspace_id, priority DESC, created_at ASC)
    WHERE status = 'READY';
CREATE INDEX IF NOT EXISTS idx_dag_tasks_active_leases
    ON dag_tasks (workspace_id, lease_expires_at)
    WHERE status = 'LEASED';
CREATE UNIQUE INDEX IF NOT EXISTS idx_dag_tasks_lease_token
    ON dag_tasks (lease_token_hash)
    WHERE lease_token_hash IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. Topological dependency edges (upstream -> downstream)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_dependencies (
    upstream_task_id UUID NOT NULL REFERENCES dag_tasks(task_id) ON DELETE CASCADE,
    downstream_task_id UUID NOT NULL REFERENCES dag_tasks(task_id) ON DELETE CASCADE,
    PRIMARY KEY (upstream_task_id, downstream_task_id),
    CHECK (upstream_task_id <> downstream_task_id)
);

CREATE INDEX IF NOT EXISTS idx_task_deps_downstream
    ON task_dependencies (downstream_task_id);

-- ---------------------------------------------------------------------------
-- 5. Fine-grained symbol call graph (callee <- caller) for blast-radius work
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbol_edges (
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    caller_artifact_id UUID NOT NULL REFERENCES code_artifacts(artifact_id) ON DELETE CASCADE,
    callee_artifact_id UUID NOT NULL REFERENCES code_artifacts(artifact_id) ON DELETE CASCADE,
    referenced_fields TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    PRIMARY KEY (workspace_id, caller_artifact_id, callee_artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_symbol_edges_callee
    ON symbol_edges (workspace_id, callee_artifact_id);

-- ---------------------------------------------------------------------------
-- 6. Cursor event stream (SSE + preemption)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kernel_events (
    event_seq BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kernel_events_cursor
    ON kernel_events (workspace_id, event_seq);

-- ---------------------------------------------------------------------------
-- 7. Runner machine tokens (Bearer vnk_...)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_tokens (
    token_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    worker_hub_id VARCHAR(128) NOT NULL UNIQUE,
    label VARCHAR(128) NOT NULL,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ
);
