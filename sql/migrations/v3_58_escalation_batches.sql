-- v3.58 Batched owner questions (Loop v1, spec-auto-loop-mode v2: semi-automatic).
--
-- Under semi automation the agents decide small things and ask the owner the
-- big ones (money, identity, legal, deleting data, external commitments) in
-- one batch rather than one by one. A queued question waits with notified_at
-- NULL; the upkeep sends each owner one batch once the oldest is old enough
-- or enough have piled up. Every other question is sent when it is raised.

ALTER TABLE escalations ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
UPDATE escalations SET notified_at = created_at WHERE notified_at IS NULL;
ALTER TABLE escalations ALTER COLUMN notified_at SET DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_escalations_unsent
    ON escalations (workspace_id, created_at) WHERE notified_at IS NULL;
