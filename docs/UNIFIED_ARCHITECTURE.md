# Veynod 统一架构（Phase 1 冻结版）

版本：1.0（2026-11-01）。本文是**最终职责边界**，实施以本文为准；与旧文档冲突时以本文与代码为准。

---

## 1. 产品与系统边界

Veynod 是一个 Agent Coding Runtime：用户提出目标 → 产品任务 → Agent Runtime 理解/规划 →
调用 Kernel 执行代码 → 确定性门禁 → 合并/证据 → Runtime 恢复 → 人类审批 → 交付。

```
USER
  │
  ▼
Veynod 产品面（AgentGraph）          身份 / Tenant / Workspace / Project / Task / 审批 / 文档 / 审计 / UI
  │  唯一产品任务 + 唯一产品状态
  ▼
Agent Runtime                        唯一模型推理与执行循环（Run / Step / Checkpoint / Lease / Delegate）
  │  ModelGateway        ToolRuntime
  │        │                  │
  │        ▼                  ▼
  │   Model Providers     KernelToolRuntime（9 capability，内部服务身份）
  │                             │
  ▼                             ▼
                         Veynod Kernel（headless）
                    Rust SchedulerCore / DAG / 写集锁 / AST
                    Leasing / Worktree / Patch / 5 阶段门禁
                    Merge Coordinator / Git CAS / Evidence / Outbox
                             │
                             ▼
                      Durable Events（kernel_events → outbox → event_inbox → runtime_wakeups → AgentGraph 投影）
```

**唯一执行链**：产品任务 → Agent Run → Kernel 工具调用。Kernel 内部可以有很多 DAG 节点，
但产品任务永远只有一个持有者（一个活动 Run）。

---

## 2. 三个对象的身份与所有权

| 对象 | 表 / 服务 | 唯一权威 | 标识 |
|---|---|---|---|
| Product Task | `collab_tasks` | AgentGraph `task_lifecycle` | `task_key` + `id` |
| Agent Run | `agent_runtime.agent_runs` | Agent Runtime `RunStore` | `run_id`（UUID） |
| Kernel DAG Task | `dag_tasks` | Kernel scheduler/gate | `task_id`（UUID） |
| Submission | `patch_submissions` | Kernel `submission_service` | `submission_id` |
| Gate evidence | `patch_submissions.gate_results` / `/evidence` | Kernel | `submission_id` |
| Merge | `merge_intents` / workspace head | Kernel merge coordinator | `merged_commit_sha` |
| Approval | `collab_tasks.status` REVIEWING→DONE | AgentGraph human review | `task_key` |

**规则**

1. Agent Runtime 只写 `agent_runtime.*`。它通过事件/命令请求产品状态变化，从不直接 UPDATE `collab_tasks`。
2. Kernel 只写自己的 schema，从不写 `collab_tasks`、`agent_runs`。
3. AgentGraph 是**唯一**写 `collab_tasks` 的服务；写入必须经过 `app/services/task_lifecycle.py`。
4. 任何服务不得创建"第二个产品任务"来表达执行实例。

### 关联 ID（贯穿所有日志、事件、审计）

```
tenant_id, workspace_id, project_id, product_task_id (task_key),
run_id, kernel_workspace_id, kernel_task_id, submission_id, trace_id
```

- `trace_id` 在创建 Run 时生成（UUID），写入 Runtime checkpoint、每次工具调用、
  Kernel submission 的 `metadata`、AgentGraph `workspace_events.payload`。
- `collab_tasks.correlation`（新增 JSONB 列）持有 `run_id / kernel_workspace_id / kernel_task_id / submission_id / trace_id` 的最新值。
- `agent_runtime.external_invocations` 持有 `submission_id`、`kernel_task_id`、`correlation`。

---

## 3. 执行链（唯一）

```
1. POST /v1/tasks/{key}/run
     AgentGraph: task_lifecycle.start_run()
       - 校验任务可执行、无活动 Run（唯一性约束）
       - 生成 trace_id，写入 collab_tasks.correlation
       - Runtime POST /v1/runs (tenant, workspace, product_task_id, goal, budget, project, repo)
2. Runtime Worker 领取 Run，LoopController.step:
     a. 组装上下文（ContextBuilder + 任务/仓库/项目 section）
     b. model.decide → 工具决策
     c. KernelToolRuntime.execute(tool, args, idempotency_key)
          - pull_task_bundle      → 取得 DAG 任务 + lease_token + write_set
          - query_symbol_signature→ 需要时补上下文
          - submit_patch          → 202 submission_id → **deferred**
          - get_task_evidence     → 门禁失败证据 → 模型修复
          - request_task_split / escalate_contract_defect / inject_dependency_task
          - get_task_status / cancel_task
3. 若工具返回 deferred:
     LoopController 持久化 external_invocation，Run → WAITING_EXTERNAL，释放租约。
4. 唤醒:
     kernel_events → AgentGraph event_inbox → runtime_wakeups(kind=SUBMISSION_FINAL, submission_id)
     或 Kernel→Runtime 直连 outbox（同一主机）→ Runtime POST /v1/runs/{id}/resume
     兜底：Runtime reconciliation 扫描超期 deferred（不是唯一机制）
5. 恢复:
     LoopController 读 checkpoint + external result → 继续模型循环
     门禁 FAIL → 把证据喂回模型 → 修复 → 再次 submit_patch（幂等键不变）
6. 完成:
     Run COMPLETED → task_lifecycle.project_run() → 记录 gate results → claim/handoff → REVIEWING
     人类审批 → DONE（唯一人类终点）
```

