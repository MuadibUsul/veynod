# Veynod 统一架构审计（Phase 0）

审计时间：2026-11-01。审计对象：`E:\Codes\veynod` 当前 `main`（`0718173`）。
方法：逐文件阅读真实实现与调用链，运行现有测试建立基线；不以 README 或既有架构文档为准。
本文中的所有判断都带有文件路径与函数名，便于复核。

---

## 1. 仓库结构（真实）

```
veynod/
├── AgentGraph/          产品控制面（FastAPI + Next.js dashboard + MCP）
│   ├── app/main.py, app/api/*, app/services/*, app/models.py
│   ├── dashboard/       Next.js 控制台（app/page.tsx + components/*）
│   ├── sql/init.sql + sql/migrations/v3_*.sql
│   └── tests/           99 个文件
├── agent_runtime/       Agent 执行循环（FastAPI + 独立 schema）
│   ├── api.py, loop.py, worker.py, pool.py, store.py, gateway.py,
│   │   context.py, delegation.py, quota.py, router.py, states.py
│   ├── migrations/001..007
│   └── tests/
├── veynod_kernel/       底层执行内核（FastAPI + Rust SchedulerCore）
│   ├── app/main.py, app/runtime.py
│   ├── app/mcp_server/syscalls.py          6 个 syscall + MCP 桥
│   ├── app/engine/{scheduler,scheduler_core,kernel_service_impl,
│   │                task_commands,mutator,lock_algebra,rust_bridge,hold_queue}.py
│   ├── app/services/{submission_service,gate_worker,verification_gate,
│   │                 merge_coordinator,git_ops,outbox,event_hub,...}.py
│   ├── app/worker.py                       Gate Worker 进程入口
│   ├── rust/veynod_core/                   Rust SchedulerCore
│   ├── sql/v4_001..v4_014
│   └── tests/           27 个文件
├── contracts/           跨服务 ID/事件/错误/幂等契约（Python）
├── veynod_platform/     第三套产品网关（:8010，自有 SQLite store）——历史遗留
├── scripts/             runtime_supervisor / runtime_tool_server / smoke_platform
└── docs/                历史 Phase 报告与架构文档（部分已落后）
```

三个可运行 HTTP 服务、三个独立数据库 schema、四套后台循环。

---

## 2. 服务职责（实际实现）

### 2.1 AgentGraph — 产品控制面

| 能力 | 实现位置 |
|---|---|
| 身份与会话 | `app/auth.py`、`app/api/auth_routes.py`、`app/api/human_routes.py` |
| Tenant/Workspace | `app/tenant.py`、`app/services/tenants.py`、`app/services/tree.py` |
| 产品任务 FSM | `app/models.py::CollabTask` + `app/models.py::TaskStatus`，`app/services/tasks.py`（`create_task`/`claim_task`/`handoff_task`/`fail_task`/`cancel_task`） |
| 文档/知识 | `app/services/documents.py`、`app/services/slots.py`、`app/services/retrieval.py` |
| 事件/审计 | `app/services/events.py`、`app/services/audit.py`、`app/models.py::WorkspaceEvent` |
| 质量门禁记录 | `app/services/quality.py::record_gate_result`、`::audit_view` |
| 产品级调度（host placement） | `app/services/scheduler.py`（`run_pass`、`_offer_work`、`_place_steps`） |
| 代码执行适配 | `app/services/code_engine.py::create_run`（**直接创建 Kernel 任务**） |
| Agent Run 适配 | `app/services/agent_runtime.py::create_task_run` |
| 事件投影 | `app/services/event_dispatch.py`（`event_inbox` 消费者） |
| 三个独立技术页面 | `app/api/code_page.py`、`app/api/agent_run_page.py`、`app/api/project_page.py` + `app/templates/*.html` |
| 主控制台 | `dashboard/app/page.tsx` |

关键路由前缀：`/v1`（v1_routes）、`/v1/code`、`/v1/tasks/{key}/run`、`/v1/projects`、`/v1/admin`、`/v1/human`、`/v1/stream`、`/v1/deploy-gate`。

