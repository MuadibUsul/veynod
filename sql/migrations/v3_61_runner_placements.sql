-- v3.61 Runner placements (full-managed Loop, step 3: runner-3-scheduler).
--
-- One row per decision to put a task on a host. Under full automation the
-- scheduler assigns the task to the host's agent (status offered, then claimed
-- once the host takes it) and the assignment pokes the host. Under manual and
-- semi automation it only records a suggestion. A placement ends as
-- delivered, failed (the task came back), moved (host offline, out of quota,
-- revoked or slow to claim; reason says which) or superseded (someone else
-- assigned it). When no host qualifies the row says why (unplaced), in words
-- the owner can act on. placement_id doubles as the idempotency key a runner
-- uses to ignore a repeated poke.

CREATE TABLE IF NOT EXISTS runner_placements (
    placement_id VARCHAR(40) PRIMARY KEY DEFAULT agentgraph_new_id('plc'),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tenant_id VARCHAR(40) NOT NULL,
    task_id UUID NOT NULL REFERENCES collab_tasks(id) ON DELETE CASCADE,
    task_key VARCHAR(128) NOT NULL,
    host_id VARCHAR(40) REFERENCES runner_hosts(host_id) ON DELETE SET NULL,
    agent_id VARCHAR(40),
    mode VARCHAR(8) NOT NULL CHECK (mode IN ('assign', 'suggest')),
    status VARCHAR(16) NOT NULL CHECK (status IN (
        'suggested', 'offered', 'claimed', 'unplaced',
        'delivered', 'failed', 'moved', 'superseded', 'withdrawn')),
    -- The task's lease epoch when it was offered: a higher one means the host took it.
    lease_epoch INT NOT NULL DEFAULT 0,
    reason VARCHAR(64),
    note VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_runner_placements_task
    ON runner_placements (workspace_id, task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runner_placements_host
    ON runner_placements (host_id, status);
-- At most one open placement per task.
CREATE UNIQUE INDEX IF NOT EXISTS uq_runner_placements_open
    ON runner_placements (task_id)
    WHERE status IN ('suggested', 'offered', 'claimed', 'unplaced');

DO $$
BEGIN
    EXECUTE 'DROP TRIGGER IF EXISTS trg_derive_tenant ON runner_placements';
    EXECUTE 'CREATE TRIGGER trg_derive_tenant BEFORE INSERT OR UPDATE OF '
            'workspace_id, tenant_id ON runner_placements FOR EACH ROW '
            'EXECUTE FUNCTION agentgraph_derive_tenant()';
    EXECUTE 'ALTER TABLE runner_placements ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE runner_placements FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON runner_placements';
    EXECUTE 'CREATE POLICY tenant_isolation ON runner_placements '
            'USING (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = ANY(agentgraph_tenant_scope()) AND workspace_id = '
            'NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON runner_placements TO agentgraph_app';
END $$;
