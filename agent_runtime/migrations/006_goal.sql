-- Agent Runtime 006: run goal / task specification.

ALTER TABLE {{schema}}.agent_runs
    ADD COLUMN IF NOT EXISTS goal TEXT;
