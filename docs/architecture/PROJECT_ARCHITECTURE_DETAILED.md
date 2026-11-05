# Veynod 全量项目架构与代码路径（Detailed Architecture）

> 版本快照：2026-11-01（本地 UTC-4）
> 根目录：`E:\Codes\veynod`
> 说明：本文是**新增文档**，没有修改根目录原有 `PROJECT_ARCHITECTURE.md`、`README.md` 或重构计划原文。所有“当前状态”以本机实际代码、迁移和运行进程为准；代码尚未提交 git，因此没有 commit hash 可引用。

---

## 阅读导航

- §1 仓库路径与规模
- §2 服务边界、端口与进程
- §3 数据所有权与核心表
- §4 端到端主流程（代码任务 / Agent Run / 状态机）
- §5 AgentGraph 详细架构（入口、认证、任务、Kernel/Runtime 适配、事件、项目、API、UI、CLI、迁移）
- §6 Agent Runtime 详细架构（状态机、Store、Loop、Gateway、API、进程）
- §7 Veynod Kernel 详细架构（Engine、Scheduler、Gate、Merge、Sandbox、Rust、迁移）
- §8 契约、事件与协议
- §9 关键配置
- §10 测试地图
- §11 启动与运行路径
- §12 当前限制与容量真话
- 附录 A 完整 HTTP 路由表
- 附录 B 核心类/方法签名索引
- 附录 C 迁移索引
- 附录 D 模块职责总表

---

## 0. 一句话架构

Veynod 现在是**三个运行边界 + 一个共享契约层**：

1. **AgentGraph**：产品控制面。身份、Workspace、任务、文档、审计、项目、用量、UI。
2. **Agent Runtime**：Durable Agent Run。模型/工具循环、步骤、Checkpoint、委派、租约、配额。
3. **Veynod Kernel**：代码执行引擎。Git 仓库、DAG 任务、patch submission、Gate、Merge、Delivery、Cell。
4. **contracts**：跨服务 ID、事件 envelope、错误码、幂等键的独立 Python 包（目前未强制接入三服务请求路径）。

三者不共享数据库表，也不做数据库合并；通过 HTTP + 事件游标连接。

```text
Browser / CLI / MCP Agent
          |
          v
+---------------------------+        +----------------------------+
| AgentGraph :8000          | -----> | Agent Runtime :8002        |
| FastAPI + Next static     |        | Run/Step/Checkpoint        |
| Task/Doc/Audit/Project    |        | Loop/Model/Tool/Delegation |
+-------------+-------------+        +-------------+--------------+
              |                                      |
              | HTTP admin/task API                  | OpenAI-compatible
              v                                      v
+---------------------------+                DeepSeek / tool :8003
| Veynod Kernel :8001       |
| DAG / Git / Gate / Merge  |
| Submission / Cell / Event |
+------+-------------+------+
       |             |
       v             v
 Gate Worker :8030   PostgreSQL
 pyright/ruff/pytest  agentgraph / veynod_dev
 (local or Docker)
```

---

## 1. 仓库路径与规模

| 路径 | 角色 | 规模（Python LOC，排除缓存） |
|---|---|---:|
| `AgentGraph/app/` | 产品控制面 FastAPI、领域服务、MCP、模板 | ~72,677 |
| `AgentGraph/tests/` | AgentGraph 单元/集成测试 | ~24,240 |
| `agent_runtime/` | Durable Agent Runtime 包 | ~4,088 |
| `veynod_kernel/app/` | Kernel FastAPI、Engine、Gate/Merge 服务 | ~14,728 |
| `veynod_kernel/tests/` | Kernel 单元/集成测试 | ~5,556 |
| `contracts/` | 跨服务契约（无第三方依赖） | ~560 |
| `scripts/` | Runtime supervisor、rollback drill、tool server 等 | ~452 |
| `docs/architecture/` | Phase 0–8 审计/设计/报告 | Markdown |
| `veynod_platform/` | 早期独立 gateway 原型，**不在当前主链路** | 历史代码 |

### 1.1 顶层目录

```text
E:\Codes\veynod\
  AgentGraph\                 # 产品控制面（Docker :8000）
    app\                      # FastAPI 应用
    dashboard\                # Next.js 静态控制台 + 组件
    cli\                      # npm CLI (veynod)
    sql\                      # init.sql + 73 个迁移文件（基线+增量）
    tests\
  agent_runtime\              # Durable Agent Runtime
    migrations\               # 001–007
    tests\
  veynod_kernel\              # 代码执行 Kernel
    app\
    rust\veynod_core\         # Rust scheduler/AST/sandbox core
    sql\                      # v4_001–v4_014
    scripts\                  # bench_swarm、migrate_db 等
    tests\
  contracts\                  # identifiers/events/errors/idempotency
  docs\architecture\          # Phase 0–8 设计与验收报告
  docs\protocols\             # API/Event 契约
  scripts\                    # runtime_supervisor、rollback_drill、runtime_tool_server
  PROJECT_ARCHITECTURE.md     # 原有总览（未修改）
  PROJECT_ARCHITECTURE_DETAILED.md  # 本文
```

---

## 2. 服务边界、端口与进程

| 组件 | 入口 | 端口 | 责任 |
|---|---|---:|---|
| AgentGraph API/UI | `AgentGraph/app/main.py` | `8000`（Docker 发布 `0.0.0.0:8000`） | 产品 API、MCP `/mcp`、`/dashboard` 静态导出、健康检查 |
| AgentGraph DB | Docker `agentgraph-db-1` | 容器内 `5432` | 产品数据 |
| Agent Runtime API | `agent_runtime/api.py` | `8002` | 内部 Run API（`X-Runtime-Key`） |
| Agent Runtime Worker | `agent_runtime/worker_main.py` | metrics `8031` | 常驻 Run 执行池 |
| Agent Runtime 进程守护 | `scripts/runtime_supervisor.py` | — | 进程崩溃重启、metrics 健康重启 |
| Kernel API | `veynod_kernel/app/main.py` | `8001` | DAG、任务、submission、admin、SSE |
| Kernel Gate Worker | `veynod_kernel/app/worker.py` | metrics `8030` | submission gate、merge coordinator |
| Kernel 工具/测试 DB | Docker `veynod-test-pg` | `55432` | 开发/测试 PostgreSQL（`veynod_dev`/`veynod_test`） |
| 本地 demo tool | `scripts/runtime_tool_server.py` | `8003` | Runtime 的 echo 工具 |
| 模型 | `https://api.deepseek.com/v1` | 443 | `deepseek-chat`（Runtime 生产验证） |

> **端口 8000 冲突（重要）**：本机另有一个本地 `uvicorn app.main:app` 的 Moliply 商店应用监听 `127.0.0.1:8000`；Docker 的 AgentGraph 发布在 `0.0.0.0:8000`。因此 `curl 127.0.0.1:8000` 会命中错误应用。访问 AgentGraph 使用宿主机 LAN IP（如 `http://172.25.96.1:8000`），或 `docker exec agentgraph-api-1`。

---

## 3. 数据所有权与核心表

### 3.1 AgentGraph（`agentgraph` schema）

**拥有**：身份、Workspace、产品任务、文档/黑board、审计、事件、项目、计量。

核心表：

| 表 | 作用 |
|---|---|
| `workspaces` | 工作区、API key hash、root/tenant 绑定 |
| `tenants` / `memberships` / `human_users` / `human_sessions` | 租户、成员、人类账户 |
| `agent_tokens` | Agent/Commander token、role、namespace、lineage |
| `collab_tasks` | 产品任务（TaskStatus） |
| `leases` | 任务/资源租约、fencing epoch |
| `workspace_events`（16 分区） | 产品事件 outbox/stream |
| `timeline_streams` | 人类/Agent 时间线 |
| `shared_documents` / `document_versions` / `document_chunks` / `document_index` | 文档与检索 |
| `context_slots` | 共享状态 slot |
| `diagrams` / `diagram_chunks` | 旧 DAG/规划层 |
| `projects` | Phase 7 项目分组 |
| `event_inbox` / `code_projection_cursors` / `runtime_projection_cursors` | Phase 3 事件投递 |
| `runtime_usage_metered` / `usage_ledger` / `tenant_runtime_limits` | Runtime 计量与租户限额 |
| `quality_gate_runs` / `audit_samples` | 质量门/审计抽样 |
| `blobs` / `share_links` / `webhook_*` / `backups` / `incidents` | 辅助产品能力 |

### 3.2 Agent Runtime（`agent_runtime` schema）

**拥有**：Agent Run 状态、步骤、Checkpoint、模型/工具调用、租约、委派、事件 outbox。

| 表 | 作用 |
|---|---|
| `agent_runs` | Run 主记录：tenant/workspace/product_task/workflow/goal/status/version/budget/usage |
| `agent_steps` | 每一步动作（PLAN/MODEL/TOOL/CHECKPOINT…） |
| `run_checkpoints` | 可恢复状态 |
| `invocations` | 模型/工具调用，含 request/result/error/usage |
| `run_events` | Run 事件流（全局 event_seq） |
| `runtime_outbox` | 未发布事件（当前 AgentGraph 通过 global events 拉取） |
| `runtime_idempotency` | 命令幂等结果 |
| `agent_runs.lease_*`（003） | Worker 租约/fencing 字段（无独立 run_leases 表） |

### 3.3 Kernel（`public` schema，`veynod_dev`）

**拥有**：Git 仓库、AST artifact、DAG、submission、gate、merge、delivery、cell。

| 表 | 作用 |
|---|---|
| `workspaces` | 一个 workspace = 一个 `repo_path` + `main_branch` |
| `code_artifacts` | 符号级 AST 契约节点 |
| `dag_tasks` | Kernel DAG 任务（TaskStatus） |
| `task_dependencies` | 上游/下游 DAG 边 |
| `symbol_edges` | 调用/引用关系（启发式） |
| `task_snapshots` | 任务创建时 artifact/base/graph 快照 |
| `symbol_graph_versions` | 符号图版本 |
| `patch_submissions` | v2 提交、gate 证据、work lease |
| `merge_intents` / `merge_coordinators` | 每仓库 merge 串行化与 fencing |
| `merge_conflicts` | 冲突/重验证记录 |
| `kernel_events` / `event_outbox` | 事件游标 + 通知 outbox |
| `scheduler_authorities` | Rust core 单权威租约 |
| `admission_permits` | Gate 队列 admissions |
| `edge_rebuild_jobs` | 异步符号边重建 |
| `cells` / `repository_placements` | Phase 4 Cell/仓库分区 |
| `delivery_records` | Phase 6 交付收据 |

### 3.4 跨库关联

没有跨数据库外键。主要关联键：

- AgentGraph `CollabTask.input_context.kernel = {workspace_id, task_id}` → Kernel `dag_tasks.task_id`
- AgentGraph `CollabTask.input_context.agent_runtime = {run_id, base_url}` → Runtime `agent_runs.run_id`
- Kernel `patch_submissions.task_id` → `dag_tasks.task_id`
- Runtime `agent_runs.product_task_id` → AgentGraph `collab_tasks.task_key`（逻辑关联，无 FK）

---

## 4. 端到端主流程

### 4.1 代码任务：AgentGraph → Kernel → REVIEWING

1. `POST /v1/code/runs` → `app/api/code_routes.py::create_code_run`
2. `app/services/code_engine.py::create_run`：
   - 调 Kernel `KernelEngineClient.create_artifact` / `create_task`；
   - 创建 AgentGraph `CollabTask`，`input_context.code_engine='veynod-kernel'`、`kernel={base_url, workspace_id, task_id}`。
3. Runner 调 Kernel `POST /v1/syscalls/pull_task_bundle`：
   - `PostgresKernelEngineService.lease_next_disjoint_task` → `DisjointWriteSetScheduler.claim_next_orthogonal_task`
   - Rust `SchedulerCore.claim_next` 或 SQL `_claim_with_sql`
   - 返回 `TaskBundleResponse`（base SHA、locks、frozen tests、stubs）。
4. Runner 调 `POST /v2/syscalls/submit_patch`：
   - `SubmissionService.accept`（幂等、lease 校验、`base_sha`/`snapshot_id`）
5. Gate Worker `GateWorker.claim`：
   - `DeterministicVerificationGate.verify_patch`（5 stages）
   - `_upsert_merge_intent`
6. Merge Coordinator `process_all` → `claim` → `process_intent`：
   - `merge_verified` → `_merge`（task lease fence + cell fence + Git CAS/treemerge）
   - `_finalize_merge`：artifact sync、edge rebuild、Kahn unblock、`delivery_records`、`GATE_PASSED`。
7. AgentGraph `event_dispatch.py::ingest_kernel_once` 拉 `/v1/dag/events` → `event_inbox` → `dispatch_once` → `code_engine.sync_kernel_task_event`。
8. `code_engine._sync_lifecycle`：
   - Kernel `COMMITTED` + real evidence → `quality.record_gate_result` → `claim_task` → `handoff_task(REVIEWING)`；
   - `FAILED/CANCELLED` → `fail_task` / `cancel_task`。

### 4.2 Agent Run：AgentGraph → Runtime → REVIEWING

1. `POST /v1/tasks/{task_key}/agent-run`
2. `app/services/agent_runtime.py::create_task_run`：
   - 检查 Runtime `/v1/quota/{tenant}` + 本地 `tenant_runtime_limits`；
   - `AgentRuntimeClient.create_run` → `start_run`；
   - 写 `collab_tasks.input_context.agent_runtime = {run_id, base_url}`。
3. Runtime Worker `RuntimeWorker.run_once`：
   - `RunStore.claim_run` 租约；
   - `LoopController.step`：
     - `ContextBuilder.build`
     - `HTTPModelGateway.decide`（DeepSeek）
     - `HTTPToolRuntime.execute`（allowlist，Idempotency-Key）
     - `RunStore.record_step` / `checkpoint` / `add_usage`
4. Run 终态 → `run_events`/`runtime_outbox`。
5. AgentGraph `event_dispatch.ingest_runtime_once` → `sync_runtime_run_event` → `get_task_run`：
   - COMPLETED + READY → claim → REVIEWING；
   - FAILED/CANCELLED → fail/cancel；
   - `meter_runtime_run` 幂等写 `runtime_usage_metered` + `usage_ledger`。

### 4.3 状态机

**AgentGraph TaskStatus**（`AgentGraph/app/models.py::TaskStatus`）：

`TODO → READY → IN_PROGRESS → REVIEWING / HANDOFF / AWAITING_HUMAN / WAITING_HUMAN / ESCALATED_TO_HUMAN → DONE | FAILED | CANCELLED`

**Kernel TaskStatus**（`veynod_kernel/app/models.py::TaskStatus`）：

`BLOCKED → READY → LEASED → VERIFYING → COMMITTED | FAILED | CANCELLED`，另有 `SUSPENDED/SUSPENDED_ESCALATED/SPLIT_ARCHIVED`。

**Runtime RunState**（`agent_runtime/states.py::RunState`）：

`CREATED → QUEUED → INITIALIZING → PLANNING → READY → RUNNING → WAITING_MODEL/WAITING_TOOL/WAITING_CHILDREN/BLOCKED/CHECKPOINTING/REPLANNING/PAUSED/VERIFYING → COMPLETED/FAILED/CANCELLED/TIMED_OUT/BUDGET_EXCEEDED`。

---

## 5. AgentGraph 详细架构

### 5.1 应用入口与后台循环

文件：`AgentGraph/app/main.py`

- `lifespan(app)`：
  - 生产 `ADMIN_TOKEN` 强度校验；
  - `apply_schema()` + onboarding backfill；
  - `record_release()`、`directory.backfill_auto()`；
  - 启动 `event_bus` 与后台任务。
- 后台 loop：
  - `lease_reaper_loop`、`webhook_delivery_loop`、`external_action_loop`、`index_worker_loop`
  - `access_log_loop`、`token_usage_loop`、`situation_loop`、`situation_evaluation_loop`
  - `mail_worker_loop`、`incident_check_loop`、`blob_gc_loop`、`presence_loop`、`scheduler_loop`
  - `code_projection_loop`（30s 扫描兜底）
  - `event_dispatch_loop`（Phase 3，默认 1s）
  - `agent_run_projection_loop`（10s 扫描兜底）
  - 可选 `backup_scheduler_loop`
- 路由挂载：`v1_router`、`code_router`、`agent_run_router`、`project_router`、`admin_router`、`event_admin_router`、`stream_router`、`inbox_stream_router`、`code_page_router`、`agent_run_page_router`、`project_page_router`。
- 静态 UI：`dashboard/out` 挂载 `/dashboard`；`/docs` 挂载文档站；`/mcp` 挂载 MCP。

### 5.2 身份与权限

文件：`AgentGraph/app/auth.py`

| 类/函数 | 作用 |
|---|---|
| `RoleScope` | SUPER_ADMIN/COMMANDER/SCOUT/ANALYST/AUDITOR/EXECUTOR |
| `AuthKind` | WORKSPACE/ROOT/AGENT/HUMAN |
| `Principal` | workspace、role、tenant、lineage、token depth、allowed_namespaces；`is_commander/is_super_admin/is_human` |
| `resolve_principal(session, raw_key, agent_hint)` | 依次解析 workspace key、root key、agent token、human session |
| `get_principal(request, session)` | FastAPI 依赖 |
| `get_human_writer` / `get_human_account` | 人类写/账户依赖 |
| `require_permission(principal, permission)` | 权限检查 |
| `ensure_namespace` / `ensure_task_role` / `ensure_not_human_locked` | 命名空间/角色/人类锁 |
| `principal_for_admin(workspace)` | 内部投影使用的 commander principal |

### 5.3 任务服务

文件：`AgentGraph/app/services/tasks.py`

核心方法：

- 读取/校验：`_get_task`、`_check_version`、`_check_lease`、`_require_active_lease`、`_check_epoch`、`_check_assignee`、`_require_claim_owner`、`validate_review_next_status`
- 创建：`_create_task_core`、`create_task`、`list_tasks`、`claimable_conditions`
- 租约：`claim_task`、`renew_task`、`renew_lease_explicit`、`release_task`、`_release_dependents`
- 交付/评审：`handoff_task`、`review_task`、`_answer_by_review`、`_require_approve_done`
- 取消/失败：`cancel_task`、`cancel_own_task`、`fail_task`
- 运维：`reap_expired_task_leases`、`reap_task_timeouts`、`reassign_task`、`list_orphan_tasks`、`claim_orphan_tasks`
- 委派/树：`create_subtasks`、`get_task_tree`、`write_continuation`、`_append_erratum`
- 人机升级：`escalate_task`、`create_escalation`、`list_escalations`、`resolve_escalation`

租约/资源细节在 `AgentGraph/app/services/leases.py`：`grant_task_lease`、`active_task_lease`、`check_fencing`、`require_holder`、`heartbeat`、`release`、`orphan_task`、`flag_missed_heartbeats`、`revoke_holder`、`transfer_responsibilities`。

### 5.4 Kernel 代码运行适配

文件：`AgentGraph/app/services/code_engine.py`

| 类/方法 | 作用 |
|---|---|
| `KernelEngineClient` | Kernel admin HTTP 客户端 |
| `_request` | `X-Admin-Key`，错误转 `CodeEngineError` |
| `create_artifact` / `create_task` / `list_tasks` / `task` | 创建/读取 Kernel 对象 |
| `evidence` / `delivery` | Gate 证据 / Delivery Record |
| `cancel_task` | 取消 Kernel 任务 |
| `latest_event_seq` / `events` | 事件游标 |
| `gate_updates_from_evidence` | 只从真实 evidence 生成 CI/TESTS，不合成 PASS |
| `create_run` | 创建 Kernel artifacts/task + AgentGraph task |
| `cancel_kernel_run` | 产品取消 → Kernel 取消 |
| `list_runs` / `get_run` / `audit_run` | 产品读取 |
| `lifecycle_action` | Kernel 状态 → 产品动作 |
| `_sync_lifecycle` | claim/handoff/fail/cancel + quality gate |
| `project_code_runs_once` / `_project_code_runs_once` | 30s 扫描兜底 |
| `event_task_ids` / `_event_cursor` / `_kernel_workspace_ids` | Kernel 事件消费 |
| `sync_kernel_task_event` | 单个 Kernel 事件 → 产品任务 |
| `project_code_events_once` / `code_projection_event_loop` | 旧直接事件投影（event_dispatch 关闭时使用） |
| `code_projection_loop` | 扫描兜底 |

### 5.5 Agent Runtime 适配

文件：`AgentGraph/app/services/agent_runtime.py`

