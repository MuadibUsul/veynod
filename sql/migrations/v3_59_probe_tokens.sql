-- v3.59 Probe tokens (playtest bug 20, R7): quota apart from business tokens.
--
-- A token issued with probe=true is for trying the platform out. It does not
-- count toward the workspace, branch or issuer token quotas, its usage is
-- charged to itself only (never to the tokens above it), it cannot issue
-- tokens and it lives at most PROBE_TOKEN_MAX_TTL_HOURS. Probes have their own
-- cap per workspace (PROBE_TOKEN_QUOTA).

ALTER TABLE agent_tokens ADD COLUMN IF NOT EXISTS is_probe BOOLEAN NOT NULL DEFAULT FALSE;
