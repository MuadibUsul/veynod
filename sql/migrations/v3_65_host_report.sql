-- v3.65 What a runner says about itself (WAKE_AND_DISPATCH S-10, up-hub-p1-up-hub).
--
-- The runner's hello carries a capabilities report: whether the agent session
-- it starts is pinned to the runner's own Veynod identity (`mcp_pinning`) and
-- the runtime adapter that runs it. An unpinned session claims with whatever
-- key the runtime's MCP config holds, so its claims are refused (not_assignee)
-- and its reviews may come from the builder. The board doctor names such hosts.

ALTER TABLE runner_hosts ADD COLUMN IF NOT EXISTS capabilities_report JSONB;