| 类/方法 | 作用 |
|---|---|
| `AgentRuntimeClient` | Runtime 内部 HTTP 客户端（`X-Runtime-Key`） |
| `create_run` / `start_run` / `get_run` / `cancel_run` | Run 生命周期 |
| `events` / `latest_event_seq` | 全局 Run 事件 |
| `tenant_quota` / `tenant_usage` / `provider_health` | 配额/用量/健康 |
| `create_task_run` | 配额预检 → create/start → 写 task context |
| `runtime_status_action` | 终态 → review/fail/cancel |
| `get_task_run` | 读取/投影/计量 |
| `meter_runtime_run` | 幂等写 `runtime_usage_metered` + `usage_ledger` |
| `cancel_task_run` | 产品取消 → Runtime cancel + task cancel |
| `sync_runtime_run_event` | 单个 Runtime 事件 → 产品任务 |
| `project_agent_runs_once` / `agent_run_projection_loop` | 10s 扫描兜底 |

### 5.6 事件 Inbox / Dispatcher

文件：`AgentGraph/app/services/event_dispatch.py`

- `InboxRow`：inbox 行 DTO。
- `enqueue_kernel_events` / `enqueue_runtime_events`：幂等入 `event_inbox`。
- `ingest_kernel_once`：按 Kernel workspace 拉事件并推进 `code_projection_cursors`。
- `_runtime_base_urls` / `_runtime_cursor` / `ingest_runtime_once`：按 Runtime base_url 拉全局事件。
- `_due_rows`：`PENDING`/超时 `PROCESSING` 领取。
- `_process_row`：Kernel task → `sync_kernel_task_event`；Runtime run → `sync_runtime_run_event`。
- `_mark_processed` / `_mark_failed`：成功/指数退避/5 次 DEAD。
- `dispatch_once`、`inbox_stats`、`list_inbox`、`replay`。
- `event_dispatch_loop`：1s 循环。

管理路由：`AgentGraph/app/api/event_admin_routes.py`（`/v1/admin/event-inbox`）。

### 5.7 Projects / Usage / Delivery 产品面

文件：`AgentGraph/app/services/projects.py`

- `list_projects` / `create_project`
- `execution_graph`（`_task_node`）
- `deliveries`（逐个 code task 调 Kernel delivery）
- `runtime_observability`
- `usage_summary`
- `get_tenant_limit` / `set_tenant_limit`

路由：`AgentGraph/app/api/project_routes.py`（`/v1/projects`、`/v1/execution-graph`、`/v1/deliveries`、`/v1/runtime/observability`、`/v1/usage/runtime`、`/v1/tenant/runtime-limit`）。

### 5.8 辅助服务模块（路径/职责）

| 文件 | 主要类/入口 | 职责 |
|---|---|---|
| `app/services/collab.py` | task/doc/log 函数 | 协作、升级、子任务取消 |
| `app/services/leases.py` | 见上 | 租约/fencing/孤儿 |
| `app/services/quality.py` | `record_gate_result`、`audit_view`、`before_review`、`after_review`、`check_auto_release`、`report` | 质量门、审计抽样、自动放行 |
| `app/services/diagrams.py` | `dispatch_chunk`、`submit_chunk`、`merge_chunk` 等 | 旧 DAG/chunk 规划 |
| `app/services/reaper.py` | `lease_reaper_loop` | 过期租约/超时 |
| `app/services/scheduler.py` | `scheduler_loop` | Runner placement/调度 |
| `app/services/events.py` | `EventBus`、`poll_events` | 产品事件 |
| `app/services/usage.py` | `record_usage`、`month_usage` | 用量账本 |
| `app/services/budget.py` / `token_budget.py` | `meter_embedding`、`flush_loop` | 预算/embedding |
| `app/services/backups.py` | `run_backup`、`request_restore`、`run_drill` | 备份/恢复/演练 |
| `app/services/agent_runtime.py` | 见 5.5 | Runtime 适配 |
| `app/services/event_dispatch.py` | 见 5.6 | 事件 inbox |
| `app/services/projects.py` | 见 5.7 | 项目/交付/计量 |

### 5.9 API 路由分组

完整路由见 **附录 A**。主要分组：

| 前缀 | 文件 | 用途 |
|---|---|---|
| `/v1` | `v1_routes.py` | Agent/任务/文档/slot/事件/资源/质量/预算/导出 |
| `/v1/admin` | `admin_routes.py` | Workspace/task/document/token/backup/incident/growth |
| `/v1/admin/event-inbox` | `event_admin_routes.py` | Phase 3 inbox 运维 |
| `/v1/human` | `human_routes.py` | 人类控制台 |
| `/v1/auth` | `auth_routes.py` | 登录/OAuth/CLI session |
| `/v1/tasks/{key}/agent-run` | `agent_run_routes.py` | Agent Run |
| `/v1/code/runs` | `code_routes.py` | Kernel code run |
| `/v1/projects`、`/v1/execution-graph`、`/v1/deliveries` 等 | `project_routes.py` | Phase 7 |
| `/v1/stream` | `stream_routes.py` | SSE（Product events） |
| `/v1/inbox/stream` | `inbox_stream.py` | Agent inbox SSE |

### 5.10 UI 与 CLI

- Next 静态控制台：`AgentGraph/dashboard/app/page.tsx`（Tab：Diagram/Loop/Tasks/Documents/Agents/Governance/Blackboard/Situation/Audit），组件在 `dashboard/app/components/*.tsx`。
- 静态模板页：
  - `/dashboard/code/` → `app/templates/code.html`
  - `/dashboard/agent-runs/` → `app/templates/agent_runs.html`
  - `/dashboard/projects/` → `app/templates/projects.html`
- Python 运维 CLI：`python -m app.cli`，命令 `init-db`、`create-workspace`、`list-workspaces`、`rotate-key`、`create-agent-token`、`stats`、`projects`、`execution`、`usage`、`deliveries`。
- npm CLI：`AgentGraph/cli/bin/veynod.mjs`（up/hub/doctor/init/listen/runner/service），Runner 在 `cli/bin/runner.mjs`。

### 5.11 迁移

- 基线：`sql/init.sql`、`v2_to_v3_collab.sql`、`v3_2_production.sql` 等。
- 增量：`sql/migrations/v3_5…v3_71`，在 `app/database.py::_INCREMENTAL_MIGRATIONS` 中显式登记。
- 当前 head：**3.71.0**（`v3_71_projects_metering.sql`）。
- 新增 Phase 3/7：
  - `v3_70_event_inbox.sql`：`event_inbox`、`runtime_projection_cursors`
  - `v3_71_projects_metering.sql`：`projects`、`runtime_usage_metered`、`tenant_runtime_limits`

---

## 6. Agent Runtime 详细架构

### 6.1 模块

| 文件 | 类/函数 | 责任 |
|---|---|---|
| `agent_runtime/states.py` | `RunState`、`validate_transition` | 21 状态 + `ALLOWED` + `TERMINAL` |
| `agent_runtime/store.py` | `RunStore` | PostgreSQL 持久化/事件/租约/幂等 |
| `agent_runtime/loop.py` | `LoopController`、`ModelAction`、`ToolResult`、`ModelGateway`、`ToolRuntime`、`budget_exceeded` | Agent 单步循环 |
| `agent_runtime/api.py` | `create_app`、`_env_app` | 内部 FastAPI |
| `agent_runtime/gateway.py` | `HTTPModelGateway`、`HTTPToolRuntime`、`ModelGatewayConfig`、`ToolGatewayConfig`、`_parse_action` | OpenAI 兼容模型 / HTTP 工具 |
| `agent_runtime/context.py` | `ContextBuilder`、`ContextSection`、`estimate_tokens` | system/product/checkpoint/steps/extra + token budget |
| `agent_runtime/delegation.py` | `DelegationManager` | 父子 Run、collect、reconcile |
| `agent_runtime/quota.py` | `QuotaPolicy` | 租户 model/token/cost 上限 |
| `agent_runtime/router.py` | `RoutingModelGateway`、`ModelRoute` | provider fallback |
| `agent_runtime/pool.py` | `RuntimeWorkerPool` | 进程内 N worker 监督 |
| `agent_runtime/worker.py` | `RuntimeWorker` | claim/lease/heartbeat/run_once |
| `agent_runtime/maintenance.py` | `ReconciliationLoop` | 委派对账 |
| `agent_runtime/worker_main.py` | `main`、`_metrics_server` | 常驻进程入口 |
| `scripts/runtime_supervisor.py` | `main`、`_metrics_ok`、`_port_open` | OS 进程级重启 |

### 6.2 `RunStore` 方法分组

- 初始化：`__init__`、`close`、`init_schema`（versioned migrations）
- 事件/幂等：`_publish`、`_idempotent`、`_remember`、`list_events`、`latest_event_seq`、`list_events_global`
- Run：`create_run`、`get_run`、`list_runs_by_status`
- 状态：`start_run`、`transition`（row lock + expected_version + event/outbox）
- Checkpoint/step：`checkpoint`、`next_step_no`、`record_step`、`list_checkpoints`、`list_steps`
- 调用：`begin_invocation`、`finish_invocation`、`add_usage`、`tenant_usage`、`invocation_health`
- 委派：`list_children`、`get_child_by_delegation`
- 租约：`claim_run`、`assert_lease`、`renew_lease`、`release_lease`
- 统计：`status_counts`

### 6.3 `LoopController.step`

1. `_check_guard`（外部取消/暂停）
2. 读 run + checkpoint + steps
3. `QuotaPolicy.exceeded` + `budget_exceeded`
4. `ContextBuilder.build`
5. `ModelGateway.decide`
6. 若 tool：`begin_invocation` → `ToolRuntime.execute`（幂等）→ `finish_invocation`
7. `RunStore.record_step` / `checkpoint` / `add_usage`
8. finish → `transition(COMPLETED)`；否则进入下一状态

### 6.4 Runtime API

| Method | Path | 作用 |
|---|---|---|
| GET | `/healthz` | 健康 |
| POST | `/v1/runs` | 创建 Run（201） |
| GET | `/v1/runs/{run_id}` | 读取 |
| POST | `/v1/runs/{run_id}/transition` | 受控状态迁移 |
| POST | `/v1/runs/{run_id}/start` | 启动 |
| POST | `/v1/runs/{run_id}/pause` | 暂停 |
| POST | `/v1/runs/{run_id}/resume` | 恢复 |
| POST | `/v1/runs/{run_id}/cancel` | 取消 |
| POST | `/v1/runs/{run_id}/checkpoint` | 写 checkpoint |
| POST | `/v1/runs/{run_id}/delegate` | 创建子 Run |
| GET | `/v1/runs/{run_id}/children` | 子 Run |
| POST | `/v1/runs/{run_id}/collect` | 收集子 Run |
| POST | `/v1/delegations/reconcile` | 对账 |
| GET | `/v1/runs/{run_id}/checkpoints` | checkpoint 列表 |
| GET | `/v1/runs/{run_id}/events` | Run 事件 |
| GET | `/v1/events/head` | 全局事件头 |
| GET | `/v1/events` | 全局事件 |
| GET | `/v1/stats` | 状态统计 |
| GET | `/v1/usage/{tenant_id}` | 租户用量 |
| GET | `/v1/quota/{tenant_id}` | 配额/是否超额 |
| GET | `/v1/provider-health` | 模型/工具失败率 |

认证：`X-Runtime-Key`（`create_app(..., api_key)`，`require_key`）。

### 6.5 Runtime 迁移

`001_init.sql`（runs/steps/checkpoints/events/outbox/idempotency）→ `002_invocations_usage.sql` → `003_run_leases.sql` → `004_delegation.sql` → `005_delegation_key.sql` → `006_goal.sql` → `007_tenant_text.sql`。

### 6.6 Runtime 进程

- API：`python -m uvicorn agent_runtime.api:app --host 0.0.0.0 --port 8002`
- Worker：`python -m agent_runtime.worker_main`
- Supervisor：`python scripts/runtime_supervisor.py`
- Metrics：`GET http://127.0.0.1:8031/metrics`

---

## 7. Veynod Kernel 详细架构

### 7.1 入口与后台循环

文件：`veynod_kernel/app/main.py`、`veynod_kernel/app/runtime.py`、`veynod_kernel/app/worker.py`

API lifespan：

- `validate_api_production_settings`
- `init_db()`（genesis + v4_001–v4_014，advisory lock）
- `_bootstrap_token`、`_reconcile_workspaces`
- `get_event_hub().start()`
- `edge_rebuild_service.start()`（可选）
- `outbox_dispatcher.start()`
- 后台：`_reaper_loop`、`_scheduler_sync_loop`、`scheduler_core_manager.warmup()`

Gate Worker：

- `MergeCoordinator.run_forever()`
- N × `GateWorker.run_forever()`
- `worktree_pool_manager.warmup()`
- `edge_rebuild_service.start()`
- `outbox_dispatcher.start()`
- metrics `:8030`

### 7.2 API 路由

完整见附录 A；主要文件：

- `app/api/admin_routes.py`：workspaces/tokens/artifacts/tasks/evidence/cancel/scan-stubs/edges/outbox
- `app/api/cell_routes.py`：cells/repositories/placement
- `app/api/code_intel_routes.py`：snapshot/delivery/merge-conflicts
- `app/api/dag_routes.py`：`/v1/dag`、`/v1/dag/plan`、`/v1/dag/events`、`/healthz`
- `app/api/runner_routes.py`：`/v1/events/stream`、runner heartbeat/release/task/ping
- `app/mcp_server/syscalls.py`：6 syscalls（`/mcp`）+ v2 submit endpoints

### 7.3 Engine 类/方法

#### `PostgresKernelEngineService`（`app/engine/kernel_service_impl.py`）

- `from_session_dep`
- `_default_workspace_id`（Phase 4 cell 内选择）
- `_workspace`
- `verify_active_lease`
- `lease_next_disjoint_task`
- `_build_bundle` / `_view` / `_signature_from_head` / `_current_code`
- `verify_and_commit_patch`
- `execute_graph_split`
- `escalate_and_propagate_stale`
- `inject_prerequisite_node`
- `lookup_symbol_signatures`

#### `DisjointWriteSetScheduler`（`app/engine/scheduler.py`）

- `claim_next_orthogonal_task`
- `_reserve_admission`（Phase 8 快速拒绝）
- `_bind_permit_tx` / `_release_permit`
- `_claim_after_admission`
- `_claim_with_rust_core`
- `_admission_depth`
- `_update_selected_task`（合并后的冲突检查）
- `_claim_with_sql`
- `on_task_committed`（Kahn 解锁）
- `reap_expired_leases` / `reap_stuck_verifications`
- `extend_lease` / `release_task`

#### `SchedulerCoreManager`（`app/engine/scheduler_core.py`）

- `_ensure_core` / `available` / `reset`
- `stats` / `ready_count`
- `acquire_authority` / `_acquire_authority_locked`
- `ensure_loaded` / `reload_workspace`
- `sync_tasks`
- `claim` / `revert`
- `warmup`

#### `GraphMutator`（`app/engine/mutator.py`）

- `split_task_into_subdag`
- `escalate_and_propagate_blast_radius`
- `inject_prerequisite_node`

#### `LockAlgebra`（`app/engine/lock_algebra.py`）

`normalize`、`conflicts`、`is_covered_by_write_set`、`extract_file_level_whitelist`、`expand_write_set`、`lock_set`、`is_sub_write_set`、`promote_new_files`、`file_paths_for_locks`、`symbols_conflict`；`plan_claimable`。

#### `task_commands.py`

- `cancel_task` + `_cancel_side_effects`（递归 CANCELLED、失败 submission、拒绝 merge intent、发 `TASK_CANCELLED`）

### 7.4 Gate / Merge 服务

#### `DeterministicVerificationGate`（`app/services/verification_gate.py`）

| 方法 | 作用 |
|---|---|
| `verify_and_merge_patch` | 旧同步路径 |
| `verify_patch` | 只跑 gate，产出 `VerifiedPatch` |
| `merge_verified` | coordinator 调用，进入 Git/DB 临界区 |
| `_run_attempt` | 5 stages：WRITE_SET_BOUNDARY / AST_SCOPE_DIFF / CONTRACT_HASH_GUARD / STATIC_TYPECHECK / UNIT_TEST_SUITE |
| `_sensitive_metadata` / `_precompute_sensitive_snapshots` | 敏感文件快照 |
| `_check_ast_scope` / `_check_contracts` / `_static_checks` | gate 实现 |
| `_test_files` | 显式测试 + `symbol_edges` 受影响测试 |
| `_run_tests` | sandbox pytest/collect |
| `_merge` | task lease + cell fence + Git CAS/treemerge |
| `_tree_merge` | 快进 tree merge，失败回退 gate rerun |
| `_finalize_merge` | artifact sync、edge rebuild、Kahn、delivery record、GATE_PASSED |
| `_reject` | 失败证据 |

模块函数：`assert_task_mergeable`（`FOR UPDATE` + VERIFYING + lease hash）、`sensitive_file_unchanged`、`_workspace_merge_lock`。

#### `GateWorker`（`app/services/gate_worker.py`）

- `run_forever` / `claim` / `renew` / `fence` / `process`
- `_watch_cancellation`（COMMITTED 时停止，不取消）
- `_heartbeat_loop` / `_process_with_gate`
- `_upsert_merge_intent` / `_wait_merge_intent`
- `_finalize` / `_requeue` / `_handle_transient` / `release_task`

#### `MergeCoordinator`（`app/services/merge_coordinator.py`）

- `run_forever` / `acquire` / `renew` / `release`
- `_workspace_ids`（cell 过滤）
- `process_all` / `claim` / `process_intent`
- `_dependency_changed` / `_record_conflict` / `_revalidate`
- `_finalize_already_merged` / `_finish_intent`
- `reconcile_all` / `reconcile_workspace`

#### `gate_sandbox.py`

- `sanitized_environment`（env allowlist，去掉 DB/admin/cloud）
- `_posix_limits`（CPU/AS/NPROC/FSIZE）
- `_kill_tree`
- `_run_local`
- `_run_docker`（network none、cpus、memory、pids、nofile、cap-drop、no-new-privileges、read-only、tmpfs、可选 user）
- `run_sandboxed_command`

#### `git_ops.py`

- `GitRepository`：`head_sha`、`resolve_ref`、`validate_usable`、`list_files`、`read_file`、`read_files_batch`、`diff_at_head`、`changed_files_between`、`merge_tree`、`commit_tree`、`add_worktree`、`reset_worktree`、`apply_patch`、`stage_paths`、`commit`、`update_ref`
- `_GitBatchReader` / `_GitBatchPool`：批量 `cat-file`

### 7.5 事件 / Outbox / Admission / Cell

- `EventBus.publish`：`kernel_events` + `event_outbox` 同事务；envelope 字段。
- `OutboxDispatcher`：`SELECT ... FOR UPDATE SKIP LOCKED`、`pg_notify`、超大 payload 记录、dead-letter 列。
- `EventHub`：per-workspace ring + per-subscriber queue；`EventBufferOverflow`。
- `AdmissionDenied` / `admission_permits` / `admission_depth`。
- `cell_registry.py`：`list_cells`、`create_cell`、`list_repositories`、`move_repository`、`cell_workspace_ids`、`assert_workspace_in_current_cell`、`cell_filter_sql`。
- `submission_service.py`：`accept`、`find_existing`、`_insert`、`get_status`；幂等键 + `base_sha`/`snapshot_id`。
- `task_factory.insert_task`：插入 DAG + 依赖 + `task_snapshots`。
- `edge_rebuild.py`：异步符号边重建、graph version 递增。

### 7.6 Rust core

路径：`veynod_kernel/rust/veynod_core/src/`

| 文件 | 公开类型/函数 |
|---|---|
| `scheduler_core.rs` | `TaskRecord`、`WorkspaceState`、`CoreState`、`claim_next`、`revert_claim`、`reset_workspace`、`stats` |
| `locks.rs` | `normalize`、`expand_write_set`、`is_sub_write_set`、`is_covered`、`conflicts`、`promote_new_files`、`file_paths_for_locks`、`TaskSnapshot`、`Plan`、`plan_claimable` |
| `ast.rs` | `SignatureInfo`、`StubInfo`、`SymbolSnapshot`、`FileSnapshot`、`file_snapshot`、`signature`、`signature_hash`、`node_source`、`referenced_names`、`discover_stubs`、`file_symbols`、`file_functions`、`scan_repository` |
| `sandbox.rs` | `RunResult`、`run_command`（进程限制/超时/输出上限/进程树 kill） |
| `lib.rs` | PyO3 `SchedulerCore` 导出、`run_command`、AST/locks 绑定 |

Python 通过 `app/engine/rust_bridge.py` 调用；不可用时回退 Python/SQL（`VEYNOD_DISABLE_RUST`）。

### 7.7 Kernel 迁移

`v4_001_genesis.sql` → `v4_002_patch_submissions.sql` → `v4_003_gate_work_leases.sql` → `v4_004_merge_coordinator.sql` → `v4_005_admission_outbox_jobs.sql` → `v4_006_admission_permits.sql` → `v4_007_unlogged_admission_permits.sql` → `v4_008_active_lock_indexes.sql` → `v4_009_cancellation.sql` → `v4_010_token_workspace.sql` → `v4_011_event_delivery.sql` → `v4_012_cells_repositories.sql` → `v4_013_code_intelligence.sql` → `v4_014_delivery_records.sql`。