### 2.2 Agent Runtime — 模型驱动执行循环

- `agent_runtime/loop.py::LoopController.step` 是唯一一步执行：读 run → 预算/配额 → 组装上下文（`context.py::ContextBuilder`）→ `model.decide` → 记录 step → 若 `kind=tool` 则**同步**执行工具 → checkpoint。
- `agent_runtime/worker.py::RuntimeWorker` 用 fenced lease 领取 RUNNING run（`store.claim_run`），`worker.py::_heartbeat` 续租。
- `agent_runtime/pool.py::RuntimeWorkerPool` 进程内多循环 + 崩溃重启。
- `agent_runtime/store.py::RunStore`：`agent_runs`、`agent_steps`、`run_checkpoints`、`run_events`、`runtime_outbox`、`runtime_idempotency`、invocations、usage、run leases、delegation。
- `agent_runtime/gateway.py::HTTPModelGateway`（OpenAI 兼容）、`HTTPToolRuntime`（**通用 HTTP 工具 + allowlist**）。
- `agent_runtime/delegation.py::DelegationManager` 支持父子 run 与 collect。
- `agent_runtime/maintenance.py::ReconciliationLoop` 回收委派。

### 2.3 Veynod Kernel — 确定性执行内核

- 6 个 syscall（`app/mcp_server/syscalls.py`）：`pull_task_bundle`、`submit_patch`（v1 同步 + v2 异步 202）、`get_submission_status`、`request_task_split`、`escalate_contract_defect`、`inject_dependency_task`、`query_symbol_signature`。
- 认证：`syscalls.py::get_authenticated_worker_id` 要求 `Bearer vnk_...` 且**绑定单一 workspace**；`app/auth.py::authenticate_worker_context`。
- 调度：`app/engine/scheduler.py::DisjointWriteSetScheduler` + `scheduler_core.py`（Rust `veynod_core`）+ `hold_queue.py::GLOBAL_CLAIM_HOLD_QUEUE` + admission control。
- 提交与门禁：`services/submission_service.py::submission_service.accept`（幂等、patch hash）、`services/gate_worker.py::GateWorker`（work lease、fencing、取消观察）、`services/verification_gate.py::DeterministicVerificationGate`（5 阶段）。
- 合并：`services/merge_coordinator.py`（CAS/树合并、冲突表）。
- 事件：`services/event_bus.py`（`kernel_events` + `pg_notify`）、`services/outbox.py::OutboxDispatcher`、`services/event_hub.py::EventHub`（SSE 扇出）。
- 管理 API：`app/api/admin_routes.py`（workspaces/tokens/artifacts/tasks/evidence/cancel/scan-stubs/metrics）。
- Runner API：`app/api/runner_routes.py`（heartbeat/release/task status/SSE）。
- Cells：`app/api/cell_routes.py`、`services/cell_registry.py`、`v4_012_cells_repositories.sql`。

### 2.4 Veynod Platform（`veynod_platform/`）

第三套产品网关：自有 `store.py`（SQLite）、`clients.py` 调 AgentGraph/Kernel、`app.py` 提供 `/v1/platform/*` 与页面。
**这是与"一个产品"直接冲突的第二产品面**，且不在生产 compose 中。

---

## 3. 关键结论：当前存在两条并行代码执行链

### 链 A：AgentGraph → Kernel（无模型）

```
POST /v1/code/runs  (app/api/code_routes.py)
  -> code_engine.create_run
       -> KernelEngineClient.create_artifact × N
       -> KernelEngineClient.create_task        # Kernel 内建 DAG 任务
       -> task_service.create_task              # 产品任务，input_context.code_engine="veynod-kernel"
外部 Kernel Runner（veynod_kernel/cli）claim -> 提交 patch -> gate -> merge
AgentGraph 通过 code_projection_loop / code_projection_event_loop / event_dispatch
  把 Kernel 终态投影成产品任务状态（_sync_lifecycle）
```

`code_engine.py::_workspace_id()` 直接返回全局 `settings.kernel_workspace_id`；
`create_run` 用 `settings.kernel_admin_key` 直连 Kernel 管理 API。

