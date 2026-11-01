# MODULE_ASSET_MAP — 模块资产地图

> 目的：为 Phase 0 建立“保留 / 重构 / 迁移 / 废弃”的资产分类，避免按目标目录机械搬迁。
> 证据：2026-10-31 源码盘点与实测；路径相对于 `E:\Codes\veynod`。
> 状态标记：KEEP（可保留）、REFACTOR（需重构）、MIGRATE（后续迁移）、RETIRE（退出主链路）、UNKNOWN（证据不足）。

---

## 1. AgentGraph（产品控制面）

### 1.1 入口与基础设施

| 模块 | 路径 | 职责 | 依赖/调用方 | 测试 | 分类 |
|---|---|---|---|---|---|
| FastAPI 装配 | `AgentGraph/app/main.py` | 路由挂载、lifespan、后台循环、CORS、静态页、MCP mount | 所有 API；Docker :8000 | 间接（集成测试启动 uvicorn） | KEEP + REFACTOR（单进程后台循环需拆分） |
| 配置 | `AgentGraph/app/config.py` | 环境变量、DB、Kernel URL/key、限额 | 全局 | `tests/test_schemas.py` 等 | REFACTOR（生产默认值/RLS fail-open） |
| 数据库/租户 | `AgentGraph/app/database.py`、`app/tenant.py` | async engine、RLS GUC、role 切换 | 所有服务 | `tests/test_production.py:618` | KEEP + REFACTOR（RLS 未强制） |
| 认证/RBAC | `AgentGraph/app/auth.py` | Principal、token、human session、permission matrix | API/MCP/服务 | `tests/test_security_hardening.py` 等 | KEEP |
| ORM | `AgentGraph/app/models.py` | 48 张表 | 所有服务 | 通过服务测试 | REFACTOR（职责过多，后续按 bounded context 拆 schema） |
| Schema | `AgentGraph/app/schemas.py` | Pydantic 请求/响应、状态白名单 | API/MCP | `tests/test_schemas.py` | KEEP |
| 错误/版本 | `app/error_registry.py`、`app/versioning.py` | 错误码、API 版本 | API/MCP | `tests/test_error_registry.py`、`test_protocol.py` | KEEP |
| 事件总线 | `app/events.py` | 进程内 fan-out + PostgreSQL LISTEN | SSE | `tests/test_events.py` | REFACTOR（跨进程语义） |
| 中间件 | `app/middleware.py`、`app/protocol_middleware.py`、`app/concurrency.py` | 请求上下文、协议、准入 | 全局 | 部分 | KEEP |

### 1.2 任务与协作

| 模块 | 路径 | 职责 | 依赖/调用方 | 测试 | 分类 |
|---|---|---|---|---|---|
| 任务服务 | `AgentGraph/app/services/tasks.py` | 创建、claim、renew、release、handoff、review、escalate、cancel、reassign、subtask | API/MCP/quality/code_engine | 55 项任务/租约/FSM 测试（9 文件） | REFACTOR：抽出**唯一任务命令层 + 转换矩阵** |
| 任务租约服务 | `app/services/leases.py` | grant/renew/orphan/transfer、continuation | tasks/scheduler | `tests/test_leases.py`、`test_fsm_fencing.py` | REFACTOR：与 tasks 的写入权合并 |
| 协作/问题 | `app/services/collab.py` | 人类问题、子任务取消、escalation 任务、恢复 | tasks/API | `tests/test_collaboration.py` | REFACTOR（直接改任务状态） |
| 评审 | `app/services/reviews.py` | peer review 直接 DONE | tasks | 少量 | REFACTOR（绕过 FSM） |
| 质量门禁 | `app/services/quality.py` | gate 结果、audit view、review 后处理 | code_engine/tasks | `tests/test_quality_control.py` | KEEP + REFACTOR（证据信任） |
| 任务树 | `app/services/tasks.py` 的 subtask/tree | 父子任务、依赖 | API | `tests/test_collaboration.py` | KEEP |
| 任务超时/回收 | `app/services/tasks.py`、`app/services/reaper.py` | lease 过期、unclaimed timeout | lifespan | `tests/test_v3_integration.py` | KEEP |

### 1.3 Code 路径