---

### 7.8 Phase 8 运行平面补充

- `app/runtime.py::install_api_plane_guard`：
  - `VN_API_PLANE=all` 为兼容默认；
  - `submit` 只放行 `/v2/syscalls/submit_patch`、`/v2/submissions/*`、`/healthz`、`/v1/admin/metrics`；
  - `claim_sse` 承担 claim/SSE/admin，拒绝 submit。
- `app/runtime.py::lifespan`：
  - submit 平面不启动 EventHub/edge/outbox/reaper；
  - 仅 `VN_ENABLE_BACKGROUND_LOOPS=true` 的首个 claim 实例运行维护循环；
  - 每个平面启动时 `warm_token_cache`。
- `app/engine/hold_queue.py::ClaimHoldQueue`：
  - `wait_for_opportunity(workspace_id, timeout)`：不持有 DB 连接的 FIFO 等待；
  - `EventHub` 在 `GATE_PASSED/TASK_READY/...` 时调用 `notify_available`；
  - `DisjointWriteSetScheduler.claim_next_orthogonal_task(..., wait_seconds)` 在等待前 `session.close()`。
- `app/auth.py::warm_token_cache`：批量预热 runner token；`TOKEN_REVOKED/TOKEN_ROTATED` 由 EventHub 清本地缓存。
- `scripts/bench_swarm.py`：
  - `--submit-instances` 物理平面；
  - `--client-shards` 多进程 runner/SSE 分片；
  - `claim/submit/sse` 三连接池；
  - `_stamp_wire_sent_at` httpx request hook。


## 8. 契约、事件与协议

### 8.1 `contracts/`

| 文件 | 内容 |
|---|---|
| `contracts/identifiers.py` | `tenant_id/workspace_id/project_id/repository_id/coding_session_id/product_task_id/agent_run_id/kernel_task_id/submission_id/verification_run_id/delivery_id` |
| `contracts/events.py` | `EventEnvelope`（event_id/version/aggregate/correlation/causation）、Kernel/Runtime 事件名 |
| `contracts/errors.py` | `ErrorCode`、错误分类 |
| `contracts/idempotency.py` | canonical key 生成 |
| `contracts/tests/test_contracts.py` | 5 tests |

> 现状：`contracts` 是独立包，三个服务尚未在请求路径直接 import；Kernel/Runtime 用各自字段，AgentGraph 用 inbox envelope。

### 8.2 事件层

- Kernel：`kernel_events(event_seq, workspace_id, event_type, payload, event_id, event_version, producer, correlation_id, causation_id)` + `event_outbox`。
- Runtime：`run_events(event_seq, run_id, event_type, payload)` + `runtime_outbox`。
- AgentGraph：`event_inbox(source, source_instance, source_seq, event_type, workspace_id, payload, status, attempts, next_attempt_at, last_error)`。
- 投递语义：at-least-once；AgentGraph 投影幂等；失败重试 5 次进入 DEAD，可 replay。
- 当前是拉取（1s），不是 Kernel `pg_notify` 的跨服务 LISTEN 消费。

---

## 9. 关键配置

### 9.1 AgentGraph

- DB：`DATABASE_URL`、`AUTO_INIT_DB`
- 安全：`ADMIN_TOKEN`、`JWT/SSO`、`MCP_ENABLED`
- Kernel：`KERNEL_URL`、`KERNEL_ADMIN_KEY`、`KERNEL_WORKSPACE_ID`
- Runtime：`AGENT_RUNTIME_URL`、`AGENT_RUNTIME_KEY`、`AGENT_RUNTIME_TIMEOUT`
- Phase 3：`EVENT_DISPATCH_ENABLED`、`EVENT_DISPATCH_INTERVAL_SECONDS`
- Phase 7：`RUNTIME_PROMPT_USD_PER_1K`、`RUNTIME_COMPLETION_USD_PER_1K`
- UI：`DASHBOARD` 静态导出、`SSE_*`

### 9.2 Agent Runtime

- `RUNTIME_DATABASE_URL`、`RUNTIME_API_KEY`
- `RUNTIME_MODEL_URL/API_KEY/NAME`、`RUNTIME_TOOL_URL/API_KEY/ALLOWLIST`
- `RUNTIME_WORKER_COUNT/LEASE_SECONDS/POLL_SECONDS/METRICS_PORT`
- `RUNTIME_CONTEXT_TOKEN_BUDGET`
- `RUNTIME_TENANT_MODEL_CALL_LIMIT/...`（QuotaPolicy）
- `RUNTIME_RECONCILE_INTERVAL_SECONDS`

### 9.3 Kernel

- DB：`DATABASE_URL`、`VN_AUTO_INIT_DB`
- 认证：`VN_AUTH_MODE`、`VN_ADMIN_API_KEY`、`VN_REQUIRE_BOUND_TOKENS`
- workspace：`VN_DEFAULT_WORKSPACE_ID`、`VN_CELL_ID`
- Gate：`VN_GATE_SANDBOX`、`VN_GATE_SANDBOX_IMAGE`、`VN_GATE_CPU_LIMIT/MEMORY_MB/PIDS_LIMIT`、`VN_GATE_DOCKER_USER/NETWORK/NOFILE_LIMIT`、`VN_GATE_WORKTREE_POOL_SIZE`
- Submission：`VN_WORK_LEASE_SECONDS`、`VN_WORK_HEARTBEAT_SECONDS`、`VN_WORK_MAX_ATTEMPTS`
- Merge：`VN_MERGE_COORDINATOR_LEASE_SECONDS`、`VN_VERIFY_LEASE_SECONDS`、`VN_FAST_FORWARD_MERGE`
- Admission：`VN_ADMISSION_GATE_QUEUE_LIMIT`、`VN_ADMISSION_MERGE_QUEUE_LIMIT`、`VN_ADMISSION_CLAIM_SLOTS`、`VN_ADMISSION_RETRY_AFTER_SECONDS`
- 调度：`VN_SINGLE_KERNEL_CLAIM`、`VN_MULTI_CORE_CLAIM`
- 事件：`VN_SSE_POLL_SECONDS`、`VN_OUTBOX_POLL_SECONDS`

---

## 10. 测试地图

| 套件/路径 | 覆盖 | 最近结果 |
|---|---|---:|
| `AgentGraph/tests` 非集成 | 身份/任务/文档/事件/预算/质量等 | **80 passed, 28 skipped** |
| `AgentGraph/tests/test_phase3_phase7.py` | inbox 重试/DEAD/replay、projects、limits、metering | **4 passed** |
| `agent_runtime/tests` | API/状态机/gateway/loop/lease/delegation/quota | **28 passed** |
| `veynod_kernel/tests` 非集成 | locks/event hub/gate/bench acceptance 等 | passed |
| `veynod_kernel/tests/test_integration.py` | 真实 PG + Git + gate + cancel + token + cell + delivery | **22 passed**（一个租约用例曾顺序 flake，重跑通过） |
| `veynod_kernel/scripts/bench_swarm.py` | 2/100/500/1,000 runner 容量 | 1,000 未通过，见 §12 |
| `scripts/rollback_drill.py` | v4_010 → v4_014 升级 → 恢复 v4_010 | PASS |

运行命令：

```bash
# AgentGraph
cd AgentGraph
.venv/Scripts/python.exe -m pytest tests -q -m "not integration"

# Runtime
cd E:/Codes/veynod
python -m pytest agent_runtime/tests -q

# Kernel
cd veynod_kernel
python -m pytest -q -m "not integration"
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test \
  VEYNOD_ALLOW_DESTRUCTIVE_TESTS=1 python -m pytest tests/test_integration.py -q

# rollback drill
python scripts/rollback_drill.py
```

---

## 11. 启动与运行路径

### 11.1 本机当前进程（快照）

| 进程 | 命令 | 端口 |
|---|---|---|
| AgentGraph | `docker compose up -d api` in `AgentGraph` | 8000 |
| Runtime API | `python -m uvicorn agent_runtime.api:app --host 0.0.0.0 --port 8002` | 8002 |
| Runtime Worker | `python scripts/runtime_supervisor.py`（内部 `python -m agent_runtime.worker_main`） | 8031 |
| Kernel API | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8001` | 8001 |
| Gate Worker | `python -m app.worker --workers 8 --database-url ...` | 8030 |
| Tool demo | `python scripts/runtime_tool_server.py` | 8003 |

### 11.2 关键环境

```bash
# AgentGraph .env
KERNEL_URL=http://host.docker.internal:8001
KERNEL_ADMIN_KEY=...
KERNEL_WORKSPACE_ID=0ae92b98-f687-461f-ae03-c5941dc31db8
AGENT_RUNTIME_URL=http://host.docker.internal:8002
AGENT_RUNTIME_KEY=dev-runtime

# Runtime worker
RUNTIME_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_dev
RUNTIME_MODEL_URL=https://api.deepseek.com/v1
RUNTIME_MODEL_NAME=deepseek-chat
RUNTIME_TOOL_URL=http://127.0.0.1:8003
RUNTIME_TOOL_ALLOWLIST=echo

