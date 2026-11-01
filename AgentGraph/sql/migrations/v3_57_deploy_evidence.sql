-- v3.57 Deploy evidence for DONE (Loop v1, spec-auto-loop-mode v2 A-03).
--
-- The runner reports which board tasks each deploy carried (the task keys the
-- merged pull requests name). Under full automation a code task is approved
-- to DONE only once a deploy that carried it passed its smoke test (G-05);
-- before that an approval leaves it in HANDOFF, waiting for the deploy.

ALTER TABLE deploy_gate_runs
    ADD COLUMN IF NOT EXISTS task_keys TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_deploy_gate_runs_task_keys
    ON deploy_gate_runs USING GIN (task_keys);
