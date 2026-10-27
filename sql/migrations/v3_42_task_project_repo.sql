-- v3.42 Optional task routing metadata.
-- Distinct from schema 3.41 (storage invariants). Existing tasks stay valid
-- when project/repo are NULL. REST, MCP and handoff may omit both fields.
-- The v0.2 protocol attribution block stays tri-state and is not stored here.
ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS project VARCHAR(128),
    ADD COLUMN IF NOT EXISTS repo VARCHAR(256);
