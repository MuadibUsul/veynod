-- v3.52 Diagram dispatch, automation stage (spec-diagram-dispatch v3, sections 8-10, 14).
--
-- * watchers: who is poked when a chunk unblocks or merges (M-14, M-26).
-- * commands: compile / lint / test commands the sandbox runs (M-19, section 14).
-- * semantic_rules: rules a chunk adds to the three default review checks (M-20).
-- * context_slice: what the dispatcher injected and from where (M-16, auditable).
-- * submission: the implementer's latest submission (files, commit, diff) (M-17, M-21).
-- * ci: the deterministic gate's result for that submission (M-19).
-- * block_kind: why a chunk waits: dependency or file_lock (M-18).
ALTER TABLE diagrams ADD COLUMN IF NOT EXISTS watchers TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS commands JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS semantic_rules TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS context_slice JSONB;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS submission JSONB;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS ci JSONB;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS block_kind VARCHAR(16)
    CHECK (block_kind IS NULL OR block_kind IN ('dependency', 'file_lock'));
