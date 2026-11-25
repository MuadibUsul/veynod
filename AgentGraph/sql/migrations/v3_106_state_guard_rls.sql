-- v3.106 Grant least-privilege state-machine table access to the RLS app role.
--
-- Under RLS_ENABLED=true, tenant-scoped sessions switch via SET LOCAL ROLE to
-- agentgraph_app. Updating any guarded status column (such as runner_hosts.status
-- on pause/resume/revoke) fires agentgraph_guard_transition(), which reads
-- state_transitions and state_guard and appends undeclared moves to
-- state_transition_violations.
--
-- Expand-only and idempotent.

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
VALUES ('v3_106_state_guard_rls', NOW())
ON CONFLICT (version) DO NOTHING;