# Kernel
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_dev
VN_AUTH_MODE=dev
VN_ADMIN_API_KEY=dev-admin
VN_DEFAULT_WORKSPACE_ID=...
```

---

## 12. 当前限制与容量真话

### 12.1 架构限制

1. Phase 4 仍是 **一个 workspace = 一个 Git 仓库**；Cell 是多个 workspace/仓库的分区，不是同一 workspace 多仓库。
2. Phase 5 没有强制“过期 snapshot 拒绝提交”；Snapshot 是元数据绑定，gate 仍按当前 HEAD 重验。
3. Phase 6 没有提供 gate Docker 镜像、磁盘配额、seccomp profile；Docker 模式需自备镜像并显式设置非 root user。
4. Phase 7 UI 是 FastAPI 静态模板，不是原生 Next 页面；没有完整 SDK/账单/企业权限。
5. AgentGraph/Runtime/Kernel 的事件仍是轮询 + at-least-once，不是跨服务 LISTEN/Exclusive consumer。
6. `contracts` 包未接入请求路径；版本化 envelope 尚未强制。
7. AgentGraph 是单进程控制面；Kernel 调度权威是 per-workspace；Runtime 是单机 Worker 池。
8. 没有 git commit；工作区改动未提交。

### 12.2 Phase 8 容量结论

最终 1,000 production 复测（`phase8-1000-multicore`）：

- **975/1000 COMMITTED**，25 LEASED 到 30 分钟截止；
- **server accept handler P99 886.8ms**；
- **client accept P99 84.4s**（未达标）；
- **SSE 1,000/1,000 打开，0 重连、0 错误**；
- DB pool timeout 0；
- claim P50/P99 250/4,281ms；
- 结论：**未通过生产验收**。服务端 handler 接近 1s，但 benchmark 单进程客户端、无服务端等待队列、gate CPU 吞吐共同导致端到端失败。

详细证据：`docs/architecture/PHASE8_CAPACITY_REPORT.md`、`docs/architecture/CAPACITY_BASELINE.md`。

---

# 附录 A：完整 HTTP 路由清单

> 由源码 `@router/@app` 装饰器自动提取；按文件分组，已拼接 APIRouter prefix。路径中的 `{...}` 是 FastAPI 参数。AgentGraph 约 293 条、Kernel 约 37 条，另有 Runtime 21 条与 MCP/syscall 补充表。


### AgentGraph/app/api

**`AgentGraph/app/api/admin_routes.py`** — prefix `/v1/admin`

| Method | Path |
|---|---|
| POST | `/v1/admin/workspaces` |
| GET | `/v1/admin/workspaces` |
| PATCH | `/v1/admin/workspaces/{workspace_id}` |
| POST | `/v1/admin/workspaces/{workspace_id}/rotate-key` |
| GET | `/v1/admin/workspaces/{workspace_id}/stats` |
| GET | `/v1/admin/workspaces/{workspace_id}/timeline` |
| GET | `/v1/admin/workspaces/{workspace_id}/tasks` |
| POST | `/v1/admin/workspaces/{workspace_id}/tasks` |
| GET | `/v1/admin/workspaces/{workspace_id}/tasks/{task_key}` |
| POST | `/v1/admin/workspaces/{workspace_id}/tasks/{task_key}/review` |
| POST | `/v1/admin/workspaces/{workspace_id}/tasks/{task_key}/escalate` |
| GET | `/v1/admin/workspaces/{workspace_id}/escalations` |
| POST | `/v1/admin/workspaces/{workspace_id}/escalations/{escalation_id}/resolve` |
| GET | `/v1/admin/workspaces/{workspace_id}/slots` |
| PUT | `/v1/admin/workspaces/{workspace_id}/slots/{slot_key:path}` |
| GET | `/v1/admin/workspaces/{workspace_id}/documents` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}/verify` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}/submit-review` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}/deprecate` |
| GET | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}/versions` |
| GET | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}/versions/{version}` |
| GET | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}` |
| GET | `/v1/admin/workspaces/{workspace_id}/document-parts` |
| PUT | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}` |
| GET | `/v1/admin/workspaces/{workspace_id}/agent-onboarding` |
| GET | `/v1/admin/workspaces/{workspace_id}/namespaces` |
| GET | `/v1/admin/workspaces/{workspace_id}/agent-tokens` |
| POST | `/v1/admin/workspaces/{workspace_id}/agent-tokens` |
| POST | `/v1/admin/workspaces/{workspace_id}/agent-tokens/{token_id}/revoke-delegated` |
| POST | `/v1/admin/workspaces/{workspace_id}/agent-tokens/{token_id}/rotate` |
| PATCH | `/v1/admin/workspaces/{workspace_id}/agent-tokens/{token_id}` |
| GET | `/v1/admin/workspaces/{workspace_id}/audit` |
| POST | `/v1/admin/workspaces/{workspace_id}/batches/{batch_id}/undo` |
| GET | `/v1/admin/workspaces/{workspace_id}/policies` |
| POST | `/v1/admin/workspaces/{workspace_id}/policies` |
| PATCH | `/v1/admin/workspaces/{workspace_id}/policies/{policy_id}` |
| DELETE | `/v1/admin/workspaces/{workspace_id}/policies/{policy_id}` |
| GET | `/v1/admin/workspaces/{workspace_id}/budget` |
| PUT | `/v1/admin/workspaces/{workspace_id}/budget` |
| GET | `/v1/admin/workspaces/{workspace_id}/usage` |
| GET | `/v1/admin/workspaces/{workspace_id}/agents` |
| GET | `/v1/admin/workspaces/{workspace_id}/blackboard` |
| GET | `/v1/admin/workspaces/{workspace_id}/blackboard/archive` |
| GET | `/v1/admin/workspaces/{workspace_id}/blackboard/tasks/{task_key}/events` |
| GET | `/v1/admin/workspaces/{workspace_id}/presence/delivery` |
| GET | `/v1/admin/workspaces/{workspace_id}/presence/history` |
| GET | `/v1/admin/workspaces/{workspace_id}/alerts` |
| POST | `/v1/admin/workspaces/{workspace_id}/alerts/{alert_id}/ack` |
| GET | `/v1/admin/workspaces/{workspace_id}/metrics` |
| GET | `/v1/admin/workspaces/{workspace_id}/human-users` |
| POST | `/v1/admin/workspaces/{workspace_id}/human-users` |
| PATCH | `/v1/admin/workspaces/{workspace_id}/human-users/{user_id}` |
| GET | `/v1/admin/workspaces/{workspace_id}/proposals` |
| POST | `/v1/admin/workspaces/{workspace_id}/proposals/{proposal_id}/apply` |
| POST | `/v1/admin/workspaces/{workspace_id}/proposals/{proposal_id}/reject` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}/taint` |
| GET | `/v1/admin/workspaces/{workspace_id}/index-status` |
| POST | `/v1/admin/workspaces/{workspace_id}/reindex` |
| GET | `/v1/admin/workspaces/{workspace_id}/webhooks` |
| POST | `/v1/admin/workspaces/{workspace_id}/webhooks` |
| PATCH | `/v1/admin/workspaces/{workspace_id}/webhooks/{rule_id}` |
| DELETE | `/v1/admin/workspaces/{workspace_id}/webhooks/{rule_id}` |
| POST | `/v1/admin/workspaces/{workspace_id}/webhooks/{rule_id}/test` |
| GET | `/v1/admin/workspaces/{workspace_id}/webhooks/{rule_id}/deliveries` |
| GET | `/v1/admin/workspaces/{workspace_id}/external-actions` |
| POST | `/v1/admin/workspaces/{workspace_id}/external-actions/{action_id}/approve` |
| POST | `/v1/admin/workspaces/{workspace_id}/external-actions/{action_id}/reject` |
| POST | `/v1/admin/workspaces/{workspace_id}/external-actions/{action_id}/compensate` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents/backfill-verified` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents/normalize-html` |
| POST | `/v1/admin/workspaces/{workspace_id}/documents/{doc_key:path}/normalize-html` |
| POST | `/v1/admin/workspaces/{workspace_id}/tasks/{task_key}/force-release` |
| DELETE | `/v1/admin/workspaces/{workspace_id}` |
| POST | `/v1/admin/workspaces/{workspace_id}/tasks/{task_key}/reassign` |
| POST | `/v1/admin/workspaces/{workspace_id}/tasks/{task_key}/cancel` |
| GET | `/v1/admin/budget-limits/{scope_kind}/{scope_id}` |
| PUT | `/v1/admin/budget-limits/{scope_kind}/{scope_id}` |
| POST | `/v1/admin/tenants/{tenant_id}/exports` |
| GET | `/v1/admin/exports/{export_id}` |
| GET | `/v1/admin/exports/{export_id}/download` |
| POST | `/v1/admin/imports` |
| GET | `/v1/admin/tenants` |
| GET | `/v1/admin/tenants/{tenant_id}` |
| POST | `/v1/admin/tenants/{tenant_id}/suspend` |
| POST | `/v1/admin/tenants/{tenant_id}/resume` |
| POST | `/v1/admin/tenants/{tenant_id}/delete` |
| POST | `/v1/admin/tenants/{tenant_id}/restore` |
| POST | `/v1/admin/tenants/{tenant_id}/children` |
| GET | `/v1/admin/backups` |
| POST | `/v1/admin/backups` |
| GET | `/v1/admin/backups/{backup_id}` |
| POST | `/v1/admin/restore` |
| GET | `/v1/admin/restores/{restore_id}` |
| GET | `/v1/admin/restore-drills` |
| POST | `/v1/admin/restore-drills` |
| GET | `/v1/admin/alerts` |
| POST | `/v1/admin/alerts/{alert_id}/ack` |
| GET | `/v1/admin/incidents` |
| POST | `/v1/admin/incidents` |
| GET | `/v1/admin/incidents/{incident_id}` |
| PATCH | `/v1/admin/incidents/{incident_id}` |
| POST | `/v1/admin/incidents/{incident_id}/confirm` |
| POST | `/v1/admin/incidents/{incident_id}/close` |
| POST | `/v1/admin/incidents/{incident_id}/scope` |
| POST | `/v1/admin/incidents/{incident_id}/notices` |
| POST | `/v1/admin/incidents/{incident_id}/updates` |
| GET | `/v1/admin/incidents/{incident_id}/record` |
| POST | `/v1/admin/incidents/check-sla` |
| POST | `/v1/admin/workspaces/{workspace_id}/root-token:rotate` |
| POST | `/v1/admin/tenants/{tenant_id}/rights-requests` |
| POST | `/v1/admin/rights-requests/{request_id}/locate` |
| POST | `/v1/admin/rights-requests/{request_id}/erase` |
| GET | `/v1/admin/rights-requests/{request_id}` |
| POST | `/v1/admin/eval/retrieval` |
| GET | `/v1/admin/eval/retrieval/runs` |
| POST | `/v1/admin/eval/retrieval/gate` |
| POST | `/v1/admin/eval/self-recall/run` |
| GET | `/v1/admin/eval/self-recall` |
| POST | `/v1/admin/tenants/{tenant_id}/signing-keys/rotate` |
| POST | `/v1/admin/exports/scheduled/run` |
| GET | `/v1/admin/growth` |

**`AgentGraph/app/api/agent_run_page.py`** — prefix `(none)`

| Method | Path |
|---|---|
| GET | `/dashboard/agent-runs/` |

**`AgentGraph/app/api/agent_run_routes.py`** — prefix `/v1/tasks`

| Method | Path |
|---|---|
| POST | `/v1/tasks/{task_key}/agent-run` |
| GET | `/v1/tasks/{task_key}/agent-run` |
| POST | `/v1/tasks/{task_key}/agent-run/cancel` |

**`AgentGraph/app/api/auth_routes.py`** — prefix `/v1/auth`

| Method | Path |
|---|---|
| GET | `/v1/auth/config` |
| POST | `/v1/auth/register` |
| POST | `/v1/auth/google` |
| POST | `/v1/auth/password` |
| POST | `/v1/auth/google/link` |
| POST | `/v1/auth/login` |
| GET | `/v1/auth/me` |
| POST | `/v1/auth/logout` |
| POST | `/v1/auth/accept-invite` |
| POST | `/v1/auth/switch-workspace` |
| POST | `/v1/auth/cli/sessions` |
| POST | `/v1/auth/cli/poll` |
| GET | `/v1/auth/cli/sessions/{user_code}` |
| POST | `/v1/auth/cli/sessions/{user_code}/approve` |
| POST | `/v1/auth/cli/sessions/{user_code}/deny` |
| GET | `/v1/auth/oauth/requests/{request_id}` |
| POST | `/v1/auth/oauth/requests/{request_id}/approve` |
| POST | `/v1/auth/oauth/requests/{request_id}/deny` |

**`AgentGraph/app/api/blob_routes.py`** — prefix `/v1`

| Method | Path |
|---|---|
| POST | `/v1/blobs` |
| GET | `/v1/blobs` |
| GET | `/v1/blobs/{blob_id}` |
| DELETE | `/v1/blobs/{blob_id}` |
| POST | `/v1/blobs/{blob_id}/restore` |
| POST | `/v1/blobs/gc` |
| POST | `/v1/blobs/migrate-parts` |
| POST | `/v1/blobs/uploads` |
| PUT | `/v1/blobs/uploads/{upload_id}/parts/{part_index}` |
| POST | `/v1/blobs/uploads/{upload_id}/complete` |
| DELETE | `/v1/blobs/uploads/{upload_id}` |
| GET | `/v1/documents/{doc_key:path}/blobs` |
| POST | `/v1/documents/{doc_key:path}/blobs` |
| DELETE | `/v1/documents/{doc_key:path}/blobs/{blob_id}` |
| POST | `/v1/cleanup-grants` |
| GET | `/v1/cleanup-grants` |
| DELETE | `/v1/cleanup-grants/{grant_id}` |

**`AgentGraph/app/api/code_page.py`** — prefix `(none)`

| Method | Path |
|---|---|
| GET | `/dashboard/code/` |

**`AgentGraph/app/api/code_routes.py`** — prefix `/v1/code`

| Method | Path |
|---|---|
| GET | `/v1/code/runs` |
| POST | `/v1/code/runs` |
| GET | `/v1/code/runs/{run_id}` |
| POST | `/v1/code/runs/{run_id}/sync` |
| GET | `/v1/code/runs/{run_id}/audit` |

**`AgentGraph/app/api/event_admin_routes.py`** — prefix `/v1/admin/event-inbox`

| Method | Path |
|---|---|
| GET | `/v1/admin/event-inbox/stats` |
| POST | `/v1/admin/event-inbox/{inbox_id}/replay` |
| POST | `/v1/admin/event-inbox/dispatch` |

**`AgentGraph/app/api/human_routes.py`** — prefix `/v1/human`

| Method | Path |
|---|---|
| GET | `/v1/human/namespaces` |
| DELETE | `/v1/human/namespaces/{namespace}` |
| GET | `/v1/human/agent-tokens` |
| GET | `/v1/human/agent-onboarding` |
| POST | `/v1/human/agent-tokens` |
| PATCH | `/v1/human/agent-tokens/{token_id}` |
| POST | `/v1/human/agent-tokens/{token_id}/revoke-delegated` |
| GET | `/v1/human/workspace` |
| GET | `/v1/human/automation` |
| PUT | `/v1/human/automation` |
| POST | `/v1/human/tasks/{task_key}/review` |
| POST | `/v1/human/escalations/{escalation_id}/resolve` |
| POST | `/v1/human/commander-grants` |
| GET | `/v1/human/commander-grants` |
| POST | `/v1/human/commander-grants/{grant_id}/revoke` |
| POST | `/v1/human/tasks/{task_key}/escalate` |
| PUT | `/v1/human/slots/{slot_key:path}` |
| POST | `/v1/human/documents` |
| PUT | `/v1/human/documents/{doc_key:path}` |
| POST | `/v1/human/documents/{doc_key:path}/verify` |
| POST | `/v1/human/documents/{doc_key:path}/submit-review` |
| POST | `/v1/human/documents/{doc_key:path}/deprecate` |
| POST | `/v1/human/documents/{doc_key:path}/taint` |
| POST | `/v1/human/batches/{batch_id}/undo` |
| GET | `/v1/human/audit` |
| GET | `/v1/human/proposals` |
| GET | `/v1/human/proposals/{proposal_id}` |
| POST | `/v1/human/proposals/{proposal_id}/apply` |
| POST | `/v1/human/proposals/{proposal_id}/reject` |
| GET | `/v1/human/external-actions` |
| POST | `/v1/human/external-actions/{action_id}/approve` |
| POST | `/v1/human/external-actions/{action_id}/reject` |
| POST | `/v1/human/external-actions/{action_id}/compensate` |
| GET | `/v1/human/usage` |
| GET | `/v1/human/budget` |
| PUT | `/v1/human/budget` |
| GET | `/v1/human/accounts` |
| POST | `/v1/human/accounts` |
| PATCH | `/v1/human/accounts/{user_id}` |
| GET | `/v1/human/policies` |
| POST | `/v1/human/policies` |
| PATCH | `/v1/human/policies/{policy_id}` |
| DELETE | `/v1/human/policies/{policy_id}` |
| GET | `/v1/human/webhooks` |
| POST | `/v1/human/webhooks` |
| PATCH | `/v1/human/webhooks/{rule_id}` |
| DELETE | `/v1/human/webhooks/{rule_id}` |
| POST | `/v1/human/webhooks/{rule_id}/test` |
| GET | `/v1/human/webhooks/{rule_id}/deliveries` |
| GET | `/v1/human/inbox` |
| POST | `/v1/human/inbox/ack` |
| POST | `/v1/human/reindex` |
| GET | `/v1/human/tenant` |
| GET | `/v1/human/onboarding` |
| POST | `/v1/human/onboarding` |
| GET | `/v1/human/tenant/final-export` |
| POST | `/v1/human/tenant/delete` |
| POST | `/v1/human/tenant/restore` |
| POST | `/v1/human/documents/{doc_key:path}/purge` |
| POST | `/v1/human/transfer-responsibilities` |
| GET | `/v1/human/orphan-tasks` |
| POST | `/v1/human/orphan-tasks/claim` |
| POST | `/v1/human/budget/circuits/{principal_key}/clear` |
| GET | `/v1/human/incident-notices` |
| GET | `/v1/human/tenant/security-contacts` |
| PUT | `/v1/human/tenant/security-contacts` |
| POST | `/v1/human/root-token:rotate` |
| POST | `/v1/human/agents/merge-legacy` |
| PUT | `/v1/human/quality/tiers` |
| POST | `/v1/human/quality/circuit/{scope_key:path}/close` |
| GET | `/v1/human/quality/samples` |
| POST | `/v1/human/quality/samples/{sample_id}/verdict` |
| POST | `/v1/human/quality/escapes` |
| POST | `/v1/human/quality/escapes/{escape_id}/resolve` |
| POST | `/v1/human/quality/poison` |
| GET | `/v1/human/quality/poison` |
| GET | `/v1/human/quality/report` |
| GET | `/v1/human/document-parts` |
| POST | `/v1/human/product-events` |
| GET | `/v1/human/support-access` |
| POST | `/v1/human/support-access` |
| DELETE | `/v1/human/support-access` |
| POST | `/v1/human/support-access/make-private` |
| POST | `/v1/human/groups/{conv_key}/approve` |
| GET | `/v1/human/blackboard` |
| GET | `/v1/human/blackboard/archive` |
| GET | `/v1/human/blackboard/tasks/{task_key}/events` |

**`AgentGraph/app/api/inbox_stream.py`** — prefix `/v1`

| Method | Path |
|---|---|
| GET | `/v1/inbox/stream` |

**`AgentGraph/app/api/oauth_routes.py`** — prefix `(none)`

| Method | Path |
|---|---|
| GET | `/.well-known/oauth-protected-resource` |
| GET | `/.well-known/oauth-protected-resource/{resource:path}` |
| GET | `/.well-known/oauth-authorization-server` |
| GET | `/.well-known/oauth-authorization-server/{issuer:path}` |
| GET | `/.well-known/openid-configuration` |
| POST | `/oauth/register` |
| GET | `/oauth/authorize` |
| POST | `/oauth/token` |

**`AgentGraph/app/api/org_routes.py`** — prefix `/v1`

| Method | Path |
|---|---|
| GET | `/v1/tenants/{tenant_id}/members` |
| POST | `/v1/tenants/{tenant_id}/members` |
| PATCH | `/v1/tenants/{tenant_id}/members/{principal_id}` |
| DELETE | `/v1/tenants/{tenant_id}/members/{principal_id}` |
| POST | `/v1/tenants/{tenant_id}/children` |
| GET | `/v1/tenants/{tenant_id}/tree` |
| POST | `/v1/tenants/{tenant_id}/move` |
| POST | `/v1/tenants/{tenant_id}/attach` |
| PUT | `/v1/tenants/{tenant_id}/policies/{key}` |
| GET | `/v1/tenants/{tenant_id}/effective-policy` |
| PUT | `/v1/tenants/{tenant_id}/quotas/{dimension}` |
| GET | `/v1/tenants/{tenant_id}/usage` |
| POST | `/v1/tenants/{tenant_id}/delete` |
| POST | `/v1/tenants/{tenant_id}/restore` |
| POST | `/v1/tenants/{tenant_id}/detach` |
| POST | `/v1/shares` |
| GET | `/v1/shares` |
| DELETE | `/v1/shares/{share_id}` |
| POST | `/v1/tenants/{tenant_id}/access-sessions` |
| POST | `/v1/access-sessions/{access_session_id}/close` |
| POST | `/v1/tenants/{tenant_id}/rights-requests` |
| GET | `/v1/tenants/{tenant_id}/rights-requests` |
| GET | `/v1/rights-requests/{request_id}` |
| POST | `/v1/rights-requests/{request_id}/locate` |
| POST | `/v1/rights-requests/{request_id}/export` |
| GET | `/v1/rights-requests/{request_id}/export/download` |
| POST | `/v1/rights-requests/{request_id}/erase` |
| GET | `/v1/rights-requests/{request_id}/explain` |
| POST | `/v1/rights-requests/{request_id}/close` |

**`AgentGraph/app/api/project_page.py`** — prefix `(none)`

| Method | Path |
|---|---|
| GET | `/dashboard/projects/` |

**`AgentGraph/app/api/project_routes.py`** — prefix `/v1`

| Method | Path |
|---|---|
| GET | `/v1/projects` |
| POST | `/v1/projects` |
| GET | `/v1/execution-graph` |
| GET | `/v1/projects/{project}/execution` |
| GET | `/v1/deliveries` |
| GET | `/v1/projects/{project}/deliveries` |
| GET | `/v1/runtime/observability` |
| GET | `/v1/usage/runtime` |
| GET | `/v1/tenant/runtime-limit` |
| PUT | `/v1/tenant/runtime-limit` |

**`AgentGraph/app/api/public_docs.py`** — prefix `(none)`

| Method | Path |
|---|---|
| GET | `/docs` |
| GET | `/docs/` |
| GET | `/docs/{slug}/` |

**`AgentGraph/app/api/share_routes.py`** — prefix `/v1`

| Method | Path |
|---|---|
| POST | `/v1/documents/{doc_key:path}/share-links` |
| GET | `/v1/documents/{doc_key:path}/share-links` |
| DELETE | `/v1/share-links/{link_id}` |
| GET | `/v1/share-links/{link_id}/accesses` |

**`AgentGraph/app/api/stream_routes.py`** — prefix `/v1`

| Method | Path |
|---|---|
| GET | `/v1/stream` |

**`AgentGraph/app/api/v1_routes.py`** — prefix `/v1`

| Method | Path |
|---|---|
| GET | `/v1/orientation` |
| POST | `/v1/orientation/ack` |
| POST | `/v1/orientation/memory-seed/ack` |
| GET | `/v1/whoami` |
| GET | `/v1/workspace/map` |
| GET | `/v1/sync` |
| GET | `/v1/events` |
| POST | `/v1/subscriptions` |
| GET | `/v1/subscriptions` |
| DELETE | `/v1/subscriptions/{subscription_id}` |
| GET | `/v1/agent-docs` |
| GET | `/v1/openapi.json` |
| GET | `/v1/tasks` |
| POST | `/v1/tasks` |
| GET | `/v1/tasks/{task_key}` |
| POST | `/v1/tasks/{task_key}/claim` |
| POST | `/v1/tasks/{task_key}/renew` |
| PATCH | `/v1/tasks/{task_key}` |
| POST | `/v1/tasks/{task_key}/lease/renew` |
| POST | `/v1/leases/{lease_id}/heartbeat` |
| POST | `/v1/leases/{lease_id}/release` |
| GET | `/v1/leases` |
| POST | `/v1/tasks/{task_key}/release` |
| POST | `/v1/tasks/{task_key}/handoff` |
| POST | `/v1/tasks/{task_key}/review` |
| POST | `/v1/tasks/{task_key}/escalate` |
| POST | `/v1/tasks/{task_key}/continuation` |
| POST | `/v1/tasks/{task_key}/subtasks` |
| GET | `/v1/tasks/{task_key}/tree` |
| POST | `/v1/escalations` |
| GET | `/v1/escalations` |
| POST | `/v1/escalations/{escalation_id}/resolve` |
| GET | `/v1/slots` |
| POST | `/v1/slots/{slot_key:path}/patch` |
| PUT | `/v1/slots/{slot_key:path}` |
| GET | `/v1/slots/{slot_key:path}` |
| GET | `/v1/documents` |
| POST | `/v1/documents/{doc_key:path}/verify` |
| POST | `/v1/documents/{doc_key:path}/submit-review` |
| POST | `/v1/documents/{doc_key:path}/deprecate` |
| GET | `/v1/documents/{doc_key:path}/explain` |
| POST | `/v1/tokens` |
| POST | `/v1/tokens/{ref}/revoke` |
| POST | `/v1/tokens/issue` |
| PATCH | `/v1/tokens/{ref}/namespaces` |
| POST | `/v1/tokens/{ref}/renew` |
| POST | `/v1/tokens/{ref}/rotate` |
| POST | `/v1/agents/register` |
| GET | `/v1/agents` |
| POST | `/v1/agents/me/status` |
| GET | `/v1/agents/{ref}` |
| POST | `/v1/agents/merge` |
| POST | `/v1/agents/{ref}/generations` |
| GET | `/v1/agents/{ref}/generations` |
| GET | `/v1/agents/{ref}/references` |
| GET | `/v1/agents/{ref}/name-history` |
| PATCH | `/v1/agents/{ref}` |
| GET | `/v1/tokens/{ref}` |
| GET | `/v1/tokens/{ref}/lineage` |
| GET | `/v1/tokens/{ref}/tree` |
| GET | `/v1/documents/{doc_key:path}/versions` |
| GET | `/v1/documents/{doc_key:path}/versions/{version}` |
| POST | `/v1/documents/{doc_key:path}/patch-section` |
| POST | `/v1/documents/{doc_key:path}/proposals` |
| GET | `/v1/documents/{doc_key:path}/proposals` |
| GET | `/v1/proposals` |
| GET | `/v1/proposals/{proposal_id}` |
| POST | `/v1/proposals/{proposal_id}/vote` |
| POST | `/v1/proposals/{proposal_id}/apply` |
| POST | `/v1/proposals/{proposal_id}/reject` |
| POST | `/v1/documents/{doc_key:path}/taint` |
| POST | `/v1/documents/{doc_key:path}/entries` |
| GET | `/v1/documents/{doc_key:path}/entries` |
| GET | `/v1/documents/{doc_key:path}/sections` |
| GET | `/v1/webhooks` |
| POST | `/v1/webhooks` |
| DELETE | `/v1/webhooks/{rule_id}` |
| GET | `/v1/inbox` |
| POST | `/v1/inbox/ack` |
| PUT | `/v1/agents/me/wake` |
| POST | `/v1/presence/heartbeat` |
| POST | `/v1/runner/hello` |
| POST | `/v1/runner/heartbeat` |
| GET | `/v1/loop/overview` |
| GET | `/v1/runner/hosts` |
| GET | `/v1/runner/hosts/{ref}` |
| GET | `/v1/runner/placements` |
| GET | `/v1/runner/doctor` |
| GET | `/v1/runner/placements/{placement_id}` |
| POST | `/v1/runner/placements/{placement_id}/report` |
| POST | `/v1/runner/placements/{placement_id}/move` |
| POST | `/v1/runner/placements/{placement_id}/withdraw` |
| GET | `/v1/runner/agenda` |
| POST | `/v1/runner/schedule` |
| POST | `/v1/runner/hosts/{ref}/pause` |
| POST | `/v1/runner/hosts/{ref}/resume` |
| POST | `/v1/runner/hosts/{ref}/revoke` |
| GET | `/v1/presence` |
| GET | `/v1/presence/delivery` |
| GET | `/v1/presence/history` |
| GET | `/v1/diagrams` |
| POST | `/v1/diagrams` |
| GET | `/v1/diagrams/{diagram_key}` |
| PUT | `/v1/diagrams/{diagram_key}` |
| POST | `/v1/diagrams/{diagram_key}/policy` |
| POST | `/v1/diagrams/{diagram_key}/confirm` |
| POST | `/v1/diagrams/{diagram_key}/freeze` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/dispatch` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/gate` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/submit` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/ci` |
| GET | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/triptych` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/queue-check` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/escalate` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/signal` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/usage` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/budget` |
| GET | `/v1/diagrams/{diagram_key}/queue` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/merge` |
| POST | `/v1/diagrams/{diagram_key}/chunks/{chunk_key}/repoint` |
| POST | `/v1/diagrams/{diagram_key}/changes` |
| POST | `/v1/diagrams/{diagram_key}/increments` |
| GET | `/v1/diagrams/{diagram_key}/changes/{change_key}` |
| POST | `/v1/diagrams/{diagram_key}/changes/{change_key}/decide` |
| POST | `/v1/diagrams/{diagram_key}/changes/{change_key}/revoke` |
| GET | `/v1/diagrams/{diagram_key}/replay` |
| POST | `/v1/tasks/{task_key}/cancel` |
| GET | `/v1/pokes/receipts` |
| POST | `/v1/quality/gate-results` |
| GET | `/v1/quality/tasks/{ref}/audit-view` |
| PUT | `/v1/tasks/{ref}/acceptance` |
| POST | `/v1/quality/claims` |
| POST | `/v1/quality/claims/{claim_id}/verify` |
| GET | `/v1/quality/samples` |
| POST | `/v1/quality/samples/{sample_id}/claim` |
| POST | `/v1/quality/samples/{sample_id}/verdict` |
| GET | `/v1/quality/report` |
| POST | `/v1/lineage/attestations` |
| POST | `/v1/supersessions` |
| PUT | `/v1/watermarks/{name}` |
| GET | `/v1/watermarks/{name}` |
| GET | `/v1/digest` |
| POST | `/v1/peer-reviews` |
| POST | `/v1/peer-reviews/{task_key}/submit` |
| GET | `/v1/resources` |
| POST | `/v1/resources` |
| POST | `/v1/resources/acquire` |
| POST | `/v1/resources/release/{lease_id}` |
| GET | `/v1/alerts` |
| POST | `/v1/alerts/{alert_id}/ack` |
| GET | `/v1/metrics` |
| POST | `/v1/documents/{doc_key:path}/clear-taint` |
| POST | `/v1/documents/{doc_key:path}/recall` |
| GET | `/v1/recalls/{recall_id}` |
| POST | `/v1/recalls/{recall_id}/dismiss` |
| GET | `/v1/tokens/{token_ref}/budget` |
| PUT | `/v1/tokens/{token_ref}/budget` |
| GET | `/v1/budgets` |
| GET | `/v1/policies` |
| GET | `/v1/usage` |
| POST | `/v1/exports` |
| GET | `/v1/exports` |
| GET | `/v1/exports/{export_id}` |
| GET | `/v1/exports/{export_id}/download` |
| POST | `/v1/imports` |
| GET | `/v1/index/status` |
| GET | `/v1/usage/embedding` |
| POST | `/v1/external-actions` |
| GET | `/v1/external-actions` |
| POST | `/v1/documents:batchUpsert` |
| POST | `/v1/documents/batch` |
| POST | `/v1/documents:batchGet` |
| POST | `/v1/dereference-many` |
| POST | `/v1/documents/{doc_key:path}/archive` |
| POST | `/v1/documents/{doc_key:path}/rename` |
| POST | `/v1/documents/{doc_key:path}/parts` |
| GET | `/v1/namespaces` |
| POST | `/v1/namespaces` |
| POST | `/v1/documents/{doc_key:path}/restore` |
| PUT | `/v1/documents/{doc_key:path}` |
| GET | `/v1/documents/{doc_key:path}` |
| POST | `/v1/query` |
| POST | `/v1/dereference` |
| POST | `/v1/links` |
| GET | `/v1/links` |
| POST | `/v1/timeline` |
| GET | `/v1/timeline` |
| GET | `/v1/audit` |
| GET | `/v1/stats` |

