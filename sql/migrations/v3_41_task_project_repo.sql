-- Optional task routing metadata. Existing tasks remain valid with NULL values.
ALTER TABLE collab_tasks
    ADD COLUMN IF NOT EXISTS project VARCHAR(128),
    ADD COLUMN IF NOT EXISTS repo VARCHAR(256);