**禁止**：Runtime Worker 在等待门禁期间占用 run lease；AgentGraph 用 GET 轮询作为唯一同步；
Kernel Runner CLI 与 Runtime 同时认领同一产品任务。

---

## 4. KernelToolRuntime（Phase 2 交付物）

位置：`agent_runtime/tools/kernel.py`，实现 `agent_runtime.loop.ToolRuntime`。

```python
class KernelToolRuntime:
    CAPABILITIES: dict[str, Capability]  # 显式注册表
    async def execute(name, arguments, idempotency_key) -> ToolResult
```

| Capability | Kernel 端点 | 备注 |
|---|---|---|
| `pull_task_bundle` | `POST /v1/syscalls/pull_task_bundle` | 返回 lease_token，Runtime 必须加密/不出日志 |
| `submit_patch` | `POST /v2/syscalls/submit_patch` | 202 → `ToolResult(deferred=...)` |
| `get_submission` | `GET /v2/submissions/{id}` | 恢复时读取最终状态 |
| `query_symbol_signature` | `POST /v1/syscalls/query_symbol_signature` | |
| `get_task_evidence` | `GET /v1/runner/tasks/{id}/evidence` | 新增 runner-plane 端点 |
| `get_task_status` | `GET /v1/runner/tasks/{id}` | 已有 |
| `cancel_task` | `POST /v1/runtime/tasks/{id}/cancel` | 新增：runtime 服务身份 |
| `request_task_split` | `POST /v1/syscalls/request_task_split` | |
| `escalate_contract_defect` | `POST /v1/syscalls/escalate_contract_defect` | |
| `inject_dependency_task` | `POST /v1/syscalls/inject_dependency_task` | |

### 认证边界

- Runtime 使用**专用服务凭据** `VN_RUNTIME_SERVICE_KEY`（不是 admin key，不进模型、不进浏览器）。
- Kernel 新增 `runtime` 认证依赖：`Bearer vnk_...`（工作区绑定的 runner token）用于 syscall plane，
  以及 `X-Veynod-Service-Key` 用于服务面（evidence/cancel）。
- 每个请求携带 `X-Workspace-Id`（协议升级，缺省回落单 workspace 行为，向后兼容）。
- 所有调用写 `runtime_tool_audit`（Runtime schema）：run_id, tool, idempotency_key,
  status, latency_ms, kernel_status, error_class, trace_id。

### 错误分类（映射到 `ToolResult.error_class`）

`AUTH`（401/403）→ 永久失败；`VALIDATION`（422）→ 返回给模型修复；`CONFLICT`（409）→ 幂等重放或失败；
`ADMISSION`（429 + Retry-After）→ 可重试，带退避；`TRANSPORT`（5xx/超时）→ 有限重试后失败；
`NOT_FOUND`（404）→ 失败。

---

## 5. 状态机

### 5.1 Product Task（不新增状态，只收紧写入权）

`TODO → READY → IN_PROGRESS → REVIEWING → DONE`，分支 `FAILED/CANCELLED/HANDOFF/AWAITING_HUMAN/WAITING_HUMAN/ESCALATED_TO_HUMAN`。
新增运行期映射（`task_lifecycle.project_run`）：

| Run 状态 | 产品动作 |
|---|---|
| CREATED/QUEUED/INITIALIZING/PLANNING/READY/RUNNING/WAITING_* | READY→IN_PROGRESS（首次） |
| COMPLETED | 记录门禁证据 → REVIEWING（claim+handoff，系统身份） |
| FAILED | FAILED（携带错误摘要） |
| CANCELLED | CANCELLED |

单调性：终态不可回退；REVIEWING/DONE 不被迟到的 Run/Kernel 事件改写。

### 5.2 Agent Run（新增 `WAITING_EXTERNAL`）

```
RUNNING → WAITING_EXTERNAL → RUNNING | FAILED | CANCELLED | TIMED_OUT
```
`external_invocations` 表记录 `submission_id / kernel_task_id / deadline`，唯一键 `(run_id, idempotency_key)`。

### 5.3 Kernel DAG Task