**`AgentGraph/app/api/wellknown_routes.py`** — prefix `/.well-known/agentgraph`

| Method | Path |
|---|---|
| GET | `/.well-known/agentgraph/tenants/{alias}/jwks.json` |
| GET | `/.well-known/agentgraph/tenants/{alias}/revoked` |


### veynod_kernel/app/api

**`veynod_kernel/app/api/admin_routes.py`** — prefix `/v1/admin`

| Method | Path |
|---|---|
| POST | `/v1/admin/workspaces` |
| GET | `/v1/admin/workspaces` |
| POST | `/v1/admin/tokens` |
| GET | `/v1/admin/tokens` |
| POST | `/v1/admin/tokens/{token_id}/revoke` |
| POST | `/v1/admin/tokens/{token_id}/rotate` |
| GET | `/v1/admin/event-outbox/stats` |
| GET | `/v1/admin/event-outbox/dead-letter` |
| GET | `/v1/admin/metrics` |
| POST | `/v1/admin/metrics/reset` |
| POST | `/v1/admin/artifacts` |
| GET | `/v1/admin/artifacts` |
| POST | `/v1/admin/tasks` |
| GET | `/v1/admin/tasks` |
| GET | `/v1/admin/tasks/{task_id}` |
| GET | `/v1/admin/tasks/{task_id}/evidence` |
| POST | `/v1/admin/tasks/{task_id}/cancel` |
| POST | `/v1/admin/workspaces/{workspace_id}/scan-stubs` |
| POST | `/v1/admin/workspaces/{workspace_id}/edges/rebuild` |

**`veynod_kernel/app/api/cell_routes.py`** — prefix `/v1/admin`

| Method | Path |
|---|---|
| GET | `/v1/admin/cells` |
| POST | `/v1/admin/cells` |
| GET | `/v1/admin/repositories` |
| POST | `/v1/admin/repositories/{workspace_id}/move` |

**`veynod_kernel/app/api/code_intel_routes.py`** — prefix `/v1/admin`

| Method | Path |
|---|---|
| GET | `/v1/admin/tasks/{task_id}/snapshot` |
| GET | `/v1/admin/tasks/{task_id}/delivery` |
| GET | `/v1/admin/merge-conflicts` |
| POST | `/v1/admin/merge-conflicts/{conflict_id}/resolve` |

**`veynod_kernel/app/api/dag_routes.py`** — prefix `(none)`

| Method | Path |
|---|---|
| GET | `/v1/dag` |
| GET | `/v1/dag/plan` |
| GET | `/v1/dag/events` |
| GET | `/dashboard` |
| GET | `/healthz` |

**`veynod_kernel/app/api/runner_routes.py`** — prefix `/v1`

| Method | Path |
|---|---|
| GET | `/v1/events/stream` |
| POST | `/v1/runner/heartbeat` |
| POST | `/v1/runner/release` |
| GET | `/v1/runner/tasks/{task_id}` |
| GET | `/v1/runner/ping` |



### 补充：Agent Runtime API（`agent_runtime/api.py`，prefix 无）

| Method | Path |
|---|---|
| GET | `/healthz` |
| POST | `/v1/runs` |
| GET | `/v1/runs/{run_id}` |
| POST | `/v1/runs/{run_id}/transition` |
| POST | `/v1/runs/{run_id}/start` |
| POST | `/v1/runs/{run_id}/pause` |
| POST | `/v1/runs/{run_id}/resume` |
| POST | `/v1/runs/{run_id}/cancel` |
| POST | `/v1/runs/{run_id}/checkpoint` |
| POST | `/v1/runs/{run_id}/delegate` |
| GET | `/v1/runs/{run_id}/children` |
| POST | `/v1/runs/{run_id}/collect` |
| POST | `/v1/delegations/reconcile` |
| GET | `/v1/runs/{run_id}/checkpoints` |
| GET | `/v1/runs/{run_id}/events` |
| GET | `/v1/events/head` |
| GET | `/v1/events` |
| GET | `/v1/stats` |
| GET | `/v1/usage/{tenant_id}` |
| GET | `/v1/quota/{tenant_id}` |
| GET | `/v1/provider-health` |

### 补充：Kernel syscall / MCP（`veynod_kernel/app/mcp_server/syscalls.py`）

| Method | Path |
|---|---|
| POST | `/v1/syscalls/pull_task_bundle` |
| POST | `/v2/syscalls/submit_patch` |
| GET | `/v2/submissions/find` |
| GET | `/v2/submissions/{submission_id}` |
| POST | `/v1/syscalls/submit_patch`（deprecated，返回 successor） |
| POST | `/v1/syscalls/request_task_split` |
| POST | `/v1/syscalls/escalate_contract_defect` |
| POST | `/v1/syscalls/inject_dependency_task` |
| POST | `/v1/syscalls/query_symbol_signature` |
| POST | `/mcp`（JSON-RPC，6 syscalls） |

### 补充：AgentGraph MCP / SSE / UI

| Method | Path |
|---|---|
| POST | `/mcp`（`app/mcp_server/server.py::create_http_app`） |
| GET | `/v1/stream` |
| GET | `/v1/admin/workspaces/{workspace_id}/stream` |
| GET | `/v1/inbox/stream` |
| GET | `/dashboard/`（Next 静态导出） |
| GET | `/dashboard/code/` |
| GET | `/dashboard/agent-runs/` |
| GET | `/dashboard/projects/` |
| GET | `/docs/`（静态文档） |
| GET | `/healthz`、`/readyz` |


---

# 附录 B：核心类/方法签名索引

> 由 AST 从当前源码提取；仅列关键执行链文件，完整签名以此为准。路径统一为 `/`。