### 链 B：AgentGraph → Agent Runtime（有模型，但没有代码能力）

```
POST /v1/tasks/{key}/run (app/api/agent_run_routes.py)
  -> agent_runtime.create_task_run
       -> Runtime POST /v1/runs, POST /v1/runs/{id}/start
Runtime WorkerPool 用 HTTPToolRuntime 执行工具（部署默认指向 scripts/runtime_tool_server.py，只有 echo）
AgentGraph 周期性 GET Runtime run 状态（project_agent_runs_once）并投影到产品任务
```

### 结论

- 两条链各自维护产品任务的推进逻辑：`code_engine.lifecycle_action` / `_sync_lifecycle` 与 `agent_runtime.runtime_status_action` + `get_task_run`。
- **同一个产品任务可以同时被 Kernel Runner 和 Runtime 认领**：`scheduler.py::_fits` 只看 role/namespace/repo/capabilities，不看是否存在活动 Run；`code_engine.create_run` 创建的 Kernel 任务也不检查 Runtime。
- Runtime 完全没有 Kernel 能力：`ToolRuntime` 协议只有 `execute(name, arguments, idempotency_key)`，实现只有 `HTTPToolRuntime`，演示实现 `scripts/runtime_tool_server.py` 仅支持 `echo`。
- Kernel 没有"异步等待 + 唤醒"的 Runtime 侧消费者：`WAITING_TOOL` 状态存在（`states.py`），但 `LoopController.step` 在工具返回前一直持有 `WAITING_TOOL`，随即切回 `RUNNING`/`FAILED`。异步 202 提交无法表达为"等待外部事件"。

这是本次重构必须消除的核心问题。

---

## 4. 数据与状态所有权（现状）

| 对象 | 存储 | 谁在写 | 冲突 |
|---|---|---|---|
| 产品任务 | `collab_tasks` | `tasks.py`；`code_engine._sync_lifecycle`；`agent_runtime.get_task_run`；`scheduler.py` | **三处写同一状态** |
| Agent Run | `agent_runtime.agent_runs` | `RunStore`（唯一） | 无，但产品侧无事件订阅，靠轮询 |
| Kernel DAG Task | `dag_tasks` | Kernel scheduler/gate/merge | 相对干净 |
| 提交/门禁/合并 | `patch_submissions`,`merge_intents`,`delivery_records` | Kernel | 干净 |
| 事件 | `workspace_events`、`kernel_events`+`event_outbox`、`run_events`+`runtime_outbox` | 各自服务 | 三套流，无统一 `trace_id` 贯穿 |
| 用量 | `usage_records`（产品） + `runtime_usage_metered` + Runtime usage | `meter_runtime_run` | 跨库台账不统一 |

`input_context` JSONB 承担了跨服务关联（`input_context.agent_runtime.run_id`、`input_context.kernel.task_id`），
没有独立关联表，也没有 `trace_id`、`run_id`、`submission_id` 的强约束列。

---

## 5. 事件体系（三套，未合并）

1. **产品事件**：`workspace_events` + `events.py::event_bus`（LISTEN/NOTIFY、SSE `/v1/stream`）。
2. **Kernel 事件**：`kernel_events`（durable seq）+ `event_outbox`（`outbox.py`）+ `event_hub.py`（SSE `/events/stream`）+ `GET /v1/dag/events` 游标。
3. **Runtime 事件**：`run_events` + `runtime_outbox` + `GET /v1/events` + `event_inbox`（AgentGraph 侧 `event_dispatch.py`，含 DEAD/重放）。

`event_inbox` 只由 AgentGraph 消费，Runtime Worker 不消费任何事件 —— **不存在"外部事件唤醒 Runtime"的机制**。

---

## 6. Worker / Scheduler 职责对照（现状）

