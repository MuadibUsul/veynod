# Veynod 统一迁移计划（Phase 1）

原则：Expand → Migrate → Contract。所有迁移幂等、可重复执行、有回滚路径；不删除历史数据。

---

## 0. 迁移总览

| 阶段 | Schema | 变更 | 回滚 |
|---|---|---|---|
| E1 | `agent_runtime` | 新表 `external_invocations`、`runtime_wakeups`、`runtime_tool_audit`；`agent_runs` 增 `project_id/repo_path/trace_id` | `DROP TABLE`（新表无下游） |
| E2 | `agent_runtime` | `RunState` 增 `WAITING_EXTERNAL`（代码层，无 DDL） | 代码回退 |
| E3 | `public`（AgentGraph） | `collab_tasks` 增 `correlation JSONB`、`active_run_id UUID`；新表 `kernel_bindings`、`project_repositories` | `DROP COLUMN`（列可为空，旧代码忽略） |
| E4 | `public`（Kernel） | syscall 平面接受 `X-Workspace-Id`；新增 `runtime` 服务面端点；`patch_submissions.correlation`（v4_015）；`dag_tasks.runtime_request_key`（v4_016） | 停用 header（缺省回落默认 workspace）；新列可空 |
| M1 | 数据 | 从 `collab_tasks.input_context` 回填 `correlation`/`active_run_id`；从既有 code 任务回填 `kernel_bindings` | 只写新列，回滚时忽略 |
| C1 | 旧接口 | `POST /v1/code/runs` 语义变更：只建任务+Run，不再创建 Kernel 任务 | feature flag `CODE_ENGINE_LEGACY_DIRECT=1` 恢复旧行为（保留一个版本） |

---

## 1. Runtime schema（`agent_runtime/migrations/008_external_wait.sql`）

```sql
CREATE TABLE IF NOT EXISTS {{schema}}.external_invocations (
    external_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES {{schema}}.agent_runs(run_id) ON DELETE CASCADE,
    step_id UUID,
    tool TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    submission_id UUID,
    kernel_workspace_id UUID,
    kernel_task_id UUID,
    status TEXT NOT NULL,          -- PENDING | FINAL | FAILED | ABANDONED
    request JSONB NOT NULL DEFAULT '{}'::JSONB,
    result JSONB,
    deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS {{schema}}.runtime_wakeups (
    wakeup_id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,            -- SUBMISSION_FINAL | TASK_STATE | CANCEL
    ref_id TEXT NOT NULL,
    kernel_event_seq BIGINT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (kind, ref_id, kernel_event_seq)
);

CREATE TABLE IF NOT EXISTS {{schema}}.runtime_tool_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL,
    tool TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms INT NOT NULL,
    error_class TEXT,
    kernel_status INT,
    correlation JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE {{schema}}.agent_runs ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE {{schema}}.agent_runs ADD COLUMN IF NOT EXISTS repo_path TEXT;
ALTER TABLE {{schema}}.agent_runs ADD COLUMN IF NOT EXISTS trace_id TEXT;
CREATE INDEX IF NOT EXISTS idx_runtime_runs_active ON {{schema}}.agent_runs (trace_id);
```

`RuntimeWorker` 通过 `RunStore.wake_deferred(...)` 在 `claim_run` 之前消费 wakeups，
把匹配的 `external_invocations` 置 FINAL 并把 Run 从 `WAITING_EXTERNAL` 拉回 `RUNNING`。

---

## 2. AgentGraph schema（`v3_75` ~ `v3_78`）

`v3_75` 是本轮的主迁移；`v3_76`（event_inbox 派生 tenant，列保持可空）、`v3_77`
（kernel_bindings / project_repositories 的派生 tenant）、`v3_78`（可空性收尾）是
针对已应用数据库的补充迁移，全部幂等。


```sql
ALTER TABLE collab_tasks ADD COLUMN IF NOT EXISTS correlation JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE collab_tasks ADD COLUMN IF NOT EXISTS active_run_id UUID;

CREATE TABLE IF NOT EXISTS kernel_bindings (
    binding_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project TEXT NOT NULL DEFAULT '',
    repo_path TEXT NOT NULL,
    kernel_base_url TEXT NOT NULL,
    kernel_workspace_id UUID NOT NULL,
    credential_ref TEXT NOT NULL DEFAULT 'KERNEL_RUNTIME_TOKEN',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, project, repo_path)
);

CREATE TABLE IF NOT EXISTS project_repositories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_id UUID NOT NULL,
    repo_path TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    kernel_workspace_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, repo_path)
);

-- 唯一活动 Run：同一 task 至多一个非终态 Run（由应用层维护，DB 侧用部分索引兜底）
CREATE UNIQUE INDEX IF NOT EXISTS uq_collab_tasks_active_run
    ON collab_tasks (active_run_id) WHERE active_run_id IS NOT NULL;
```