```text
### AgentGraph/app/services/tasks.py
  def _record_task_failure(principal: Principal, task_key: str, operation: str, exc: ApiError)
  def _task_failure_event(operation: str)
  def _get_task(session: AsyncSession, workspace_id: uuid.UUID, task_key: str, *, for_update: bool=False)
  def _check_version(task: CollabTask, expected: int | None)
  def _check_lease(task: CollabTask, agent: str, now: Any | None=None)
  def _require_active_lease(task: CollabTask, agent: str, now: Any)
  def _check_epoch(task: CollabTask, expected: int | None)
  def _check_assignee(task: CollabTask, principal: Principal, agent: str)
  def _require_claim_owner(task: CollabTask, principal: Principal, agent: str)
  def validate_review_next_status(decision: str, next_status: str | None)
  def _resolve_review_history(history: list[Any] | None)
  def _clear_lease(task: CollabTask)
  def _upstream_briefs(session: AsyncSession, task: CollabTask, principal: Principal)
  def _context_slots(session: AsyncSession, principal: Principal, task: CollabTask)
  def _context_documents(session: AsyncSession, principal: Principal, task: CollabTask)
  def _create_task_core(session: AsyncSession, principal: Principal, request: TaskCreateRequest, *, agent: str, batch: uuid.UUID, commit: bool, parent: CollabTask | None=None, by_graph: bool=False)
  def create_task(session: AsyncSession, principal: Principal, request: TaskCreateRequest)
  def list_tasks(session: AsyncSession, principal: Principal, *, statuses: list[str] | None=None, assigned_role: str | None=None, namespace: str | None=None, limit: int=100, assignee: str | None=None, required_role: str | None=None, parent: str | None=None, kind: str | None=None, claimable_by_me: bool=False, task_keys: list[str] | None=None, diagram_key: str | None=None)
  def claimable_conditions(session: AsyncSession, principal: Principal)
  def _warn_out_of_reach(session: AsyncSession, principal: Principal)
  def get_task(session: AsyncSession, principal: Principal, task_key: str)
  def acceptance_state(session: AsyncSession, task: CollabTask)
  def claim_task(session: AsyncSession, principal: Principal, task_key: str, request: TaskClaimRequest)
  def renew_task(session: AsyncSession, principal: Principal, task_key: str, request: TaskRenewRequest)
  def renew_lease_explicit(session: AsyncSession, principal: Principal, task_key: str, request: TaskLeaseRenewRequest)
  def release_task(session: AsyncSession, principal: Principal, task_key: str, request: TaskReleaseRequest)
  def _release_dependents(session: AsyncSession, completed: CollabTask, principal: Principal)
  def handoff_task(session: AsyncSession, principal: Principal, task_key: str, request: TaskHandoffRequest)
  def _require_approve_done(principal: Principal)
  def review_task(session: AsyncSession, principal: Principal, task_key: str, request: TaskReviewRequest, *, grant_id: str | None=None)
  def _answer_by_review(session: AsyncSession, principal: Principal, task: CollabTask, request: TaskReviewRequest)
  def escalate_task(session: AsyncSession, principal: Principal, task_key: str, request: TaskEscalateRequest)
  def reap_expired_task_leases(session: AsyncSession, limit: int=50)
  def reap_task_timeouts(session: AsyncSession, threshold_seconds: int | None=None, limit: int=100)
  def reassign_task(session: AsyncSession, workspace_id: uuid.UUID, task_key: str, *, assigned_role: str, reason: str | None, expected_version: int | None, reset_to_ready: bool, actor: str='human_commander')
  def cancel_task(session: AsyncSession, workspace_id: uuid.UUID, task_key: str, *, reason: str, expected_version: int | None, actor: str='human_commander', propagate_to_kernel: bool=True)
  def fail_task(session: AsyncSession, principal: Principal, task_key: str, *, reason: str, expected_version: int | None=None)
  def cancel_own_task(session: AsyncSession, principal: Principal, task_key: str, *, reason: str, expected_version: int | None, grant_id: str | None=None)
  def list_orphan_tasks(session: AsyncSession, principal: Principal)
  def claim_orphan_tasks(session: AsyncSession, principal: Principal, task_keys: list[str], assignee: str | None=None, reason: str | None=None)
  def _append_erratum(session: AsyncSession, principal: Principal, task: CollabTask, agent: str, text_: str)
  def write_continuation(session: AsyncSession, principal: Principal, task_key: str, request: Any)
  def super_admin_update_task(session: AsyncSession, principal: Principal, task_key: str, request: TaskUpdateRequest)
  def create_subtasks(session: AsyncSession, principal: Principal, task_key: str, request: Any)
  def get_task_tree(session: AsyncSession, principal: Principal, task_key: str, *, depth: int, include: list[str])
  def create_escalation(session: AsyncSession, principal: Principal, request: Any)
  def list_escalations(session: AsyncSession, principal: Principal, *, status: str | None)
  def resolve_escalation(session: AsyncSession, principal: Principal, escalation_id: str, request: Any)
### AgentGraph/app/services/code_engine.py
  class CodeRunCreate
  class CodeEngineError
    def __init__(self, message: str, status_code: int=502)
  class KernelEngineClient
    def __init__(self, base_url: str, admin_key: str, timeout: float=45)
    def _request(self, method: str, path: str, **kwargs: Any)
    def create_artifact(self, workspace_id: str, symbol: str, *, kind: str)
    def create_task(self, workspace_id: str, payload: dict[str, Any])
    def list_tasks(self, workspace_id: str)
    def task(self, workspace_id: str, task_id: str)
    def evidence(self, task_id: str)
    def delivery(self, task_id: str)
    def cancel_task(self, task_id: str, reason: str)
    def latest_event_seq(self, workspace_id: str)
    def events(self, workspace_id: str, after_seq: int, limit: int=200)
  def _engine()
  def _workspace_id()
  def _task_dict(task: Any)
  def _status(task: Any)
  def gate_updates_from_evidence(evidence: dict[str, Any], commit_sha: str | None)
  def create_run(session: AsyncSession, principal: Principal, request: CodeRunCreate)
  def _kernel_ids(task: Any)
  def cancel_kernel_run(task: Any, reason: str)
  def list_runs(session: AsyncSession, principal: Principal)
  def lifecycle_action(kernel_status: str, task_status: str)
  def _sync_lifecycle(session: AsyncSession, principal: Principal, task: Any, kernel: dict[str, Any])
  def get_run(session: AsyncSession, principal: Principal, task_key: str)
  def audit_run(session: AsyncSession, principal: Principal, task_key: str)
  def project_code_runs_once(limit: int | None=None)
  def _project_code_runs_once(limit: int | None=None)
  def event_task_ids(events: list[dict[str, Any]])
  def _event_cursor(engine: KernelEngineClient, kernel_workspace: str)
  def merge_kernel_workspace_ids(configured: str | None, discovered: list[str])
  def _kernel_workspace_ids()
  def sync_kernel_task_event(kernel_workspace: str, kernel_task_id: str)
  def _project_one_kernel_workspace(engine: KernelEngineClient, kernel_workspace: str, limit: int)
  def project_code_events_once(limit: int=200)
  def code_projection_event_loop()
  def code_projection_loop()
### AgentGraph/app/services/agent_runtime.py
  class AgentRunCreate
  class AgentRuntimeError
    def __init__(self, message: str, status_code: int=502)
  class AgentRuntimeClient
    def __init__(self, base_url: str, api_key: str, timeout: float=30, *, transport: httpx.AsyncBaseTransport | None=None)
    def _request(self, method: str, path: str, **kwargs: Any)
    def create_run(self, *, tenant_id: str, workspace_id: str, product_task_id: str, budget: dict[str, Any], goal: str | None=None)
    def tenant_usage(self, tenant_id: str)
    def provider_health(self, window_minutes: int=15)
    def events(self, after_seq: int, limit: int=500)
    def latest_event_seq(self)
    def tenant_quota(self, tenant_id: str)
    def start_run(self, run_id: str)
    def get_run(self, run_id: str)
    def cancel_run(self, run_id: str, reason: str)
  def _client()
  def _run_ids(task: Any)
  def runtime_status_action(runtime_status: str, task_status: str)
  def create_task_run(session: AsyncSession, principal: Principal, task_key: str, request: AgentRunCreate)
  def meter_runtime_run(session: AsyncSession, *, workspace_id: Any, run: dict[str, Any], tenant_id: str)
  def get_task_run(session: AsyncSession, principal: Principal, task_key: str)
  def cancel_task_run(session: AsyncSession, principal: Principal, task_key: str, reason: str)
  def sync_runtime_run_event(base_url: str, run_id: str)
  def project_agent_runs_once(limit: int=100)
  def agent_run_projection_loop()
### AgentGraph/app/services/event_dispatch.py
  class InboxRow
  def enqueue_kernel_events(kernel_workspace: str, events: list[dict[str, Any]])
  def enqueue_runtime_events(base_url: str, events: list[dict[str, Any]])
  def _json(value: Any)
  def ingest_kernel_once(limit: int=500)
  def _runtime_base_urls()
  def _runtime_cursor(base_url: str)
  def ingest_runtime_once(limit: int=500)
  def _due_rows(limit: int)
  def _mark_processed(inbox_id: int)
  def _mark_failed(row: InboxRow, error: str)
  def _process_row(row: InboxRow)
  def dispatch_once(limit: int=100)
  def inbox_stats()
  def list_inbox(status: str | None=None, source: str | None=None, limit: int=100)
  def replay(inbox_id: int)
  def event_dispatch_loop()
### AgentGraph/app/services/projects.py
  def list_projects(session: AsyncSession, principal: Principal)
  def create_project(session: AsyncSession, principal: Principal, *, name: str, repo: str | None, description: str)
  def _task_node(task: CollabTask)
  def execution_graph(session: AsyncSession, principal: Principal, project: str | None=None)
  def deliveries(session: AsyncSession, principal: Principal, *, project: str | None=None, limit: int=50)
  def runtime_observability(session: AsyncSession, principal: Principal)
  def usage_summary(session: AsyncSession, principal: Principal)
  def get_tenant_limit(session: AsyncSession, tenant_id: str)
  def set_tenant_limit(session: AsyncSession, tenant_id: str, *, max_model_calls: float | None, max_cost_usd: float | None)
### AgentGraph/app/services/leases.py
  def lease_out(lease: Lease)
  def ttl_for(lease_ttl_seconds: int | None, lease_seconds: int | None)
  def continuation_key(task: CollabTask)
  def clear_task_lease(task: CollabTask)
  def grant_task_lease(session: AsyncSession, principal: Principal, task: CollabTask, agent: str, ttl: int)
  def active_task_lease(session: AsyncSession, task: CollabTask)
  def _get_lease(session: AsyncSession, workspace_id: uuid.UUID, lease_id: str)
  def check_fencing(task: CollabTask, presented: int | None)
  def _lost(task: CollabTask, lease: Lease | None)
  def require_holder(session: AsyncSession, task: CollabTask, agent: str, now: datetime)
  def touch(session: AsyncSession, task: CollabTask, now: datetime)
  def heartbeat(session: AsyncSession, principal: Principal, lease_id: str, request: LeaseHeartbeatRequest)
  def _heartbeat_resource(session: AsyncSession, lease: Lease, request: LeaseHeartbeatRequest)
  def _slot_for(session: AsyncSession, lease: Lease)
  def _heartbeat_slot(session: AsyncSession, lease: Lease, request: LeaseHeartbeatRequest)
  def _end_slot_lease(session: AsyncSession, lease: Lease, *, state: str, reason: str)
  def release(session: AsyncSession, principal: Principal, lease_id: str, *, reason: str | None, agent: str | None)
  def _encode_cursor(lease: Lease)
  def _decode_cursor(cursor: str)
  def list_leases(session: AsyncSession, principal: Principal, *, holder: str | None, state: str | None, cursor: str | None, limit: int)
  def _auto_continuation(session: AsyncSession, task: CollabTask, lease: Lease | None, holder: str, reason: str)
  def orphan_task(session: AsyncSession, task: CollabTask, *, state: str, reason: str, actor: str)
  def flag_missed_heartbeats(session: AsyncSession, limit: int=200)
  def revoke_holder(session: AsyncSession, workspace_id: uuid.UUID, token: AgentToken, *, reason: str)
  def _watermark_refs(session: AsyncSession, workspace_id: Any, ref: str, label: str)
  def transfer_responsibilities(session: AsyncSession, principal: Principal, request: TransferResponsibilitiesRequest)
### AgentGraph/app/services/quality.py
  def enforced()
  def _is_repo_scope(scope_key: str)
  def _json(value: Any)
  def _task(session: AsyncSession, principal: Principal, ref: str, *, for_update: bool=False)
  def _claim(session: AsyncSession, principal: Principal, claim_id: Any, *, for_update: bool=False)
  def _executors(task: CollabTask)
  def configured_tier(session: AsyncSession, workspace_id: Any, scope_key: str)
  def circuit(session: AsyncSession, workspace_id: Any, scope_key: str)
  def domain(session: AsyncSession, workspace_id: Any, scope_key: str)
  def _recent_agents(session: AsyncSession, workspace_id: Any, scope_key: str)
  def _notify(session: AsyncSession, workspace_id: Any, event_type: str, kind: str, *, scope_key: str, targets: list[str], details: dict[str, Any], task_key: str | None=None)
  def _open_circuit(session: AsyncSession, workspace_id: Any, scope_key: str, reason: str)
  def _count_failure(session: AsyncSession, workspace_id: Any, scope_key: str, reason: str)
  def _reset_failures(session: AsyncSession, workspace_id: Any, scope_key: str)
  def _acceptance_complete(task: CollabTask, items: Any)
  def gate_status(session: AsyncSession, workspace_id: Any, *, task: CollabTask | None=None, claim_id: Any=None)
  def missing_gates(status: dict[str, dict[str, Any]], required: tuple[str, ...], *, runner_acceptance: bool=False)
  def record_gate_result(session: AsyncSession, principal: Principal, request: GateResultRequest)
  def _hash(value: Any)
  def update_acceptance(session: AsyncSession, principal: Principal, ref: str, request: AcceptanceUpdateRequest)
  def audit_view(session: AsyncSession, principal: Principal, ref: str)
  def before_review(session: AsyncSession, principal: Principal, task: CollabTask, request: TaskReviewRequest, agent: str)
  def after_review(session: AsyncSession, principal: Principal, task: CollabTask, request: TaskReviewRequest, context: dict[str, Any])
  def check_auto_release(session: AsyncSession, principal: Principal, task: CollabTask)
  def _artifact_bytes(session: AsyncSession, task: CollabTask)
  def _sampled(task_id: Any, rate: float)
  def _sample(session: AsyncSession, workspace_id: Any, task_id: Any, reason: str, assigned_to: str)
  def released(session: AsyncSession, workspace_id: Any, task: CollabTask, *, path: str, info: dict[str, Any] | None=None)
  def escape_rate(session: AsyncSession, workspace_id: Any, scope_key: str, days: int)
  def _insert_escape(session: AsyncSession, workspace_id: Any, *, source: str, severity: str, description: str, created_by: str, task_id: Any=None, claim_id: Any=None, scope_key: str | None=None, root_gate: str | None=None)
  def _maybe_demote(session: AsyncSession, workspace_id: Any, scope_key: str)
  def create_escape(session: AsyncSession, principal: Principal, request: EscapeCreateRequest)
  def get_escape(session: AsyncSession, principal: Principal, escape_id: Any)
  def resolve_escape(session: AsyncSession, principal: Principal, escape_id: uuid.UUID, request: EscapeResolveRequest)
  def _row(row: Any)
  def list_samples(session: AsyncSession, principal: Principal, *, assigned: str | None, verdict: str | None, limit: int=100)
  def claim_sample(session: AsyncSession, principal: Principal, sample_id: uuid.UUID)
  def sample_verdict(session: AsyncSession, principal: Principal, sample_id: uuid.UUID, request: SampleVerdictRequest)
  def claim_scope(external_ref: str, related_doc_key: str | None)
  def _claim_view(session: AsyncSession, workspace_id: Any, claim: dict[str, Any])
  def report_claim(session: AsyncSession, principal: Principal, request: ChangeClaimRequest)
  def verify_claim(session: AsyncSession, principal: Principal, claim_id: uuid.UUID, request: ChangeClaimVerifyRequest)
  def set_tier(session: AsyncSession, principal: Principal, request: AutonomyTierRequest)
  def promotion_check(session: AsyncSession, workspace_id: Any, scope_key: str)
  def close_circuit(session: AsyncSession, principal: Principal, scope_key: str, note: str)
  def create_poison(session: AsyncSession, principal: Principal, request: PoisonTaskRequest)
  def list_poison(session: AsyncSession, principal: Principal)
  def report(session: AsyncSession, principal: Principal, window: str)
### AgentGraph/app/auth.py
  class RoleScope
  class AuthKind
  class Principal
    def own_names(self)
    def tenant_id(self)
    def tenant_scope(self)
    def is_commander(self)
    def is_super_admin(self)
    def is_human(self)
  def hash_api_key(raw_key: str)
  def generate_api_key()
  def generate_agent_key()
  def generate_human_key()
  def extract_bearer_token(request: Request)
  def extract_admin_token(request: Request)
  def _resolve_principal(session: AsyncSession, raw_key: str, *, agent_hint: str | None=None)
  def _revoked(token: AgentToken, ancestor: str)
  def _expired(token: AgentToken, expired: AgentToken)
  def meet(first: list[str], second: list[str])
  def _agent_principal(session: AsyncSession, workspace: Workspace, token: AgentToken, now: datetime)
  def resolve_principal(session: AsyncSession, raw_key: str, *, agent_hint: str | None=None)
  def is_write_request(method: str, path: str)
  def ensure_tenant_usable(principal: Principal, *, write: bool, path: str='')
  def get_principal(request: Request, session: AsyncSession=Depends(get_db))
  def get_human_principal(principal: Principal=Depends(get_principal))
  def get_human_writer(principal: Principal=Depends(get_principal))
  def get_human_account(principal: Principal=Depends(get_principal))
  def get_current_workspace(principal: Principal=Depends(get_principal))
  def principal_for_admin(workspace: Workspace)
  def principal_for_human(user: HumanUser, workspace: Workspace)
  def require_admin(request: Request)
  def capability_role(principal: Principal)
  def has_capability(principal: Principal, capability: str)
  def require_capability(principal: Principal, capability: str)
  def scopes_for_role(role: RoleScope)
  def effective_scopes(principal: Principal)
  def role_permissions(principal: Principal)
  def effective_permissions(principal: Principal)
  def can(principal: Principal, permission: str)
  def require_permission(principal: Principal, permission: str)
  def is_manager(principal: Principal)
  def effective_agent(principal: Principal, provided: str | None)
  def ensure_namespace(principal: Principal, namespace: str, *, write: bool=False)
  def ensure_not_human_locked(principal: Principal, resource: str, locked: bool, note: str | None=None)
  def ensure_task_role(principal: Principal, assigned_role: str | None)
  def session_batch_id(value: uuid.UUID | None)
  def utcnow()
### AgentGraph/app/models.py
  def utcnow()
  class AgentRole
  class TaskStatus
  class LinkType
  def _enum_values(members: type[enum.Enum])
  class Workspace
  class Tenant
  class Membership
  class AgentToken
  class SharedDocument
  class ContextSlot
  class CollabTask
  class WorkspaceLink
  class TimelineStream
  class StateAuditDiff
  class DocumentVersion
  class WorkspaceEvent
  class EventSubscription
  class DocumentChunk
  class DocumentProposal
  class ProposalVote
  class PathPolicy
  class HumanUser
  class HumanSession
  class TaintEdge
  class ExternalAction
  class ExternalActionAttempt
  class WorkspaceBudget
  class UsageLedger
  class RateLimitWindow
  class WebhookRule
  class WebhookDelivery
  class AliasRedirect
  class Tombstone
  class NamespaceRecord
  class DocumentRef
  class GrantSnapshot
  class ConstitutionAck
  class AccessAudit
  class Lease
  class DocumentIndex
  class Blob
  class DocumentBlob
  class BlobUpload
  class BlobUploadPart
  class CleanupGrant
  class ShareLink
  class ShareLinkAccess
  class Conversation
  class ConversationMember
  class Message
  class MessageDelivery
  class CommanderGrant
### AgentGraph/app/main.py
  def _is_private_path(path: str)
  def lifespan(app: FastAPI)
  def security_headers(request: Request, call_next)
  def _sitemap_lastmod()
  def robots_txt()
  def sitemap_xml()
  def llms_txt()
  def llms_full_txt()
  def og_image()
  def favicon()
  def favicon_png()
  def apple_touch_icon()
  def api_error_handler(request: Request, exc: ApiError)
  def validation_error_handler(request: Request, exc: RequestValidationError)
  def http_error_handler(request: Request, exc: StarletteHTTPException)
  def overload_handler(request: Request, exc: SQLAlchemyTimeoutError)
  def database_error_handler(request: Request, exc: SQLAlchemyError)
  def unhandled_error_handler(request: Request, exc: Exception)
  def _require_docs_auth(credentials: HTTPBasicCredentials | None=Depends(_docs_security))
  def _current_release()
  def root(request: Request)
  def _wants_json(accept: str)
  def root_zh(request: Request)
  def demo_redirect()
  def demo_page()
  def healthz()
  def readyz()
### AgentGraph/app/cli.py
  def _run(args: argparse.Namespace)
  def build_parser()
  def main()
### veynod_kernel/app/engine/kernel_service_impl.py
  def _now()
  def _leaf(symbol_path: str)
  class PostgresKernelEngineService
    def __init__(self, session: AsyncSession)
    def from_session_dep(cls, session: Annotated[AsyncSession, Depends(get_db_session)])
    def _default_workspace_id(self)
    def _workspace(self, workspace_id: UUID)
    def verify_active_lease(self, task_id: UUID, lease_token: str, worker_id: str)
    def lease_next_disjoint_task(self, req: PullTaskBundleRequest, worker_id: str)
    def _build_bundle(self, task: DagTask, workspace: Workspace, raw_token: str, max_context_tokens: int)
    def _view(self, artifact: CodeArtifact, content: str)
    def _signature_from_head(self, artifact: CodeArtifact, repo: GitRepository, rev: str='HEAD')
    def _current_code(self, artifact: CodeArtifact, repo: GitRepository, *, for_test: bool, rev: str='HEAD')
    def verify_and_commit_patch(self, lease: ActiveLeaseContext, req: SubmitPatchRequest, touched_files: set[str])
    def execute_graph_split(self, lease: ActiveLeaseContext, req: RequestTaskSplitRequest)
    def escalate_and_propagate_stale(self, lease: ActiveLeaseContext, req: EscalateContractDefectRequest)
    def inject_prerequisite_node(self, lease: ActiveLeaseContext, req: InjectDependencyTaskRequest)
    def lookup_symbol_signatures(self, req: QuerySymbolSignatureRequest)
  def _estimate_tokens(text: str | None)
  def _fit_bundle(bundle: TaskBundleResponse, max_context_tokens: int)
### veynod_kernel/app/engine/scheduler.py
  class AdmissionDenied
    def __init__(self, retry_after: int, reason: str, queue_depth: dict[str, int])
  class DisjointWriteSetScheduler
    def __init__(self, session: AsyncSession, event_bus: EventBus)
    def claim_next_orthogonal_task(self, workspace_id: UUID, runner_id: str, supported_task_types: list[str], lease_token_hash: str, lease_seconds: int=600)
    def _reserve_admission(self, workspace_id: UUID, lease_seconds: int)
    def _bind_permit_tx(self, permit_id: UUID, task_id: str, lease_seconds: int)
    def _release_permit(self, permit_id: UUID)
    def _claim_after_admission(self, workspace_id: UUID, runner_id: str, supported_task_types: list[str], lease_token_hash: str, lease_seconds: int=600, permit_id: UUID | None=None)
    def _claim_with_rust_core(self, workspace_id: UUID, runner_id: str, supported_task_types: list[str], lease_token_hash: str, lease_seconds: int, initial_task_id: str | None=None, permit_id: UUID | None=None)
    def _admission_depth(self, workspace_id: UUID)
    def _update_selected_task(self, task_id: UUID, workspace_id: UUID, runner_id: str, lease_token_hash: str, lease_seconds: int)
    def _claim_with_sql(self, workspace_id: UUID, runner_id: str, supported_task_types: list[str], lease_token_hash: str, lease_seconds: int)
    def on_task_committed(self, workspace_id: UUID, completed_task_id: UUID, new_head_sha: str)
    def reap_expired_leases(self, workspace_id: UUID)
    def reap_stuck_verifications(self, workspace_id: UUID, older_than_seconds: int)
    def extend_lease(self, task_id: UUID, lease_token_hash: str, extend_seconds: int)
    def release_task(self, task_id: UUID, lease_token_hash: str, reason: str, *, retry_counted: bool=False)
### veynod_kernel/app/engine/scheduler_core.py
  def _task_tuple(task: DagTask)
  class SchedulerCoreManager
    def __init__(self)
    def _ensure_core(self)
    def available(self)
    def reset(self)
    def stats(self, workspace_id: UUID)
    def ready_count(self, workspace_id: UUID)
    def acquire_authority(self, session: AsyncSession, workspace_id: UUID)
    def _acquire_authority_locked(self, session: AsyncSession, workspace_id: UUID)
    def ensure_loaded(self, session: AsyncSession, workspace_id: UUID)
    def reload_workspace(self, session: AsyncSession, workspace_id: UUID)
    def sync_tasks(self, session: AsyncSession, workspace_id: UUID, task_ids: list[UUID])
    def claim(self, workspace_id: UUID, supported_types: list[str], runner_id: str, lease_token_hash: str, claim_version: int)
    def revert(self, workspace_id: UUID, task_id: UUID, lease_token_hash: str)
    def warmup(self)
### veynod_kernel/app/engine/task_commands.py
  class TaskCommandError
  class TaskNotFound
  class TaskCancelConflict
  def _cancel_side_effects(session: AsyncSession, task_ids: list[UUID], reason: str)
  def cancel_task(session: AsyncSession, task_id: UUID, *, reason: str, actor: str='admin')
### veynod_kernel/app/engine/mutator.py
  class SubTaskNodeSpec
  class SplitResult
  class GraphMutator
    def __init__(self, session: AsyncSession, event_bus: EventBus)
    def split_task_into_subdag(self, workspace_id: UUID, parent_task_id: UUID, sub_nodes: list[SubTaskNodeSpec])
    def escalate_and_propagate_blast_radius(self, workspace_id: UUID, reporting_task_id: UUID, defective_artifact_id: UUID, affected_fields: list[str], reason: str, proposed_signature: str, revise_write_locks: list[str], revise_ancestor_locks: list[str])
    def inject_prerequisite_node(self, workspace_id: UUID, current_task_id: UUID, prerequisite_title: str, prerequisite_spec: str, prerequisite_target_ids: list[UUID], prerequisite_write_locks: list[str], prerequisite_ancestor_locks: list[str], prerequisite_priority: int=500)
### veynod_kernel/app/engine/lock_algebra.py
  class LockSet
    def conflicts_with(self, other: 'LockSet')
  class LockAlgebra
    def normalize(symbol_path: str)
    def conflicts(cls, path_a: str, path_b: str)
    def is_covered_by_write_set(cls, target_path: str, allowed_write_set: list[str])
    def extract_file_level_whitelist(cls, write_set: list[str])
    def expand_write_set(cls, raw_paths: Iterable[str])
    def lock_set(cls, raw_paths: Iterable[str])
    def is_sub_write_set(cls, parent_write_locks: Iterable[str], child_raw_paths: Iterable[str])
    def promote_new_files(cls, raw_paths: Iterable[str], existing_files: set[str])
    def file_paths_for_locks(locks: Iterable[str])
    def symbols_conflict(left: Iterable[str], right: Iterable[str])
  def _plan_claimable_python(tasks: list[dict[str, Any]])
  def plan_claimable(tasks: list[dict[str, Any]])
### veynod_kernel/app/engine/rust_bridge.py
  def available()
  def core()
  def version()
  def describe()
  def engine_name()
  def normalize(raw: str)
  def expand_write_set(paths: list[str])
  def is_sub_write_set(parent: list[str], child: list[str])
  def is_covered_by_write_set(target: str, allowed: list[str])
  def conflicts(left: str, right: str)
  def promote_new_files(paths: list[str], existing: set[str])
  def file_paths_for_locks(locks: list[str])
  def scan_workspace_repo(repo_path: str, include_tests: bool=False)
  def plan_claimable(tasks: list[dict[str, Any]])
  def run_command(command: str, cwd: str, timeout_seconds: float, extra_env: dict[str, str] | None, output_limit: int)
### veynod_kernel/app/services/verification_gate.py
  class MergeFenceLost
  def assert_task_mergeable(session: AsyncSession, task: DagTask, expected_lease_hash: str | None)
  def _workspace_merge_lock(workspace_id)
  class _AttemptResult
  class _MergeOutcome
  def _now()
  def _delta(seconds: int)
  def _quote(path: str)
  def _truncate(output: str)
  def _elapsed_ms(started: float)
  def _gate(stage: GateStage, passed: bool, code: int, diagnostics: str='', duration_ms: int=0)
  def _safe_path(worktree: Path, relative: str)
  def _source_hash(source: str | None)
  def _symbol_source(slicer: Slicer, source: str, symbol_path: str)
  def sensitive_file_unchanged(slicer: Slicer, file_path: str, base_content: str, head_content: str, symbol_paths: list[str])
  class VerifiedPatch
  class GateVerification
  class DeterministicVerificationGate
    def __init__(self, session: AsyncSession, slicer: Slicer, scheduler: DisjointWriteSetScheduler, event_bus: EventBus)
    def verify_and_merge_patch(self, task: DagTask, workspace: Workspace, git_diff: str, touched_symbols: Sequence[str], lease_token_hash: str, *, before_merge: Callable[[], Awaitable[None]] | None=None)
    def verify_patch(self, task: DagTask, workspace: Workspace, git_diff: str, touched_symbols: Sequence[str], lease_token_hash: str)
    def merge_verified(self, task: DagTask, workspace: Workspace, verified: 'VerifiedPatch', *, before_merge: Callable[[], Awaitable[None]] | None=None)
    def _run_attempt(self, repo: GitRepository, task: DagTask, workspace: Workspace, base_sha: str, git_diff: str, touched_symbols: Sequence[str])
    def _register_commit_snapshots(self, repo: GitRepository, worktree: Path, base_sha: str, new_sha: str, changed_files: list[str])
    def _sensitive_metadata(self, task: DagTask)
    def _precompute_sensitive_snapshots(self, repo: GitRepository, base_sha: str, paths: list[str])
    def _check_ast_scope(self, repo: GitRepository, worktree: Path, base_sha: str, task: DagTask, changed_files: Sequence[str])
    def _check_contracts(self, worktree: Path, task: DagTask, workspace: Workspace, changed_files: list[str])
    def _static_checks(self, worktree: Path, changed_files: list[str])
    def _test_files(self, task: DagTask)
    def _run_tests(self, worktree: Path, task: DagTask)
    def _merge(self, repo: GitRepository, task: DagTask, workspace: Workspace, base_sha: str, new_sha: str, changed_files: list[str], gate_results: list[GateCheckResult], *, sensitive_by_file: dict[str, list[CodeArtifact]] | None=None, lock_wait_ms: int=0, gate_pool_wait_ms: int=0, before_merge: Callable[[], Awaitable[None]] | None=None)
    def _tree_merge(self, repo: GitRepository, task: DagTask, workspace: Workspace, base_sha: str, current_head: str, task_commit: str, changed_files: list[str], gate_results: list[GateCheckResult], *, sensitive_by_file: dict[str, list[CodeArtifact]] | None=None, timings: dict[str, int] | None=None, started: float | None=None, before_merge: Callable[[], Awaitable[None]] | None=None)
    def _finalize_merge(self, repo: GitRepository, task: DagTask, workspace: Workspace, previous_head: str, new_sha: str, changed_files: list[str], gate_results: list[GateCheckResult], merge_mode: str, *, timings: dict[str, int] | None=None, started: float | None=None)
    def _reject(self, task: DagTask, gate_results: list[GateCheckResult], error_traceback: str)
### veynod_kernel/app/services/gate_worker.py
  class WorkLeaseLost
  class GateWorker
    def __init__(self, worker_id: str | None=None)
    def stop(self)
    def run_forever(self)
    def claim(self)
    def renew(self, record: Mapping[str, Any])
    def fence(self, record: Mapping[str, Any])
    def process(self, record: Mapping[str, Any])
    def _watch_cancellation(self, record: Mapping[str, Any], gate_task: asyncio.Task, cancel_signal: asyncio.Event)
    def _heartbeat_loop(self, record: Mapping[str, Any], lost: asyncio.Event)
    def _process_with_gate(self, record: Mapping[str, Any], lost: asyncio.Event)
    def _reject_merge_intent(self, record: Mapping[str, Any], response)
    def _upsert_merge_intent(self, *, record: Mapping[str, Any], task_id, workspace_id, lease_hash: str, verified)
    def _wait_merge_intent(self, record: Mapping[str, Any], merge_id, lost: asyncio.Event)
    def _finalize(self, record: Mapping[str, Any], *, status: str, task_status: str | None=None, result_commit_sha: str | None=None, merge_mode: str | None=None, gate_results: list[dict[str, Any]] | None=None, merge_timing: dict[str, Any] | None=None, unlocked_downstream_task_ids: list[Any] | None=None, error_code: str | None=None, error_detail: str | None=None)
    def _requeue(self, record: Mapping[str, Any], detail: str, code: str)
    def _handle_transient(self, record: Mapping[str, Any], code: str)
    def release_task(self, task_id, lease_hash: str, reason: str)
### veynod_kernel/app/services/merge_coordinator.py
  class MergeCoordinator
    def __init__(self, coordinator_id: str | None=None)
    def stop(self)
    def run_forever(self)
    def acquire(self, workspace_id: uuid.UUID)
    def renew(self, workspace_id: uuid.UUID, token: int)
    def release(self, workspace_id: uuid.UUID, token: int)
    def _workspace_ids(self)
    def process_all(self)
    def claim(self, workspace_id: uuid.UUID, token: int)
    def process_intent(self, intent: Mapping[str, Any], token: int)
    def _dependency_changed(self, task: DagTask, changed_files: list[str])
    def _finalize_already_merged(self, intent: Mapping[str, Any], workspace: Workspace, git_head: str)
    def _record_conflict(self, workspace_id: uuid.UUID, task_id: uuid.UUID, merge_id: uuid.UUID, changed_files: list[str])
    def _revalidate(self, intent: Mapping[str, Any], reason: str)
    def _finish_intent(self, merge_id: uuid.UUID, token: int | None, status: str, error_code: str | None, error_detail: str | None, *, merged_commit_sha: str | None=None, merge_mode: str | None=None, merge_timing: dict[str, Any] | None=None, unlocked: list[str] | None=None, task_status: str | None=None)
    def reconcile_all(self)
    def reconcile_workspace(self, workspace_id: uuid.UUID)
### veynod_kernel/app/services/gate_sandbox.py
  def sanitized_environment(cwd: Path)
  def _tail(text: str)
  def _posix_limits(timeout: float)
  def _kill_tree(proc: asyncio.subprocess.Process)
  def _run_local(command: str, cwd: Path, timeout: float)
  def _run_docker(command: str, cwd: Path, timeout: float)
  def run_sandboxed_command(command: str, cwd: Path, *, timeout: float | None=None)
### veynod_kernel/app/services/git_ops.py
  class GitError
    def __init__(self, command: str, returncode: int, output: str)
  class RepositoryNotUsable
  class CommandResult
    def ok(self)
  def _communicate(proc: asyncio.subprocess.Process, timeout: float)
  def run_git(args: list[str], cwd: str | Path, *, timeout: float=120.0)
  def run_git_checked(args: list[str], cwd: str | Path, *, timeout: float=120.0)
  def run_shell(command: str, cwd: str | Path, *, timeout: float | None=None, extra_env: dict[str, str] | None=None)
  class _GitBatchReader
    def __init__(self, repo_path: str | Path)
    def _ensure_process(self)
    def close(self)
    def read(self, specs: list[str], timeout: float=120.0)
  class _GitBatchPool
    def __init__(self, repo_path: str | Path, size: int=4)
    def next(self)
    def close(self)
  def _batch_pool(repo_path: str | Path)
  def close_git_batch_pools()
  class GitRepository
    def __init__(self, repo_path: str | Path, main_branch: str='main')
    def head_sha(self)
    def resolve_ref(self, ref: str)
    def is_bare(self)
    def current_branch(self)
    def checked_out_branches(self)
    def validate_usable(self, allow_nonbare: bool | None=None)
    def list_files(self, rev: str='HEAD')
    def read_file(self, path: str, rev: str='HEAD')
    def read_files_batch(self, paths: list[str] | set[str], rev: str='HEAD')
    def path_exists(self, path: str, rev: str='HEAD')
    def diff_at_head(self, worktree: Path)
    def changed_files_staged(self, worktree: Path, base: str='HEAD')
    def commit_exists(self, sha: str)
    def is_ancestor(self, ancestor: str, descendant: str)
    def changed_files_between(self, old_sha: str, new_sha: str)
    def merge_tree(self, ours: str, theirs: str)
    def commit_tree(self, tree_sha: str, parent_sha: str, message: str)
    def add_worktree(self, destination: Path, rev: str)
    def reset_worktree(self, destination: Path, rev: str)
    def configure_for_swarm(self)
    def remove_worktree(self, destination: Path)
    def apply_patch(self, worktree: Path, patch_file: Path)
    def stage_all(self, worktree: Path)
    def stage_paths(self, worktree: Path, paths: list[str])
    def commit(self, worktree: Path, message: str, *, author_name: str='Veynod Kernel', author_email: str='kernel@veynod.local')
    def update_ref(self, new_sha: str, old_sha: str | None=None)
### veynod_kernel/app/services/event_bus.py
  def _json_default(value: Any)
  def encode_payload(payload: dict[str, Any])
  class EventBus
    def __init__(self, session: AsyncSession)
    def publish(self, workspace_id: UUID, event_type: str, payload: dict[str, Any], *, correlation_id: str | None=None, causation_id: str | None=None, producer: str='kernel')
    def latest_seq(self, workspace_id: UUID)
    def fetch_after(self, workspace_id: UUID, after_seq: int, limit: int=200)
    def fetch_global_after(self, after_seq: int, limit: int=5000)
  def sse_frame(event: KernelEvent)
### veynod_kernel/app/services/event_hub.py
  class EventBufferOverflow
  class EventHub
    def __init__(self)
    def start(self)
    def stop(self)
    def _poll_loop(self)
    def _fan_out(self, event: KernelEvent)
    def _overflow(queue: asyncio.Queue)
    def latest_seq(self, workspace_id: UUID)
    def earliest_seq(self, workspace_id: UUID)
    def catch_up(self, workspace_id: UUID, cursor: int, limit: int=1000)
    def subscribe(self, workspace_id: UUID, cursor: int)
  def get_event_hub()
### veynod_kernel/app/services/outbox.py
  class OutboxDispatcher
    def __init__(self)
    def start(self)
    def stop(self)
    def _run(self)
    def _dispatch_once(self)
### veynod_kernel/app/services/submission_service.py
  class SubmissionConflict
    def __init__(self, code: str, detail: str, existing: Mapping[str, Any] | None=None)
  class SubmissionRejected
    def __init__(self, code: str, detail: str, status_code: int=400)
  def patch_sha256(git_diff: str)
  def derive_idempotency_key(*, task_id: UUID | str, runner_id: str, lease_token_hash: str, patch_digest: str)
  def normalize_idempotency_key(raw: str | None, *, task_id: UUID | str, runner_id: str, lease_token_hash: str, patch_digest: str)
  def _json_load(value: Any, default: Any)
  def _iso(value: Any)
  def _status_payload(row: Mapping[str, Any])
  class SubmissionService
    def __init__(self)
    def accept(self, session: AsyncSession, *, runner_id: str, task_id: UUID, lease_token_hash: str, idempotency_key: str | None, git_diff: str, touched_symbols: list[str], local_test_passed: bool, existing: dict[str, Any] | None=None)
    def _insert(self, session: AsyncSession, params: Mapping[str, Any])
    def _binding_matches(row: Mapping[str, Any], *, runner_id: str, task_id: UUID, lease_token_hash: str, patch_sha256: str)
    def _find_by_key(self, session: AsyncSession, key: str)
    def _find_by_binding(self, session: AsyncSession, *, task_id: UUID, lease_token_hash: str, patch_sha256: str)
    def find_existing(self, session: AsyncSession, *, runner_id: str, task_id: UUID, lease_token_hash: str, patch_sha256: str, idempotency_key: str | None)
    def get_status(self, session: AsyncSession, submission_id: UUID, runner_id: str)
    def find_by_key(self, session: AsyncSession, idempotency_key: str, runner_id: str)
  def get_submission_service()
### veynod_kernel/app/services/cell_registry.py
  def list_cells(session: AsyncSession)
  def create_cell(session: AsyncSession, *, name: str, endpoint: str | None=None, capacity: dict[str, Any] | None=None)
  def list_repositories(session: AsyncSession)
  def move_repository(session: AsyncSession, workspace_id: UUID, *, cell_id: UUID, expected_epoch: int | None=None)
  def cell_workspace_ids(session: AsyncSession, cell_id: UUID)
  def assert_workspace_in_current_cell(session: AsyncSession, workspace_id: UUID)
  def cell_filter_sql(alias: str='p')
  def _json(value: Any)
### veynod_kernel/app/services/task_factory.py
  def insert_task(session: AsyncSession, *, workspace_id: UUID, title: str, spec_prompt: str, task_type: str, read_artifact_ids: Iterable[UUID]=(), target_artifact_ids: Iterable[UUID]=(), test_artifact_ids: Iterable[UUID]=(), write_locks: Iterable[str]=(), ancestor_locks: Iterable[str]=(), depends_on_task_ids: Iterable[UUID]=(), priority: int=100, max_retries: int=3, parent_task_id: UUID | None=None)
### veynod_kernel/app/services/edge_rebuild.py
  class EdgeRebuildJob
  class EdgeRebuildService
    def __init__(self)
    def enabled(self)
    def start(self)
    def stop(self)
    def enqueue(self, job: EdgeRebuildJob, session: AsyncSession | None=None)
    def _run(self)
    def _claim(self)
    def _process(self, job: dict[str, Any])
    def _retry_or_fail(self, job: dict[str, Any], error: str)
### agent_runtime/states.py
  class RunState
  def validate_transition(current: str | RunState, target: str | RunState)
### agent_runtime/store.py
  def _split_sql(sql: str)
  def _json(value: Any)
  def _loads(value: Any, default: Any)
  def _token_hash(token: str)
  class RunStore
    def __init__(self, database_url: str, schema: str='agent_runtime')
    def close(self)
    def init_schema(self, reset: bool=False)
    def _publish(self, session: AsyncSession, run_id: UUID, event_type: str, payload: dict[str, Any])
    def _idempotent(self, session: AsyncSession, key: str, run_id: UUID | None, command: str)
    def _remember(self, session: AsyncSession, key: str, run_id: UUID, command: str, result: dict[str, Any])
    def _run_dict(row: Any)
    def create_run(self, *, tenant_id: UUID | str, workspace_id: UUID | str, product_task_id: str | None=None, goal: str | None=None, workflow_id: UUID | str | None=None, workflow_version: int | None=None, budget: dict[str, Any] | None=None, correlation_id: str | None=None, idempotency_key: str | None=None, parent_run_id: UUID | str | None=None, delegation_key: str | None=None)
    def get_run(self, run_id: UUID | str)
    def list_events(self, run_id: UUID | str, after_seq: int=0, limit: int=500)
    def latest_event_seq(self)
    def list_events_global(self, after_seq: int=0, limit: int=500)
    def list_checkpoints(self, run_id: UUID | str)
    def list_steps(self, run_id: UUID | str, limit: int=50)
    def list_children(self, parent_run_id: UUID | str)
    def get_child_by_delegation(self, parent_run_id: UUID | str, delegation_key: str)
    def list_runs_by_status(self, status: str, limit: int=200)
    def start_run(self, run_id: UUID | str)
    def transition(self, run_id: UUID | str, target: str | RunState, *, expected_version: int | None=None, reason: str='', actor: str='runtime', idempotency_key: str | None=None)
    def checkpoint(self, run_id: UUID | str, state: dict[str, Any], *, idempotency_key: str | None=None)
    def next_step_no(self, run_id: UUID | str)
    def record_step(self, run_id: UUID | str, step_no: int, kind: str, status: str, payload: dict[str, Any] | None=None, *, idempotency_key: str | None=None)
    def begin_invocation(self, run_id: UUID | str, *, kind: str, name: str, idempotency_key: str, request: dict[str, Any] | None=None, step_id: UUID | str | None=None)
    def finish_invocation(self, invocation_id: UUID | str, *, status: str, result: dict[str, Any] | None=None, error: str | None=None, usage: dict[str, Any] | None=None)
    def add_usage(self, run_id: UUID | str, **deltas: float)
    def tenant_usage(self, tenant_id: UUID | str)
    def invocation_health(self, window_minutes: int=15)
    def status_counts(self)
    def claim_run(self, worker_id: str, lease_seconds: int=60)
    def assert_lease(self, run_id: UUID | str, token: str)
    def renew_lease(self, run_id: UUID | str, token: str, lease_seconds: int=60)
    def release_lease(self, run_id: UUID | str, token: str)
    def _invocation_dict(row: Any)
### agent_runtime/loop.py
  class ModelGateway
    def decide(self, run: dict[str, Any], context: dict[str, Any])
  class ToolRuntime
    def execute(self, name: str, arguments: dict[str, Any], idempotency_key: str)
  class ModelAction
  class ToolResult
  def budget_exceeded(run: dict[str, Any])
  class LoopController
    def __init__(self, store: RunStore, model: ModelGateway, tools: ToolRuntime, *, guard: Callable[[], Awaitable[None]] | None=None, context_builder: ContextBuilder | None=None, quota: QuotaPolicy | None=None)
    def _check_guard(self)
    def step(self, run_id: str)
    def _checkpoint(self, run_id: str, state: dict[str, Any])
### agent_runtime/api.py
  class RunCreate
  class TransitionRequest
  class CheckpointRequest
  class DelegateRequest
  class ReasonRequest
  def create_app(database_url: str, api_key: str, *, schema: str='agent_runtime', reset_schema: bool=False, quota: QuotaPolicy | None=None)
  def _env_app()
### agent_runtime/gateway.py
  class ModelGatewayError
  class ModelGatewayConfig
  class HTTPModelGateway
    def __init__(self, config: ModelGatewayConfig, *, transport: httpx.AsyncBaseTransport | None=None)
    def close(self)
    def decide(self, run: dict[str, Any], context: dict[str, Any])
    def _post(self, payload: dict[str, Any])
  class ToolGatewayConfig
  class HTTPToolRuntime
    def __init__(self, config: ToolGatewayConfig, *, transport: httpx.AsyncBaseTransport | None=None)
    def close(self)
    def execute(self, name: str, arguments: dict[str, Any], idempotency_key: str)
  def _parse_action(content: str)
### agent_runtime/context.py
  def estimate_tokens(value: Any)
  class ContextSection
    def to_dict(self)
  class ContextBuilder
    def __init__(self, token_budget: int=8000)
    def build(self, *, run: dict[str, Any], checkpoint: dict[str, Any] | None=None, steps: list[dict[str, Any]] | None=None, extra: list[ContextSection] | None=None, token_budget: int | None=None)
### agent_runtime/delegation.py
  class DelegationManager
    def __init__(self, store: RunStore)
    def delegate(self, parent_run_id: str, *, product_task_id: str | None=None, budget: dict[str, Any] | None=None, idempotency_key: str | None=None)
    def reconcile(self, parent_run_id: str)
    def reconcile_all(self)
    def collect(self, parent_run_id: str)
### agent_runtime/quota.py
  class QuotaPolicy
    def exceeded(self, usage: dict[str, Any])
    def from_env(cls, env: dict[str, str] | None=None)
### agent_runtime/router.py
  class ModelRoute
  class RoutingModelGateway
    def __init__(self, routes: list[ModelRoute])
    def decide(self, run: dict[str, Any], context: dict[str, Any])
### agent_runtime/pool.py
  class RuntimeWorkerPool
    def __init__(self, store: RunStore, model: ModelGateway, tools: ToolRuntime, *, workers: int=2, lease_seconds: int=60, poll_seconds: float=1.0, context_builder: ContextBuilder | None=None, quota: QuotaPolicy | None=None)
    def start(self)
    def _supervise(self, index: int)
    def stop(self)
    def snapshot(self)
### agent_runtime/worker.py
  class RuntimeWorker
    def __init__(self, store: RunStore, model: ModelGateway, tools: ToolRuntime, *, worker_id: str | None=None, lease_seconds: int=60, poll_seconds: float=1.0, quota: QuotaPolicy | None=None, context_builder: ContextBuilder | None=None)
    def stop(self)
    def run_once(self)
    def _heartbeat(self, run_id: str, token: str)
    def run_forever(self)
### agent_runtime/maintenance.py
  class ReconciliationLoop
    def __init__(self, store: RunStore, interval_seconds: float=30.0)
    def stop(self)
    def run_once(self)
    def run_forever(self)
### agent_runtime/worker_main.py
  def _metrics_server(port: int, pool: RuntimeWorkerPool)
  def _required(name: str)
  def main()
### scripts/runtime_supervisor.py
  def _metrics_ok(port: int, timeout: float=2.0)
  def _port_open(port: int)
  def main()
### scripts/rollback_drill.py
  def _dsn(db: str=DB_NAME)
  def _files()
  def _apply(files: list[Path])
  def _reset_db()
  def _ledger()
  def _table_exists(name: str)
  def _dump(path: Path)
  def _restore(path: Path)
  def main()
```

