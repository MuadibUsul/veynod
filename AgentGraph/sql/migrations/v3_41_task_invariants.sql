-- v3.41 Task storage invariants (doc://veynod-role-rules §5.2).
--
-- The spec is written against a greenfield `tasks` table; the platform's table
-- is `collab_tasks`. The mapping used here:
--   is_locked         -> acceptance_locked   (added in v3.32)
--   fencing_token     -> lease_epoch         (added in v3.0)
--   acceptance / context_docs already exist  (v3.32 / v3.23)
--
-- Invariants:
--   * acceptance is always a JSON array;
--   * IN_PROGRESS always carries a live lease, a fencing generation and a holder;
--   * REVIEWING always carries the handoff briefing;
--   * accepting a task locks its acceptance and pinned context documents;
--   * terminal tasks (DONE / FAILED) never change status, acceptance or description.
--
-- task_key kebab-case (spec §3.1) is enforced by the ingress layer
-- (protocol/src/index.ts), not here: legacy rows contain slashes and upper case
-- and the current REST/MCP create paths still accept them.
--
-- The creator/human acceptance edit already implemented in
-- app/services/quality.py remains possible: that transaction sets the
-- transaction-local GUC veynod.acceptance_change = 'authorized' after its own
-- authorization and audit record. Any other writer is rejected.
--
-- Idempotent; safe on a fresh schema and on an existing one.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_acceptance_is_array'
    ) THEN
        ALTER TABLE collab_tasks
            ADD CONSTRAINT chk_acceptance_is_array
            CHECK (jsonb_typeof(acceptance) = 'array');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_in_progress_requires_lease'
    ) THEN
        ALTER TABLE collab_tasks
            ADD CONSTRAINT chk_in_progress_requires_lease
            CHECK (
                status <> 'IN_PROGRESS'
                OR (
                    lease_expires_at IS NOT NULL
                    AND lease_epoch > 0
                    AND (assignee IS NOT NULL OR claimed_by IS NOT NULL)
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_reviewing_requires_briefing'
    ) THEN
        ALTER TABLE collab_tasks
            ADD CONSTRAINT chk_reviewing_requires_briefing
            CHECK (status <> 'REVIEWING' OR handoff_brief IS NOT NULL);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION enforce_task_immutability()
RETURNS TRIGGER AS $$
DECLARE
    authorized TEXT := coalesce(
        current_setting('veynod.acceptance_change', true), ''
    );
BEGIN
    IF OLD.status IN ('DONE', 'FAILED') THEN
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

    -- Deliberately no NEW.updated_at := NOW() here (the spec's DDL sets it):
    -- the platform derives "waiting on people" from an explicit updated_at,
    -- and several write paths set it themselves.
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_enforce_task_immutability ON collab_tasks;
CREATE TRIGGER trg_enforce_task_immutability
    BEFORE UPDATE ON collab_tasks
    FOR EACH ROW
    EXECUTE FUNCTION enforce_task_immutability();

COMMIT;