| 进程/循环 | 入口 | 实际职责 | 问题 |
|---|---|---|---|
| AgentGraph scheduler | `services/scheduler.py::scheduler_loop` → `run_all_locked` | 把 READY 任务放置到 runner host（suggested/offered/claimed）+ 步骤（work/review/merge） | 不知道 Runtime 是否已接管；可与 Kernel 链重复 |
| AgentGraph code projection | `code_engine.py::code_projection_loop` / `code_projection_event_loop` | 扫 `collab_tasks` + 读 Kernel 事件，投影状态 | 与 `agent_runtime` 投影并存 |
| AgentGraph runtime projection | `agent_runtime.py::agent_run_projection_loop` | 扫任务、GET Runtime run、改产品状态 | 轮询 GET；非事件驱动 |
| Runtime Worker Pool | `agent_runtime/worker_main.py` → `pool.py` | 领 RUNNING run，逐步执行模型+工具 | 无 Kernel 工具；长工具调用占用租约 |
| Kernel Gate Worker | `veynod_kernel/app/worker.py` → `gate_worker.py::GateWorker` | 领取 submission、跑确定性门禁、驱动 merge | 正确，保留 |
| Kernel Runner CLI | `veynod_kernel/cli/` | 外部进程 claim DAG 任务并提交 patch | 与新 Runtime 竞争同一任务 |

---

## 7. 部署（现状）

- `AgentGraph/docker-compose.yml` 与 `docker-compose.prod.yml`：只有 `db` + `api`，**不包含 Runtime、Kernel、Gate Worker**。
- Kernel 与 Runtime 靠手工命令启动（`uvicorn app.main:app --port 8001`、`python -m app.worker`、`python -m agent_runtime.worker_main`）。
- 健康：`/healthz` 各自返回自身状态；**没有任何一层检查"Runtime 和 Kernel 是否真的可用"**。
- 配置：`KERNEL_WORKSPACE_ID` 是全局单值（`AgentGraph/app/config.py:37`），多仓库只能靠多实例。
- `scripts/runtime_supervisor.py` 只守护 Runtime worker 进程。

---

## 8. UI（现状）

- Next.js 主控制台 `dashboard/app/page.tsx` 左侧导航（约 1299–1360 行）包含独立入口：
  `/dashboard/code/`、`/dashboard/agent-runs/`、`/dashboard/projects/`。
- 三个页面分别是独立 HTML（`app/templates/code.html`、`agent_runs.html`、`projects.html`），由三个 FastAPI 路由直接返回。
- Task Detail（`components/task-board.tsx`）不包含 Loop / Gate Evidence / Commit / Delivery。
- 结果是：Code、Agent Runs、Projects 是三个"技术控制台"，与任务详情割裂，正是任务书 §8 要求收敛的部分。

---

## 9. 缺失能力清单（对照任务书）

| 任务书要求 | 现状 | 结论 |
|---|---|---|
| KernelToolRuntime（9 能力） | 仅有 `HTTPToolRuntime` + echo | **缺** |
| Capability Registry / Tool Contract | 无 | **缺** |
| Workspace Routing（多仓库） | 全局 `KERNEL_WORKSPACE_ID` | **缺** |
| 异步 submission + 持久化 submission_id | Runtime 无 | **缺** |
| Durable Event 唤醒 Runtime | Runtime 不消费事件 | **缺** |
| Checkpoint Recovery（外部等待） | 无 WAITING_EXTERNAL | **缺** |
| Gate Evidence 返回给 Runtime | 无 | **缺** |
| 统一关联 ID（trace_id 等） | 仅 JSONB 约定 | **缺** |
| Product Task / Agent Run / Kernel Task 关系 | 隐含在 JSONB | **缺** |
| 唯一状态所有权 | 三处写产品任务 | **违反** |
| 并行执行体系消除 | 两条链 + Kernel Runner | **违反** |
| UI 收敛 | 三个独立入口 | **违反** |
| 统一部署 | 只有 AgentGraph compose | **缺** |
| 数据库迁移方案 | 各服务独立、无跨库计划 | **缺**（需补） |
| 真实全链路验收 | 从未端到端跑通 Runtime→Kernel→Gate→Commit | **缺** |