---

# 附录 C：迁移索引

## AgentGraph（head 3.71.0）

```text
v2_to_v3_collab.sql
v3_10_review_rounds.sql
v3_11_onboarding.sql
v3_12_budget.sql
v3_13_async_index.sql
v3_14_budget_enforce.sql
v3_15_leases.sql
v3_16_exports.sql
v3_17_events.sql
v3_18_onboarding_progress.sql
v3_19_agent_onboarding.sql
v3_20_durability_incidents.sql
v3_21_token_tree.sql
v3_22_members.sql
v3_23_collaboration.sql
v3_24_provenance_recall_budgets.sql
v3_25_logs_contention_situation.sql
v3_26_tenant_tree.sql
v3_27_phase3.sql
v3_28_phase3_r32.sql
v3_29_event_partitions.sql
v3_2_1_mcp_improvements.sql
v3_2_production.sql
v3_30_releases.sql
v3_31_token_delta.sql
v3_32_quality_control.sql
v3_33_poke_and_seed.sql
v3_34_growth.sql
v3_35_operator_privacy.sql
v3_36_oauth.sql
v3_37_poke_realtime.sql
v3_38_chunk_embed_hash.sql
v3_39_token_naming.sql
v3_3_agent_feedback.sql
v3_40_blobs_share_links.sql
v3_41_task_invariants.sql
v3_42_task_project_repo.sql
v3_43_lease_renew.sql
v3_44_super_admin.sql
v3_45_conversations.sql
v3_46_group_conversations.sql
v3_47_message_escalations.sql
v3_48_commander_grants.sql
v3_49_blob_tenants.sql
v3_4_security_hardening.sql
v3_50_presence_states.sql
v3_51_diagrams.sql
v3_52_diagram_automation.sql
v3_53_diagram_autonomy.sql
v3_54_deploy_gate.sql
v3_55_automation_modes.sql
v3_56_owner_acceptance.sql
v3_57_deploy_evidence.sql
v3_58_escalation_batches.sql
v3_59_probe_tokens.sql
v3_5_artifact_binding.sql
v3_60_runner_hosts.sql
v3_61_runner_placements.sql
v3_62_drop_owner_acceptance.sql
v3_63_loop_steps.sql
v3_64_cli_identities.sql
v3_65_host_report.sql
v3_66_diagram_policy.sql
v3_67_placement_per_step.sql
v3_68_cancelled_terminal.sql
v3_69_code_projection_cursor.sql
v3_6_failure_protocol.sql
v3_70_event_inbox.sql
v3_71_projects_metering.sql
v3_7_tenants.sql
v3_8_addresses.sql
v3_9_lineage.sql
v3_hardening.sql
```

## Kernel（v4_001–v4_014）

```text
v4_001_genesis.sql
v4_002_patch_submissions.sql
v4_003_gate_work_leases.sql
v4_004_merge_coordinator.sql
v4_005_admission_outbox_jobs.sql
v4_006_admission_permits.sql
v4_007_unlogged_admission_permits.sql
v4_008_active_lock_indexes.sql
v4_009_cancellation.sql
v4_010_token_workspace.sql
v4_011_event_delivery.sql
v4_012_cells_repositories.sql
v4_013_code_intelligence.sql
v4_014_delivery_records.sql
```

## Agent Runtime（001–007）

```text
001_init.sql
002_invocations_usage.sql
003_run_leases.sql
004_delegation.sql
005_delegation_key.sql
006_goal.sql
007_tenant_text.sql
```

---

# 附录 D：模块职责总表

> 每个 Python 模块的首行 docstring；空值表示该文件没有模块 docstring。完整类/方法见附录 B 或直接打开路径。

### AgentGraph/app

