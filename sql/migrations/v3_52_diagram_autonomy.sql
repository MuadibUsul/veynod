-- v3.52 Diagram dispatch, merge queue and autonomy (spec-diagram-dispatch v3, sections 11-13).
--
-- * Merge queue (M-27): an approved chunk is queued, not merged. The queue is
--   serial: only its head is rebased and re-tested (by the queue controller),
--   and only a verified head is merged, still by someone who did not write it.
-- * Trust and tokens (M-22): every chunk starts at trust 100; a sandbox failure
--   costs 10, a review rejection 20. A token budget, when set, blocks the chunk
--   once spent; only a person raises it.
-- * Circuit breaker (M-23): two consecutive rejections warn, three (or trust
--   under 60) break the circuit: the implementer loses the chunk, which is then
--   upgraded to a stronger executor or sent back to the spec.
-- * Replanning (M-25): a NEED_GRAPH_MUTATION signal or a spec escalation
--   suspends the chunk's unmerged subgraph (BLOCKED_BY_SPEC) until an approved
--   change resumes it.
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS queue_check JSONB;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS trust_score INT NOT NULL DEFAULT 100;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS token_budget INT;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS token_consumed INT NOT NULL DEFAULT 0;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS review_reject_count INT NOT NULL DEFAULT 0;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS escalation_count INT NOT NULL DEFAULT 0;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS graph_mutation_signal JSONB;
ALTER TABLE diagram_chunks ADD COLUMN IF NOT EXISTS trust_log JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE diagram_chunks DROP CONSTRAINT IF EXISTS diagram_chunks_state_check;
ALTER TABLE diagram_chunks ADD CONSTRAINT diagram_chunks_state_check CHECK (state IN
    ('pending', 'dispatched', 'blocked', 'rework', 'merged', 'retired', 'suspended'));
ALTER TABLE diagram_chunks DROP CONSTRAINT IF EXISTS diagram_chunks_block_kind_check;
ALTER TABLE diagram_chunks ADD CONSTRAINT diagram_chunks_block_kind_check CHECK (
    block_kind IS NULL OR block_kind IN ('dependency', 'file_lock', 'budget', 'circuit', 'spec'));