已有的、必须保留并复用的能力：
Rust SchedulerCore、DAG/写集锁、Task Leasing/Fencing、5 阶段确定性门禁、
Merge Coordinator（含冲突记录）、durable submission、admission control、
Kernel outbox/SSE、Runtime 的 RunStore/Loop/Lease/Pool/Delegation/Quota。

---

## 10. 已实现 vs 未实现（按模块）

- **AgentGraph 产品面**：成熟（身份、文档、审计、租约、审批、事件、MCP、dashboard）。
- **Agent Runtime**：Run 状态机、持久化、租约、池、委派、预算/配额、模型网关成熟；**工具面与 Kernel 无真实连接**。
- **Kernel**：调度、锁、门禁、合并、提交幂等、事件、容量控制成熟；**syscall 单 workspace 限制**。
- **跨服务**：仅"创建 + 轮询投影"，没有融合执行链。

---

## 11. 测试基线（本次审计实测）

| 套件 | 命令 | 结果 |
|---|---|---|
| agent_runtime + contracts | `python -m pytest agent_runtime/tests contracts/tests -q`（`TEST_DATABASE_URL=.../veynod_test`） | 11 passed, 22 skipped（无 DB 时）；带 DB 后 `agent_runtime` 28 passed |
| Kernel | `cd veynod_kernel && python -m pytest tests -q` | 138 passed（先释放 8030 端口后） |
| AgentGraph | `cd AgentGraph && TEST_DATABASE_URL=.../agentgraph_test .venv/Scripts/python.exe -m pytest tests -q` | 需要 pgvector；旧测试库 `veynod-test-pg` 无 `vector` 扩展，历史上 448 个集成测试被跳过。本次新建 `veynod-test-pgvector`（:55433）后运行，见 `docs/UNIFIED_ACCEPTANCE_REPORT.md` |

环境事实：PostgreSQL `veynod-test-pg`（:55432，postgres:16-alpine，无 pgvector）、新增 `veynod-test-pgvector`（:55433，pgvector/pgvector:pg16）。端口 8000 被 Docker 容器 `agentgraph-api-1` 占用，不是 Kernel。

---

## 12. 保留 / 重构 / 合并 / 废弃

| 模块 | 决定 |
|---|---|
| AgentGraph 身份、文档、审计、审批、事件、MCP、dashboard | **保留** |
| `services/code_engine.py` 的"Clients + 证据映射" | **保留为底层适配**，但**移除其自动创建/推进执行链的职责** |
| `services/agent_runtime.py` 的 Run 客户端与用量计量 | **重构**为统一生命周期服务 |
| AgentGraph scheduler（host placement） | **保留**，增加"已被 Run 接管则跳过"的排他规则 |
| Agent Runtime 全量 | **保留并扩展**（唯一 Agent 执行控制中心） |
| `agent_runtime/gateway.py::HTTPToolRuntime` | **保留**用于通用 HTTP 工具；新增 `KernelToolRuntime` |
| `scripts/runtime_tool_server.py` | **降级为测试夹具**，不得作为默认执行工具 |
| Kernel 调度/门禁/合并/事件/容量 | **保留原样**（不重写 Rust 核心） |
| Kernel syscall 单 workspace | **升级为显式 workspace 路由**（协议向后兼容） |
| Kernel Runner CLI | **保留为兼容/开发工具**，默认不再是产品任务的执行者 |
| `veynod_platform/` | **废弃**（不删除代码，移出产品路径，标记 deprecated） |
| 三个独立页面与导航项 | **合并进 Task Detail / 项目视图**，路由保留为重定向（受权限保护的内部视图） |

---

## 13. Phase 0 结论

当前系统不是"一个 Veynod"，而是：

> 一套成熟的产品控制面 + 一个没有工具面的 Agent Runtime + 一个强大的 Kernel，
> 通过"建任务 + 轮询状态"松耦合拼接，且存在两条可同时认领同一任务的执行链。

Phase 1 冻结架构时必须回答的唯一问题：**让 Runtime 成为唯一 Agent 执行控制链，通过 KernelToolRuntime 调用 Kernel，Kernel 只做确定性执行与门禁。**
后续所有实施都围绕这一条主线。
