-- v3_80: one Kernel node per Diagram chunk.
--
-- Before this, the Diagram had no Kernel binding at all: a dispatched chunk was
-- executed by a host runner over a pull request and "merged" by a human call
-- that only flipped a row.  A chunk now names the Kernel task that delivers it,
-- and the unique index is the backstop that a retried dispatch cannot create a
-- second node for the same chunk.
--
-- Expand-only: existing chunks keep NULL and continue on the legacy path.

ALTER TABLE diagram_chunks
    ADD COLUMN IF NOT EXISTS kernel_task_id UUID,
    ADD COLUMN IF NOT EXISTS kernel_workspace_id UUID,
    ADD COLUMN IF NOT EXISTS scope_hash CHAR(64),
    ADD COLUMN IF NOT EXISTS merged_commit_sha VARCHAR(64);

-- A chunk has at most one Kernel node; at most one chunk may own a node.
CREATE UNIQUE INDEX IF NOT EXISTS uq_diagram_chunks_kernel_task
    ON diagram_chunks (kernel_task_id) WHERE kernel_task_id IS NOT NULL;

-- Projection idempotency: one row per (chunk, kernel event) so a replayed
-- commit event cannot unlock downstream chunks twice.
CREATE TABLE IF NOT EXISTS diagram_chunk_projections (
    dedupe_key TEXT PRIMARY KEY,
    chunk_id BIGINT NOT NULL REFERENCES diagram_chunks(id) ON DELETE CASCADE,
    task_key VARCHAR(128),
    action VARCHAR(32) NOT NULL,
    commit_sha VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_diagram_chunk_projections_chunk
    ON diagram_chunk_projections (chunk_id, created_at DESC);

-- Kernel-owned chunks must carry the frozen scope they were created with; a
-- chunk without a scope stays legacy and is never treated as Kernel-owned.
ALTER TABLE diagram_chunks DROP CONSTRAINT IF EXISTS ck_diagram_chunks_kernel_scope;
ALTER TABLE diagram_chunks ADD CONSTRAINT ck_diagram_chunks_kernel_scope
    CHECK (
        kernel_task_id IS NULL
        OR (scope_hash IS NOT NULL AND array_length(allowed_write_paths, 1) > 0)
    );