| 模块 | 路径 | 职责 | 依赖/调用方 | 测试 | 分类 |
|---|---|---|---|---|---|
| **Code 引擎适配** | `AgentGraph/app/services/code_engine.py` | 调 Kernel admin 创建 artifact/task；读状态；合成 gate；claim/handoff | `/v1/code/*` | **无** | **MIGRATE/REFACTOR，且必须纳入版本控制** |
| Code API | `app/api/code_routes.py` | `/v1/code/runs`、sync、audit | code_engine | **无** | 同上 |
| Code 页面 | `app/api/code_page.py`、`app/templates/code.html` | 人类极简 Code 页面 | 浏览器 | 无自动化 | REFACTOR（无 diff review） |
| 旧 Diagram/Chunk | `app/services/diagrams.py`、`app/api/v1_routes.py` | 图表、chunk 任务、提交/CI/queue/merge 元数据 | dashboard/MCP | `tests/test_diagrams.py`（20+） | **RETIRE from code path**（只保留协作编排能力） |
| 旧 Deploy Gate | `app/services/deploy_gate.py`、`app/api/deploy_gate_routes.py`、`scripts/deploy_gate.py` | VPS 部署门禁/证据 | 特定部署板 | `tests/test_deploy_gate*.py` | KEEP（运维域），不得作为代码交付主路径 |
| Flow | `app/services/flow.py` | 只读 loop 概览 | `/v1/loop/overview` | 间接 | KEEP |
| AgentGraph CLI | `AgentGraph/cli/bin/runner.mjs`、`veynod.mjs` | 设备登录、hub、listener、runner placement | 人类/Agent | `cli/test/*.mjs` 62 项 | KEEP + REFACTOR（无 code run 管理） |

### 1.4 事件、消息、文档

| 模块 | 路径 | 职责 | 测试 | 分类 |
|---|---|---|---|---|
| 事件服务 | `app/services/events.py` | poll/sync/cursor/subscription | `tests/test_events.py` | KEEP + REFACTOR（统一 envelope） |
| SSE 工作区流 | `app/api/stream_routes.py` | replay + live fan-out | `tests/test_hardening.py:237` | REFACTOR（id/seq 不一致、500 上限） |
| Inbox 流 | `app/api/inbox_stream.py`、`app/services/inbox.py` | targeted events、watermark、receipts | `tests/test_event_delivery.py`、`test_poke_*` | KEEP + REFACTOR |
| Webhook | `app/services/webhooks.py`、`outbound.py` | HMAC、重试、DEAD | `tests/test_phase3.py`、`test_protocol.py` | KEEP（补 replay） |
| 文档/黑板 | `app/services/documents.py`、`blackboard.py`、`slots.py` | 文档、版本、chunk、状态槽 | 多个文档测试 | KEEP（体量大，后续按域拆） |
| 消息 | `app/services/messaging.py` | 1:1/group、回执、mark read | `tests/test_messaging*.py` | KEEP |
| 预算/计量 | `app/services/budget.py`、`token_budget.py` | workspace 预算、usage ledger | `tests/test_budget*.py` | KEEP + REFACTOR（强制点） |
| 审计/DSAR | `app/services/audit.py`、`rights.py` | 审计、撤销、隐私删除 | `tests/test_operator_privacy.py` | REFACTOR（append-only、绕过触发器） |

### 1.5 Runner / Scheduler

| 模块 | 路径 | 职责 | 测试 | 分类 |
|---|---|---|---|---|
| Runner 主机 | `app/services/runners.py` | hello、heartbeat、配额、revoke | `tests/test_runners.py` | KEEP |
| Placement Scheduler | `app/services/scheduler.py` | 任务→主机、review/merge/supervise 步骤、move/withdraw | `tests/test_scheduler.py`、`test_loop_*` | KEEP（与 Kernel DAG 调度分离） |
| Loop view | `app/services/flow.py`、`app/services/scheduler.py` | 只读/放置视图 | 同上 | KEEP |
| MCP server | `app/mcp_server/server.py` | 113 个工具、3 个 resource | 多个 MCP/协议测试 | KEEP（无代码执行工具） |

### 1.6 Dashboard

| 模块 | 路径 | 职责 | 测试 | 分类 |
|---|---|---|---|---|
| Next.js 控制台 | `AgentGraph/dashboard/app/`、`components/` | Diagram、Loop、Documents、Settings、Code 链接 | 无 test/lint 脚本；只 `next build` | REFACTOR（补 Project/Review/Observability） |
| Code 页面 | FastAPI 静态页（非 Next 路由） | 创建/列表/JSON | 无 | REFACTOR |
| 产品文档 | `dashboard/app/docs/`、`app/docs_site.py` | 公开文档 | `tests/test_docs_site.py` | REFACTOR（未覆盖 Kernel/代码交付） |

---

## 2. Veynod Kernel（代码执行引擎）

### 2.1 入口与运行时

