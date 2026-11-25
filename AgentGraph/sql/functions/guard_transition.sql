-- Refuse (or, in record mode, count) a change of a declared state column to a
-- value its machine does not allow from the old one. TG_ARGV[0] is the column.
-- Refused only for the application's connections (application_name 'veynod');
-- an undo (veynod.state_override = 'undo') may restore any earlier state.
-- See app/state_machines.py.
CREATE OR REPLACE FUNCTION agentgraph_guard_transition() RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    col     TEXT := TG_ARGV[0];
    old_val TEXT := to_jsonb(OLD) ->> TG_ARGV[0];
    new_val TEXT := to_jsonb(NEW) ->> TG_ARGV[0];
    allowed TEXT[];
BEGIN
    IF old_val IS NOT DISTINCT FROM new_val OR old_val IS NULL OR new_val IS NULL THEN
        RETURN NEW;
    END IF;
    IF EXISTS (SELECT 1 FROM state_transitions
               WHERE table_name = TG_TABLE_NAME AND column_name = col
                 AND from_state = old_val AND to_state = new_val) THEN
        RETURN NEW;
    END IF;
    -- Undo restores a snapshot: a move back to a state the row really had.
    IF current_setting('veynod.state_override', true) = 'undo' THEN
        RETURN NEW;
    END IF;
    -- Only the application is held to the declared moves; anyone else (an
    -- operator repairing a row, a test arranging a scenario) is recorded.
    IF (SELECT mode FROM state_guard WHERE singleton) = 'enforce'
       AND current_setting('application_name', true) = 'veynod' THEN
        SELECT coalesce(array_agg(to_state ORDER BY to_state), '{}') INTO allowed
        FROM state_transitions
        WHERE table_name = TG_TABLE_NAME AND column_name = col AND from_state = old_val;
        RAISE EXCEPTION 'ERR_ILLEGAL_TRANSITION: %.% cannot go from % to %',
            TG_TABLE_NAME, col, old_val, new_val
            USING DETAIL = json_build_object(
                'table', TG_TABLE_NAME, 'column', col, 'current', old_val,
                'requested', new_val, 'allowed', allowed)::text;
    END IF;
    -- Append-only: never waits on another writer (see v3_104).
    INSERT INTO state_transition_violations
        (table_name, column_name, from_state, to_state, writer)
    VALUES (TG_TABLE_NAME, col, old_val, new_val, current_setting('application_name', true));
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentgraph_app') THEN
        GRANT EXECUTE ON FUNCTION agentgraph_guard_transition() TO agentgraph_app;
    END IF;
END $$;
