-- v3.104 State machines are declared, and the database holds every writer to them.
--
-- A status column used to be a state machine only by convention: about thirty
-- places in eight files wrote collab_tasks.status, raw SQL and ORM alike, and
-- nothing said which moves were legal. ``app/state_machines.py`` now declares
-- each machine; at start the server copies its moves into ``state_transitions``
-- and puts ``agentgraph_guard_transition`` on each column (sql/functions).
--
-- ``state_guard.mode``: ``enforce`` refuses a move that is not declared
-- (ERR_ILLEGAL_TRANSITION, a 409 naming the moves that are allowed);
-- ``record`` lets it through and counts it in ``state_transition_violations``,
-- for a deployment to run before it enforces.
--
-- Expand-only and idempotent.

CREATE TABLE IF NOT EXISTS state_transitions (
    table_name  TEXT NOT NULL,
    column_name TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    PRIMARY KEY (table_name, column_name, from_state, to_state)
);

CREATE TABLE IF NOT EXISTS state_guard (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mode      TEXT NOT NULL DEFAULT 'record' CHECK (mode IN ('record', 'enforce'))
);
INSERT INTO state_guard (singleton, mode) VALUES (TRUE, 'record') ON CONFLICT DO NOTHING;

-- One row per move that was let through undeclared. Append-only on purpose: a
-- counter row upserted by every writer makes two transactions making the same
-- move wait for each other, and a writer that waits on the other at the
-- application level then never finishes. Read it grouped (state_machines.py).
CREATE TABLE IF NOT EXISTS state_transition_violations (
    id          BIGSERIAL PRIMARY KEY,
    table_name  TEXT NOT NULL,
    column_name TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    -- Who made it: 'veynod' is the application; anything else is a person or a
    -- tool working on the database directly.
    writer      TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_state_transition_violations_at
    ON state_transition_violations (at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentgraph_app') THEN
        GRANT SELECT ON state_transitions, state_guard TO agentgraph_app;
        REVOKE INSERT, UPDATE, DELETE ON state_transitions, state_guard FROM agentgraph_app;
        GRANT SELECT, INSERT ON state_transition_violations TO agentgraph_app;
        REVOKE UPDATE, DELETE ON state_transition_violations FROM agentgraph_app;
        GRANT USAGE, SELECT ON SEQUENCE state_transition_violations_id_seq TO agentgraph_app;
    END IF;
END $$;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('v3_104_state_machines', NOW())
ON CONFLICT (version) DO NOTHING;
