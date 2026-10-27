-- v3.44 SUPER_ADMIN role (spec-super-admin-role v1 R-01).
--
-- The agent_tokens.role_scope CHECK predates the role; widen it. No other table
-- constrains the role, and no data changes are needed.
BEGIN;

ALTER TABLE agent_tokens DROP CONSTRAINT IF EXISTS agent_tokens_role_scope_check;
ALTER TABLE agent_tokens ADD CONSTRAINT agent_tokens_role_scope_check
    CHECK (role_scope IN ('SCOUT', 'ANALYST', 'AUDITOR', 'EXECUTOR', 'SUPER_ADMIN'));

COMMIT;