| 模块 | 路径 | 职责 | 调用方 | 测试 | 分类 |
|---|---|---|---|---|---|
| API 装配 | `veynod_kernel/app/main.py` | 9 行装配：syscalls + api routers + dependency override | uvicorn | `tests/test_main_wiring.py` | KEEP |
| API 生命周期 | `app/runtime.py` | init_db、HEAD 对账、Rust warmup、reaper、event sync、edge/outbox | API | 间接 | REFACTOR（API 启动不跑 merge reconcile） |
| Worker | `app/worker.py` | 独立 GateWorker × N、MergeCoordinator、outbox、edge、worktree、metrics :8030 | 运维 | `tests/test_work_leases.py` 等 | KEEP（部署必须独立进程） |
| 配置 | `app/config.py` | 全部运行时开关 | 全局 | — | KEEP + REFACTOR（安全默认值） |
| DB | `app/database.py` | async engine、session、genesis+migrations | 全局 | 集成测试 | KEEP + REFACTOR（迁移 ledger） |
| 模型 | `app/models.py` | 7 张 ORM 核心表；其余表由 SQL 迁移使用 | 全局 | — | REFACTOR（ORM/迁移不一致） |
| 认证 | `app/auth.py` | vnk token 哈希、dev 模式 | syscalls/runner | 无负向测试 | REFACTOR（生产默认/租户） |

### 2.2 调度与并发

| 模块 | 路径 | 职责 | 测试 | 分类 |
|---|---|---|---|---|
| Scheduler | `app/engine/scheduler.py` | claim、admission、authority、Rust/SQL 路径、reaper、release | `test_admission.py`、`test_horizontal_api.py`、集成 | KEEP + REFACTOR（单写入/公平性） |
| Rust Core Manager | `app/engine/scheduler_core.py` | authority lease、加载/sync、warmup、revert | `test_rust_core.py` | KEEP + REFACTOR（fencing 不参与写） |
| Rust Bridge | `app/engine/rust_bridge.py` | PyO3 导入 | `test_rust_core.py` | KEEP |
| Rust SchedulerCore | `rust/veynod_core/src/scheduler_core.rs` | 内存图、READY 堆、锁索引、claim/revert/sync | 6 Rust 测试（1 ignored） | KEEP |
| Lock Algebra | `rust/.../locks.rs`、`app/engine/lock_algebra.py` | 符号/祖先锁归一化、冲突、SPLIT 计划 | 11 Python + 5 Rust | KEEP |
| AST | `rust/.../ast.rs`、`app/services/ast_slicer.py` | tree-sitter 签名/引用/切片 | 16 Python + 4 Rust | KEEP |
| Mutator | `app/engine/mutator.py` | SPLIT/ESCALATE/INJECT | 集成测试 | REFACTOR（SPLIT/ESCALATE/INJECT lease 复核） |
| Syscalls | `app/mcp_server/syscalls.py`、`app/engine/kernel_service_impl.py` | 6 原语、v2 提交、MCP | `test_mcp_bridge.py`、集成 | KEEP + REFACTOR（workspace 维度、MCP submit 仍指向 v1） |

### 2.3 提交、Gate、Merge

| 模块 | 路径 | 职责 | 测试 | 分类 |
|---|---|---|---|---|
| Submission Service | `app/services/submission_service.py` | 202 接受、幂等、状态查询 | `test_submission_protocol_unit.py`、重启 E2E | KEEP + REFACTOR（accept TOCTOU） |
| Gate Worker | `app/services/gate_worker.py` | work lease、heartbeat、fence、gate→merge intent、重试 | `test_work_leases.py` | KEEP + REFACTOR（worker 死亡/VERIFYING 语义） |
| Verification Gate | `app/services/verification_gate.py` | 四阶段、AST 范围、契约哈希、静态检查、测试、merge/CAS | 集成测试（部分 skip typecheck/lint） | REFACTOR（merge 复核 task lease、证据、tree merge） |
| Gate Sandbox | `app/services/gate_sandbox.py` | local/docker 执行 | `test_integration.py:1272` 等 | REFACTOR（生产不得 local；Docker 未验证） |
| Merge Coordinator | `app/services/merge_coordinator.py` | per-workspace 选举、intent、CAS、reconcile | `test_merge_coordinator.py` | KEEP + REFACTOR（fencing/reconcile 幂等） |
| Git Ops | `app/services/git_ops.py` | worktree、apply、commit、update-ref、batch | `test_git_merge.py`、`test_git_batch.py` | KEEP |
| Worktree Pool | `app/services/worktree_pool.py` | 复用/重置 worktree | 间接 | KEEP |
| Artifact Snapshot | `app/services/artifact_snapshot.py` | 进程内 Git/AST 快照缓存 | 集成 | REFACTOR（持久化/共享） |
| Artifact Sync | `app/services/artifact_sync.py` | commit 后 artifact/edge 更新 | 集成 | KEEP |
| Edge Rebuild | `app/services/edge_rebuild.py` | 持久化 edge job | `test_edge_outbox.py` | KEEP |

### 2.4 事件与可观测性

