-- Agent Runtime 007: tenant IDs are stable text IDs (AgentGraph uses ten_...).

ALTER TABLE {{schema}}.agent_runs
    ALTER COLUMN tenant_id TYPE TEXT USING tenant_id::text;
