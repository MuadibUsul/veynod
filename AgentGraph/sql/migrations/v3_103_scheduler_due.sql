-- v3.103 Each workspace is scheduled on its own.
--
-- The sweep used to visit every workspace with a computer on every round, one
-- process for the whole platform, so a new task waited behind everyone else's
-- workspaces. Changes now schedule their own workspace at once (a "kick"), and
-- what only time brings about (an offer not taken, a lease running out, a
-- computer gone quiet, a supervise round due) is written here as the time the
-- workspace next needs a look. The sweep takes only workspaces whose time has
-- come, and takes each by moving that time forward, so several server
-- processes share the sweep without a platform-wide lock.
--
-- Expand-only and idempotent.

ALTER TABLE runner_hosts ADD COLUMN IF NOT EXISTS next_pass_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_runner_hosts_next_pass
    ON runner_hosts (next_pass_at)
    WHERE status <> 'revoked';

INSERT INTO schema_migrations (version, applied_at)
VALUES ('v3_103_scheduler_due', NOW())
ON CONFLICT (version) DO NOTHING;