| 模块 | 路径 | 职责 | 测试 | 分类 |
|---|---|---|---|---|
| Event Bus | `app/services/event_bus.py` | `kernel_events` + `event_outbox` 同事务 | `test_edge_outbox.py` | KEEP |
| Event Hub | `app/services/event_hub.py` | 轮询 DB → SSE fan-out | `test_horizontal_api.py` | KEEP + REFACTOR（consumer/DLQ） |
| Outbox | `app/services/outbox.py` | pg_notify + published_at | `test_edge_outbox.py` | REFACTOR（无 consumer/DLQ/retention） |
| Runtime Metrics | `app/services/runtime_metrics.py` | 内存 metrics/pool | 间接 | KEEP（需接 Prometheus/OTel） |
| Request Timing | `app/services/request_timing.py` | `X-Response-Time-Ms` | — | KEEP |
| API Routes | `app/api/admin_routes.py`、`runner_routes.py`、`dag_routes.py` | admin/syscalls/runner/SSE/dag/health | 集成/部分 | REFACTOR（鉴权、workspace 绑定） |

### 2.5 Kernel CLI / Dashboard / Docs

| 模块 | 路径 | 职责 | 分类 |
|---|---|---|---|
| Runner | `veynod_kernel/cli/bin/runner.mjs` | claim、worktree、模型、v2 提交/轮询/abort | KEEP + REFACTOR（workspace、sandbox、默认端口） |
| Dashboard-lite | `veynod_kernel/dashboard-lite/index.html` | debug DAG 画布 | RETIRE（或仅内网 debug） |
| Docs | `veynod_kernel/docs/*` | 架构、v2、gate、merge、容量 | REFACTOR（与实现同步） |

---

## 3. veynod_platform（原型）

| 模块 | 路径 | 职责 | 引用 | 分类 |
|---|---|---|---|---|
| Platform gateway | `veynod_platform/` | 早期独立网关/Code Run store | AgentGraph/Kernel 无引用；仅 `scripts/smoke_platform.py` | **RETIRE**（归档；不进入主链路） |
| Platform smoke | `scripts/smoke_platform.py` | 跨服务 smoke | 端口 8010 当前是其他应用 | RETIRE 或重写 |

---

## 4. 重复与冲突模块

| 重复/冲突 | 证据 | 处理建议 |
|---|---|---|
| 两套代码任务路径 | AgentGraph diagrams/chunk + Kernel code run | 明确“代码任务只走 Kernel”；diagram 只做协作编排 |
| 两套 Runner | AgentGraph CLI runner、Kernel CLI runner | 统一协议/命名；不要让两个 runner 抢同一任务 |
| 两套事件 | `workspace_events` 触发器 outbox、Kernel `kernel_events/event_outbox` | Phase 1 统一 envelope；Phase 3 建投影 |
| 两套任务状态 | AgentGraph `collab_tasks`、Kernel `dag_tasks` | 保留领域边界，但建立身份链与投影，不互写 |
| 两套调度 | AgentGraph placement scheduler、Kernel DAG scheduler | 明确 Orchestration vs Execution Scheduling |
| 两套 gate 证据 | AgentGraph `quality_gate_runs`、Kernel `gate_results` | AgentGraph 只投影 Kernel 证据，不合成 |
| 两套 dashboard | AgentGraph Next.js、Kernel `/dashboard` | Kernel dashboard 仅 debug/内网 |
| 两套 v1/v2 提交 | `/v1/syscalls/submit_patch`（410/sync）、`/v2/*` | 下线 v1 sync；MCP 提交改 v2 |
| 多写入任务状态 | 9+ AgentGraph、6+ Kernel 模块 | Phase 1 收口到命令层 |

---

## 5. 资产处置原则

- **KEEP**：已验证、边界清楚、有测试的能力（Rust 锁/AST、Kernel claim/CAS、AgentGraph auth/task 基础、事件 poll）。
- **REFACTOR**：有价值但边界/状态/安全需要修正（任务 FSM、事件协议、Gate/Merge、sandbox、admin auth）。
- **MIGRATE**：需要搬到新领域的代码/数据（Agent Loop、Project/Repository/CodingSession、Delivery）。
- **RETIRE**：已退出主链路或与目标冲突（platform、Kernel debug dashboard、旧 diagram code path 的执行语义、v1 sync submit）。
- **不要现在做**：按目标目录大规模搬迁；合并数据库；重写 Rust；拆微服务。

---

## 6. 迁移前置条件

1. 完成 Phase 1 统一 ID/契约/事件 envelope；
2. 建立唯一任务命令入口和转换矩阵；
3. 修复 P0 安全/正确性问题（merge lease、合成 gate、取消、admin/sandbox）；
4. code_engine 纳入版本控制并补契约测试；
5. 明确 Project/Repository/CodingSession 的归属；
6. 任何搬迁前先建立调用图和回归测试。

在此之前，只允许小步、可回滚的修复，不允许“大迁移”。
