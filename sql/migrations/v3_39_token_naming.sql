-- v3.39 token naming: stable agent identity + generation (TASK-8012EB76).
--
-- Display names live in agent_directory and remain free-form. agent_id is the
-- machine identity; a token generation is one credential for that identity.
-- Existing rows keep generation 1; the directory link backfills agent_id where
-- the token already belongs to an entry.

ALTER TABLE agent_tokens ADD COLUMN IF NOT EXISTS agent_id VARCHAR(40);
ALTER TABLE agent_tokens ADD COLUMN IF NOT EXISTS generation INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent_id
    ON agent_tokens (workspace_id, agent_id, generation);

UPDATE agent_tokens t
SET agent_id = d.agent_id
FROM agent_directory d
WHERE d.workspace_id = t.workspace_id
  AND t.canonical_id = ANY(d.token_ids)
  AND t.agent_id IS NULL;

UPDATE agent_tokens
SET agent_id = left(lower(regexp_replace(agent_signature, '[^a-zA-Z0-9-]+', '-', 'g')), 40)
WHERE agent_id IS NULL;