保持 Kernel 现有 FSM 不变（`BLOCKED/READY/LEASED/VERIFYING/COMMITTED/FAILED/CANCELLED/...`）。

---

## 6. 事件协议

统一信封（`contracts/events.py` 扩展）：

```json
{
  "source": "runtime|kernel|product",
  "type": "run.completed|submission.committed|gate.failed|...",
  "seq": 123,
  "occurred_at": "...",
  "correlation": {"tenant_id","workspace_id","project_id","product_task_id","run_id","kernel_workspace_id","kernel_task_id","submission_id","trace_id"},
  "payload": {}
}
```

- Kernel → AgentGraph：`event_inbox`（已有，`source=kernel`）。
- Runtime → AgentGraph：`event_inbox`（已有，`source=runtime`）。
- Kernel → Runtime：新增 `agent_runtime.runtime_wakeups`，由 Runtime 的 kernel-event consumer 写入
  （读 `GET /v1/dag/events` + 游标，或在同机部署下读 `kernel_events`）。幂等键 `(kind, ref_id, kernel_event_seq)`。
- 投影幂等：`event_inbox (source, source_instance, source_seq)` 唯一；投影副作用以
  `(task_key, action, dedupe_key)` 去重。

---

## 7. 多租户 / 多项目 / 多仓库

```
Tenant → Workspace → Project → Repository → Kernel Workspace(binding)
```

- 新增表 `kernel_bindings`（AgentGraph schema）：
  `(workspace_id, project, repo_path) → kernel_workspace_id, kernel_base_url, credential_ref, status`。
- 新增表 `projects`（已存在）+ `project_repositories`（新增）：一个项目可绑定多个仓库。
- Runtime 在创建 Run 时把 `project_id/repo_path` 写进 run；`KernelToolRuntime` 按 run 的
  `repo_path + project` 解析 binding（含缓存），请求带 `X-Workspace-Id`。
- 同一 Kernel 实例承载多个 workspace（syscall 平面按 header 路由）；如需分片，
  `kernel_bindings` 的 `kernel_base_url` 指向不同实例，对产品透明。
- 隔离：Kernel token 绑定 workspace，跨 workspace 请求在 `get_authenticated_worker_id` 直接 403。

---

## 8. 产品 UI 信息架构

唯一入口：`/dashboard/`。

```
Project（项目）
  ├─ 目标 / 仓库 / 进度 / 任务列表
Task（任务）
  ├─ 目标、状态、计划、子任务（Kernel DAG）
  ├─ Loop：Agent 正在做什么、当前工具、等待什么、是否需人工
  ├─ Code：变更文件、patch、commit
  ├─ Verification：门禁阶段与证据
  ├─ Delivery：交付记录
  └─ Approval：审批与最终交付
Agent（Agent）
  └─ 身份、能力、分工、执行记录
```

- 旧 `/dashboard/code/`、`/dashboard/agent-runs/`、`/dashboard/projects/` 从主导航移除；
  作为**受管理员权限保护的内部运维视图**迁到 `/dashboard/internal/{code,agent-runs,delivery}`，
  默认不出现在导航，非管理员 404/403。作为 Task Detail 的数据来源保留后端 API。
- 后端路由同步调整：`/v1/code`、`/v1/tasks/{key}/run` 仍保留（Task Detail 使用），
  但 `code_engine` 不再自动创建 Kernel 任务。

---

## 9. 部署边界

一套 compose（仓库根 `docker-compose.yml`）：

| 服务 | 进程 | 依赖 | 健康 |
|---|---|---|---|
| `db` | postgres+pgvector | - | `pg_isready` |
| `api` | AgentGraph | db, kernel, runtime | `/healthz`（自身）+ `/healthz/deep`（下游） |
| `runtime-api` | `agent_runtime.api` | db | `/healthz` |
| `runtime-worker` | `agent_runtime.worker_main` | db, runtime-api, kernel | `/metrics` |
| `kernel-api` | `veynod_kernel.app.main` | db | `/healthz` |
| `kernel-gate` | `veynod_kernel.app.worker` | db, kernel-api | 进程存活 + `/v1/admin/metrics` |
| `kernel-reaper`（可选） | runtime 内置 reaper | db | - |

统一：一个 `.env`、一个 `make up`/`docker compose up -d`、一个迁移入口
（`scripts/migrate_all.py`：AgentGraph schema → Runtime schema → Kernel schema，幂等）。
`/healthz/deep` 只有在 Runtime 与 Kernel 都可用、并且 DB 迁移到当前版本时才 200。
Worker 崩溃由 compose `restart: unless-stopped` + 进程内 supervisor 恢复。

---

## 10. 不在范围内

- 不重写 Rust SchedulerCore、写集锁、门禁算法、merge 算法。
- 不把所有表合并进一个 schema（保留 bounded context）。
- 不删除历史表/数据；只做 expand→migrate→contract。
