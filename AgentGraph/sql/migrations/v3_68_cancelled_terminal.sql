-- v3_68: CANCELLED is a terminal product-task state
--
-- v3_41 only froze DONE/FAILED.  Cancelled tasks must not be edited either;
-- otherwise a stale worker/undo can resurrect work that was explicitly
-- withdrawn.

CREATE OR REPLACE FUNCTION enforce_task_immutability()
RETURNS TRIGGER AS $$
DECLARE
    authorized TEXT := coalesce(
        current_setting('veynod.acceptance_change', true), ''
    );
BEGIN
    IF OLD.status IN ('DONE', 'FAILED', 'CANCELLED') THEN
        IF NEW.status IS DISTINCT FROM OLD.status
           OR NEW.acceptance IS DISTINCT FROM OLD.acceptance
           OR NEW.description IS DISTINCT FROM OLD.description THEN
            RAISE EXCEPTION
                'ERR_TERMINAL_STATE_IMMUTABLE: task % is in terminal state %',
                OLD.task_key, OLD.status;
        END IF;
    END IF;

    IF NEW.status = 'IN_PROGRESS' THEN
        NEW.acceptance_locked := TRUE;
    END IF;

    IF OLD.acceptance_locked
       AND NEW.acceptance IS DISTINCT FROM OLD.acceptance
       AND authorized <> 'authorized' THEN
        RAISE EXCEPTION
            'ERR_ACCEPTANCE_LOCKED: acceptance criteria for % are locked after the first claim',
            OLD.task_key;
    END IF;

    IF OLD.acceptance_locked
       AND NEW.context_docs IS DISTINCT FROM OLD.context_docs THEN
        RAISE EXCEPTION
            'ERR_CONTEXT_DOCS_LOCKED: pinned context_docs for % are locked after the first claim',
            OLD.task_key;
    END IF;

    RETURN NEW;
END
$$ LANGUAGE plpgsql;