`projects` 表已存在（`v3_*`），无需重建。

---

## 3. Kernel 协议升级（向后兼容）

- 所有 syscall 接受可选请求头 `X-Workspace-Id: <uuid>`。
  - 未提供：保持现状（`VN_DEFAULT_WORKSPACE_ID` 或最早 workspace）。
  - 提供：必须是 token 绑定的 workspace（否则 403），并由 engine 使用该 workspace。
- 新增 runner-plane 端点：
  - `GET /v1/runner/tasks/{task_id}/evidence`（runner token，workspace 校验）
  - `POST /v1/runtime/tasks/{task_id}/cancel`（服务身份 `X-Veynod-Service-Key` + `X-Workspace-Id`）
- 不改变现有端点路径、参数、响应；旧 Runner 完全兼容。

---

## 4. 数据回填（M1）

```sql
-- correlation 回填（幂等）
UPDATE collab_tasks
SET correlation = jsonb_strip_nulls(jsonb_build_object(
      'run_id', input_context->'agent_runtime'->>'run_id',
      'kernel_workspace_id', input_context->'kernel'->>'workspace_id',
      'kernel_task_id', input_context->'kernel'->>'task_id'))
WHERE correlation = '{}'::jsonb
  AND (input_context ? 'agent_runtime' OR input_context ? 'kernel');

-- active_run_id 回填（仅非终态任务）
UPDATE collab_tasks
SET active_run_id = (input_context->'agent_runtime'->>'run_id')::uuid
WHERE active_run_id IS NULL
  AND status IN ('TODO','READY','IN_PROGRESS')
  AND input_context->'agent_runtime'->>'run_id' IS NOT NULL;

-- kernel_bindings 回填（从历史 code 任务）
INSERT INTO kernel_bindings (workspace_id, project, repo_path, kernel_base_url,
                             kernel_workspace_id, credential_ref)
SELECT DISTINCT ON (t.workspace_id, COALESCE(t.project,''), COALESCE(t.repo,''))
       t.workspace_id, COALESCE(t.project,''), COALESCE(t.repo,''),
       :kernel_base_url,
       (t.input_context->'kernel'->>'workspace_id')::uuid,
       'KERNEL_RUNTIME_TOKEN'
FROM collab_tasks t
WHERE t.input_context->'kernel'->>'workspace_id' IS NOT NULL
ON CONFLICT (workspace_id, project, repo_path) DO NOTHING;
```

回填脚本 `scripts/backfill_unified.py`（幂等，可重复执行）。

---

## 5. 旧接口兼容期

| 接口 | 处理 | 兼容期 |
|---|---|---|
| `POST /v1/code/runs` | 语义改为"建产品任务 + 启动 Run"；响应保留 `kernel_task_id`（可为空） | 2 个版本；超期后 `410` 并指向 `/v1/tasks/{key}/run` |
| `GET /v1/code/runs*` | 保留只读视图 | 同上 |
| `POST /v1/tasks/{key}/run` | 保留，成为唯一入口 | 长期 |
| `/dashboard/code/`、`/dashboard/agent-runs/`、`/dashboard/projects/` | 302 → `/dashboard/`；内部视图 `/dashboard/internal/*` | 保留重定向 |
| Kernel `POST /v1/syscalls/submit_patch`（同步） | Kernel 已有 `VN_LEGACY_SUBMIT_MODE=reject` 机制 | 沿用 |
| Kernel Runner CLI | 保留为开发工具，产品默认不使用 | 长期 |

---

## 6. 回滚手册

1. 代码回滚到上一个 release tag。
2. 新表可保留（旧代码不读）；如需清理：`DROP TABLE IF EXISTS` 上述三张 Runtime 表。
3. `collab_tasks` 新增列可保留（有默认值，旧代码忽略）。
4. Kernel header 路由缺失即回落旧行为，无需回滚。
5. 迁移脚本全部 `IF NOT EXISTS`，重复执行安全。

---

## 7. 执行顺序

```
1) 008 迁移 + RunState.WAITING_EXTERNAL（Runtime）
2) v3_75 迁移 + correlation/active_run_id（AgentGraph）
3) Kernel header 路由 + runner evidence + runtime cancel
4) KernelToolRuntime + external_invocations 持久化
5) task_lifecycle 投影（唯一写入口）
6) 关闭 code_engine 直接创建执行链；scheduler 排他
7) UI 收敛；部署 compose；回填脚本；验收
```