| Module | First docstring line |
|---|---|
| `AgentGraph/app/__init__.py` |  |
| `AgentGraph/app/actors.py` | Typed actor refs for responses (PRD AG-ID-06 point 3, API version 2026-11). |
| `AgentGraph/app/addresses.py` | Address grammar (PRD AG-ST-17 point 3): canonical ids, namespaces, doc keys. |
| `AgentGraph/app/agent_guide.py` | Agent-readable API guide generated from code constants (PRD AG-OB-03). |
| `AgentGraph/app/api/__init__.py` |  |
| `AgentGraph/app/api/admin_routes.py` | Human commander console API (admin-token protected). |
| `AgentGraph/app/api/agent_run_page.py` | The human console's Agent Runs page (static, session-token based). |
| `AgentGraph/app/api/agent_run_routes.py` | Product-task Agent Runtime routes (Phase 2K). |
| `AgentGraph/app/api/auth_routes.py` | Human account login, identity, and logout. |
| `AgentGraph/app/api/blob_routes.py` | Blob storage, attachments and cleanup-grant routes (v3.40). |
| `AgentGraph/app/api/code_page.py` | The human console's Code page. |
| `AgentGraph/app/api/code_routes.py` | Code delivery routes. |
| `AgentGraph/app/api/event_admin_routes.py` | Operator view of the durable cross-domain event inbox (Phase 3). |
| `AgentGraph/app/api/human_routes.py` | Workspace-scoped human console API using account sessions. |
| `AgentGraph/app/api/inbox_stream.py` | GET /v1/inbox/stream: pokes pushed to an online agent (TASK-72A59BD9). |
| `AgentGraph/app/api/oauth_routes.py` | OAuth 2.1 endpoints for MCP clients (v3.36); see app/services/oauth.py. |
| `AgentGraph/app/api/org_routes.py` | Organisation routes: members and roles (AG-ID-10), rights requests (AG-TC-07). |
| `AgentGraph/app/api/project_page.py` | Product console page for projects, execution graph and delivery. |
| `AgentGraph/app/api/project_routes.py` | Projects, execution graph, delivery and runtime observability (Phase 7). |
| `AgentGraph/app/api/public_docs.py` | Plain-HTML documentation fallback at /docs. |
| `AgentGraph/app/api/share_routes.py` | Public document share-link routes (v3.40). |
| `AgentGraph/app/api/stream_routes.py` | Replayable SSE over the transactional workspace_events outbox. |
| `AgentGraph/app/api/v1_routes.py` | Veynod tenant/agent API: tasks, state grid, documents, links, timeline. |
| `AgentGraph/app/api/wellknown_routes.py` | Public, unauthenticated keys and revocation checks (PRD AG-ID-08). |
| `AgentGraph/app/auth.py` | Authentication, scoped agent tokens, and the v3 capability matrix. |
| `AgentGraph/app/batch_memo.py` | Lookups the items of one batch write share. |
| `AgentGraph/app/capabilities.py` | What this deployment can do, for agents reading the root (retest finding 2). |
| `AgentGraph/app/cli.py` | Operator CLI: schema bootstrap, workspace provisioning, and inspection. |
| `AgentGraph/app/concurrency.py` | In-flight request limits. |
| `AgentGraph/app/config.py` | Application configuration for Veynod v3.9.0. |
| `AgentGraph/app/database.py` | Async SQLAlchemy engine, session factory, and schema bootstrap helpers. |
| `AgentGraph/app/demo_page.py` | Public read-only demo workspace page (growth acquisition). |
| `AgentGraph/app/docs_site.py` | Public documentation site content (single source of truth). |
| `AgentGraph/app/engine/__init__.py` |  |
| `AgentGraph/app/engine/blackboard.py` | Deterministic blackboard mechanics: leases, JSON Patch, section patching, |
| `AgentGraph/app/engine/chunking.py` | Markdown-aware chunking for hybrid retrieval and section-level rebase. |
| `AgentGraph/app/engine/embeddings.py` | Embedding providers: OpenAI, local Ollama, and a deterministic offline hash. |
| `AgentGraph/app/engine/formatter.py` | Human/agent views for documents, handoff briefs, and token-efficient output. |
| `AgentGraph/app/engine/merge.py` | Line-based three-way merge for non-overlapping document edits. |
| `AgentGraph/app/engine/notion.py` | Detect and convert Notion-exported HTML into safe Markdown. |
| `AgentGraph/app/error_registry.py` | Error-code registry: every client-facing code maps to a class and an action. |
| `AgentGraph/app/errors.py` | Domain errors and a consistent JSON error envelope. |
| `AgentGraph/app/events.py` | Single-process fan-out for PostgreSQL workspace_events. |
| `AgentGraph/app/ids.py` | Canonical object ids: ``<prefix>_<ULID>`` (PRD §2.1, AG-ST-17). |
| `AgentGraph/app/main.py` | Veynod FastAPI gateway: REST, admin API, MCP mount, health. |
| `AgentGraph/app/mcp_server/__init__.py` |  |
| `AgentGraph/app/mcp_server/__main__.py` | Entry point: python -m app.mcp_server (stdio transport). |
| `AgentGraph/app/mcp_server/server.py` | Native MCP server for the Veynod shared blackboard workspace. |
| `AgentGraph/app/middleware.py` | Pure-ASGI request logging that is safe for MCP streaming responses. |
| `AgentGraph/app/models.py` | ORM models for the Veynod shared blackboard workspace. |
| `AgentGraph/app/protocol_middleware.py` | Pure-ASGI failure-semantics protocol layer (PRD AG-ST-18). |
| `AgentGraph/app/root_page.py` | Human-facing service descriptor rendered at ``/``. |
| `AgentGraph/app/schemas.py` | Pydantic contracts for the Veynod shared blackboard API. |
| `AgentGraph/app/schemas_blobs.py` | Pydantic contracts for blob storage and public share links (v3.40). |
| `AgentGraph/app/seo_geo.py` | Public, machine-readable site context generated from published docs. |
| `AgentGraph/app/services/__init__.py` | v3 shared blackboard service layer. |
| `AgentGraph/app/services/access_log.py` | Per-token access log: who touched which workspace, when, and what they read. |
| `AgentGraph/app/services/accounts.py` | Human accounts, password sessions, and console identity. |
| `AgentGraph/app/services/addresses.py` | Address resolution, namespaces, references, rename and purge (PRD AG-ST-17). |
| `AgentGraph/app/services/agent_merge.py` | Merge duplicate directory identities into one stable agent_id. |
| `AgentGraph/app/services/agent_onboarding.py` | Agent onboarding over MCP: five protocol steps, role-aware, never a gate. |
| `AgentGraph/app/services/agent_runtime.py` | AgentGraph -> Veynod Agent Runtime adapter (Phase 2K). |
| `AgentGraph/app/services/alerts.py` | Platform alerts for the operator (restore drill failures, incident SLA checkpoints). |
| `AgentGraph/app/services/attestation.py` | Verifiable lineage across tenants (PRD AG-ID-08). |
| `AgentGraph/app/services/audit.py` | Session-batch audit queries and time-travel undo. |
| `AgentGraph/app/services/automation.py` | The owner's automation mode: manual, semi-automatic or full automatic (Loop v1). |
| `AgentGraph/app/services/backups.py` | Backups, restores and restore drills (PRD AG-ST-10). |
| `AgentGraph/app/services/blackboard.py` | The live blackboard (agentgraph/spec-live-blackboard v1): an observation surface. |
| `AgentGraph/app/services/blobs.py` | First-class blob storage (v3.40). |
| `AgentGraph/app/services/budget.py` | Embedding metering, per-token write limits and loop breakers (PRD AG-ST-15). |
| `AgentGraph/app/services/cli_setup.py` | `npx veynod init`: a device flow that hands an agent key to a terminal. |
| `AgentGraph/app/services/code_engine.py` | Veynod Kernel as AgentGraph's code engine. |
| `AgentGraph/app/services/collab.py` | Collaboration primitives for tasks (PRD Phase 2 R2.1). |
| `AgentGraph/app/services/common.py` | Serialization, audit, events, and snapshots shared by the service layer. |
| `AgentGraph/app/services/contention.py` | Hot-document protection and section hashes (PRD AG-ST-14 point 3). |
| `AgentGraph/app/services/detail.py` | Detail levels: summary / standard / full (2026-10-23 call-cost proposal). |
| `AgentGraph/app/services/diagrams.py` | Diagrams and chunked dispatch (agentgraph/spec-diagram-dispatch v3), lightweight stage. |
| `AgentGraph/app/services/digest.py` | Watermarks and the "while you were away" digest (PRD AG-EV-04). |
| `AgentGraph/app/services/directory.py` | Collaborator directory: who the agents are (PRD AG-ID-06). |
| `AgentGraph/app/services/documents.py` | Shared document operations with versioning, human locks, and L2 artifacts. |
| `AgentGraph/app/services/event_dispatch.py` | Durable cross-domain event inbox and projection dispatcher (Phase 3). |
| `AgentGraph/app/services/events.py` | Unified event log: poll_events, subscriptions, sync changes (PRD AG-EV-01/02). |
| `AgentGraph/app/services/export_schedule.py` | Scheduled exports (PRD AG-ST-02 point 6). |
| `AgentGraph/app/services/exports.py` | Tenant export and import: the storage format, round-trip exact (PRD AG-ST-02). |
| `AgentGraph/app/services/external_actions.py` | Approved external-action outbox with retries and compensation. |
| `AgentGraph/app/services/flow.py` | The Loop page: the engine's view across every diagram (v3.24). |
| `AgentGraph/app/services/grants.py` | CommanderGrant: owner-issued, scoped, expiring authority (role rules v0.2). |
| `AgentGraph/app/services/growth_metrics.py` | Operator growth KPIs (v3.34): is anyone getting value, and coming back? |
| `AgentGraph/app/services/human_onboarding.py` | Getting-started checklist for people in the console. |
| `AgentGraph/app/services/idempotency.py` | Idempotency records for write requests (PRD AG-ST-18 point 1, §6.1 point 1). |
| `AgentGraph/app/services/inbox.py` | The inbox: every change reaches the people it concerns (2026-10-22 delivery |
| `AgentGraph/app/services/inbox_hints.py` | Pokes ride along (2026-10-24): every agent tool result says what is new. |
| `AgentGraph/app/services/incidents.py` | Security incidents: scope, notices within 72 hours, the record (PRD AG-TC-06). |
| `AgentGraph/app/services/indexer.py` | Asynchronous document indexing (PRD AG-ST-13). |
| `AgentGraph/app/services/leases.py` | Unified leases: heartbeats, fencing, expiry, orphans and handover (PRD AG-CO-03). |
| `AgentGraph/app/services/lifecycle.py` | Document lifecycle: superseded, expired, cold (PRD AG-ST-11). |
| `AgentGraph/app/services/lineage.py` | Lineage stamps, grant snapshots and accountability queries (PRD AG-ID-03). |
| `AgentGraph/app/services/links.py` | Typed links between tasks, documents, and slots. |
| `AgentGraph/app/services/logs.py` | Append-only log documents (PRD AG-ST-14 points 1-2). |
| `AgentGraph/app/services/mailer.py` | Outgoing mail through an outbox (incident notices, operator alerts). |
| `AgentGraph/app/services/members.py` | Human members and roles of a tenant node (PRD AG-ID-10). |
| `AgentGraph/app/services/messaging.py` | Friend-style messaging (blueprint-L3-messaging-open). |
| `AgentGraph/app/services/oauth.py` | OAuth 2.1 for MCP clients (v3.36): claude.ai connectors, Claude Desktop, IDEs. |
| `AgentGraph/app/services/onboarding.py` | Workspace conventions: constitution, namespace readmes (PRD AG-OB-02). |
| `AgentGraph/app/services/operator_privacy.py` | Operator privacy (v3.35): the operator sees counts and changes, never content. |
| `AgentGraph/app/services/orientation.py` | Orientation pack: an agent's first call (PRD AG-OB-01, degraded R0.2 form). |
| `AgentGraph/app/services/outbound.py` | Safe outbound HTTP helpers for webhooks and approved external actions. |
| `AgentGraph/app/services/pokes.py` | Realtime pokes (TASK-72A59BD9): presence, push, wake-up and receipts. |
| `AgentGraph/app/services/policies.py` | Path-level write policy and JSON Schema gates. |
| `AgentGraph/app/services/presence.py` | Presence states (blueprint L2; spec-realtime-messaging M-05a / M-01a). |
| `AgentGraph/app/services/product_events.py` | Operator product events (v3.34): moments the workspace data cannot show. |
| `AgentGraph/app/services/projects.py` | Product projects, execution graph and delivery views (Phase 7). |
| `AgentGraph/app/services/proposals.py` | Reviewed document proposals, Proposal Arena votes, and safe apply. |
| `AgentGraph/app/services/provenance.py` | Write provenance and the read window (PRD AG-TC-01). |
| `AgentGraph/app/services/quality.py` | Automated quality control 1.0 (工程设计 执行版 1.0). |
| `AgentGraph/app/services/reaper.py` | Background lease reaper for tasks and state slots. |
| `AgentGraph/app/services/recall.py` | Data recall along derivation edges (PRD AG-TC-02). |
| `AgentGraph/app/services/releases.py` | Release discipline: every deploy is visible to agents (2026-10-23 retest, finding 1). |
| `AgentGraph/app/services/resources.py` | Scarce resources: mutual exclusion and FIFO queues (PRD AG-CO-07). |
| `AgentGraph/app/services/retrieval.py` | Chunk indexing and hybrid (vector + lexical) document retrieval. |
| `AgentGraph/app/services/retrieval_eval.py` | Retrieval recall baseline and probes (PRD AG-ST-12). |
| `AgentGraph/app/services/reviews.py` | Peer reviews bound to outputs (PRD AG-CO-06). |
| `AgentGraph/app/services/rights.py` | Personal rights requests: locate, export, correct, pseudonymise, redact (PRD AG-TC-07). |
| `AgentGraph/app/services/runners.py` | Runner hosts: the computers that run agents (full-managed Loop, step 2). |
| `AgentGraph/app/services/scheduler.py` | Place tasks on runner hosts (full-managed Loop, step 3: runner-3-scheduler). |
| `AgentGraph/app/services/share_links.py` | Public document share links (v3.40). |
| `AgentGraph/app/services/sharing.py` | Cross-node visibility inside one tree (PRD AG-ID-12). |
| `AgentGraph/app/services/situation.py` | Situation awareness: per-principal metrics and anomaly alerts (PRD AG-TC-05). |
| `AgentGraph/app/services/slots.py` | Live state grid operations: JSON Patch, TTL leases, human sovereignty. |
| `AgentGraph/app/services/taint.py` | Prompt-injection taint provenance and downstream propagation. |
| `AgentGraph/app/services/tasks.py` | Task board: claims, TTL leases, L1/L2 handoffs, review and circuit breaker. |
| `AgentGraph/app/services/tenants.py` | Tenant lifecycle and membership sync (PRD AG-ID-07, AG-ID-10 groundwork). |
| `AgentGraph/app/services/timeline.py` | Timeline append/read helpers. |
| `AgentGraph/app/services/token_budget.py` | Per-token daily budgets along the token tree (PRD AG-ST-16). |
| `AgentGraph/app/services/token_naming.py` | Stable agent identity, credential generations and rename references. |
| `AgentGraph/app/services/token_tree.py` | The token tree: root token, issuance, revocation, renewal (PRD AG-ID-01/02/04/05). |
| `AgentGraph/app/services/tree.py` | The tenant tree in use (PRD AG-ID-09, AG-ID-11). |
| `AgentGraph/app/services/usage.py` | Workspace budgets, usage ledger, and fixed-window request rate limits. |
| `AgentGraph/app/services/webhooks.py` | Persistent webhook rules, matching, signed delivery, and retries. |
| `AgentGraph/app/services/workspace.py` | Workspace provisioning, L0 map, stats, and scoped agent tokens. |
| `AgentGraph/app/stamp_context.py` | The current request's lineage stamp (PRD AG-ID-03). |
| `AgentGraph/app/tenant.py` | Tenant row-security context for PostgreSQL RLS. |
| `AgentGraph/app/versioning.py` | API version negotiation (PRD AG-ST-18 point 5, §6.1 point 5). |
| `AgentGraph/app/warnings_context.py` | Soft-constraint warnings collected during a request (PRD §6 ``warnings[]``). |

### veynod_kernel/app

| Module | First docstring line |
|---|---|
| `veynod_kernel/app/__init__.py` | Veynod Kernel v4.0. |
| `veynod_kernel/app/api/__init__.py` | Aggregate API router (admin, runner, read-only DAG). |
| `veynod_kernel/app/api/admin_routes.py` | Bootstrap/admin plane: workspaces, runner tokens, artifacts, task seeding. |
| `veynod_kernel/app/api/cell_routes.py` | Cell and repository placement administration (Phase 4). |
| `veynod_kernel/app/api/code_intel_routes.py` | Snapshot, conflict and delivery read APIs (Phases 5/6). |
| `veynod_kernel/app/api/dag_routes.py` | Read-only DAG topology for the observation dashboard. |
| `veynod_kernel/app/api/runner_routes.py` | Runner plane: SSE preemption stream, lease heartbeat/release, task status. |
| `veynod_kernel/app/auth.py` | Machine (Runner) token authentication. |
| `veynod_kernel/app/config.py` | Core configuration for Veynod Kernel v4.0. |
| `veynod_kernel/app/database.py` | Async SQLAlchemy engine/session factory and genesis schema bootstrap. |
| `veynod_kernel/app/engine/__init__.py` | Veynod kernel graph-state engine. |
| `veynod_kernel/app/engine/kernel_service_impl.py` | SQLAlchemy-backed implementation of the 6-syscall KernelEngineService. |
| `veynod_kernel/app/engine/lock_algebra.py` | Hierarchical write-set lock algebra + a safe in-memory claim planner. |
| `veynod_kernel/app/engine/mutator.py` | Runtime graph mutations: SPLIT, contract ESCALATE, dependency INJECTION. |
| `veynod_kernel/app/engine/rust_bridge.py` | Optional bridge to the Rust core extension (`veynod_core`). |
| `veynod_kernel/app/engine/scheduler.py` | O(1) disjoint write-set scheduler and Kahn topological unblocker. |
| `veynod_kernel/app/engine/scheduler_core.py` | Python lifecycle for the Rust authoritative in-memory scheduler. |
| `veynod_kernel/app/engine/task_commands.py` | Minimal task command layer. Phase 1B starts with cancellation. |
| `veynod_kernel/app/main.py` |  |
| `veynod_kernel/app/mcp_server/__init__.py` | MCP / syscall surface (6 primitives). |
| `veynod_kernel/app/mcp_server/syscalls.py` | Veynod Kernel (v4.0) - Core MCP Syscalls & REST Unified Router |
| `veynod_kernel/app/models.py` | SQLAlchemy 2.0 ORM models for the bipartite contract DAG. |
| `veynod_kernel/app/runtime.py` | Application lifecycle: schema bootstrap, reconciliation and lease reaper. |
| `veynod_kernel/app/security.py` | Production safety checks for the Kernel. |
| `veynod_kernel/app/services/__init__.py` | Compiler, AST and event services. |
| `veynod_kernel/app/services/artifact_snapshot.py` | Immutable Git content and AST snapshot cache for the merge fast path. |
| `veynod_kernel/app/services/artifact_store.py` | Artifact upsert/refresh helper shared by the admin API and the engine. |
| `veynod_kernel/app/services/artifact_sync.py` | Post-commit artifact refresh and heuristic symbol call-graph rebuild. |
| `veynod_kernel/app/services/ast_slicer.py` | Tree-sitter based AST slicing. |
| `veynod_kernel/app/services/cell_registry.py` | Cell and repository placement registry (Phase 4). |
| `veynod_kernel/app/services/edge_rebuild.py` | Durable symbol-edge rebuild jobs. |
| `veynod_kernel/app/services/event_bus.py` | Cursor-based event stream (SSE) with PostgreSQL LISTEN/NOTIFY hints. |
| `veynod_kernel/app/services/event_hub.py` | Single-poller SSE fan-out with per-subscriber queues. |
| `veynod_kernel/app/services/gate_sandbox.py` | Isolated execution for agent-supplied gate commands. |
| `veynod_kernel/app/services/gate_worker.py` | Independent deterministic-gate worker. |
| `veynod_kernel/app/services/git_ops.py` | Async Git plumbing for worktree sandboxes and the atomic merge gate. |
| `veynod_kernel/app/services/merge_coordinator.py` | Per-repository fenced merge coordinator. |
| `veynod_kernel/app/services/outbox.py` | Transactional outbox dispatcher for kernel event notifications. |
| `veynod_kernel/app/services/request_timing.py` | Request timing middleware and benchmark latency headers. |
| `veynod_kernel/app/services/runtime_metrics.py` | Process-local runtime metrics for capacity acceptance. |
| `veynod_kernel/app/services/submission_service.py` | Durable asynchronous patch submissions. |
| `veynod_kernel/app/services/task_factory.py` | Shared task insertion used by the admin API and the cold-start scanner. |
| `veynod_kernel/app/services/verification_gate.py` | Four-stage deterministic verification gate + atomic Git merge. |
| `veynod_kernel/app/services/worktree_pool.py` | Reusable detached Git worktrees for the gate. |
| `veynod_kernel/app/worker.py` | Standalone deterministic-gate worker process. |

### agent_runtime

| Module | First docstring line |
|---|---|
| `agent_runtime/__init__.py` | Veynod Durable Agent Runtime (Phase 2A foundation). |
| `agent_runtime/api.py` | Internal Agent Runtime API (Phase 2C). |
| `agent_runtime/context.py` | Context assembly for the Agent Runtime (Phase 2F). |
| `agent_runtime/delegation.py` | Parent/child Agent Run delegation (Phase 2G). |
| `agent_runtime/errors.py` | Agent Runtime errors and command outcomes. |
| `agent_runtime/gateway.py` | HTTP adapters for the Agent Runtime (Phase 2E). |
| `agent_runtime/loop.py` | Durable Agent Loop Controller (Phase 2B). |
| `agent_runtime/maintenance.py` | Background maintenance loops for the Runtime (Phase 2J). |
| `agent_runtime/pool.py` | Supervised pool of RuntimeWorkers (Phase 2H). |
| `agent_runtime/quota.py` | Tenant-level runtime quotas (Phase 2J). |
| `agent_runtime/router.py` | Provider routing with ordered fallback (Phase 2J). |
| `agent_runtime/states.py` | Agent Run state machine (Phase 2A). |
| `agent_runtime/store.py` | Durable Agent Runtime store (Phase 2A). |
| `agent_runtime/tests/test_api.py` |  |
| `agent_runtime/tests/test_gateway.py` |  |
| `agent_runtime/tests/test_runtime.py` |  |
| `agent_runtime/worker.py` | Runtime worker: claim one RUNNING run under a fenced lease and execute a step. |
| `agent_runtime/worker_main.py` | Runtime Worker process entry point (Phase 2E). |

### contracts

| Module | First docstring line |
|---|---|
| `contracts/__init__.py` | Veynod cross-domain contracts (Phase 1 foundation). |
| `contracts/errors.py` | Cross-domain error codes. |
| `contracts/events.py` | Versioned event envelope shared by AgentGraph, Agent Runtime and Kernel. |
| `contracts/idempotency.py` | Canonical idempotency keys for cross-domain commands. |
| `contracts/identifiers.py` | Cross-domain identifiers for the Veynod Agent Coding Runtime. |
| `contracts/tests/test_contracts.py` |  |


---

## 文档校验清单

- [x] 服务边界、端口、数据库所有权
- [x] AgentGraph 核心服务/路由/UI/CLI
- [x] Agent Runtime 状态机/Store/Loop/API/进程
- [x] Kernel Engine/Scheduler/Gate/Merge/Sandbox/Rust
- [x] 端到端代码任务与 Agent Run 流程
- [x] 事件/契约/数据表
- [x] 完整 HTTP 路由表（附录 A）
- [x] 核心类/方法签名索引（附录 B）
- [x] 迁移索引（附录 C）
- [x] 测试命令与 Phase 8 真实结果
