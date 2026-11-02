# Veynod 项目架构与完整路径图谱

> **基准**：`main` @ `d37b781`（"Merge production/qualification: VPS deploy runbook and qualification"）。
> **快照日期**：2026-11-02。
> **读取方式**：本文全部路径与行为都来自 `main` 分支的真实文件（`git archive main` 导出后逐个核对），
> 不包含当时正在进行中的 `codex/phase2-diagram-kernel` 分支及未提交改动。
> **性质**：描述性清单 + 边界说明。凡标注"目标/规划"的段落都不是已实现事实。

如果要回答"这个仓库里有什么、放在哪、谁调谁、数据落哪张表、怎么起、怎么验"，
本文是唯一按目录逐项展开的索引。相邻文档的分工：

| 文档 | 用途 |
|---|---|
| [README.md](../../README.md) | 产品定位、一次任务链路、启动与 API 入口 |
| [docs/UNIFIED_ARCHITECTURE.md](../UNIFIED_ARCHITECTURE.md) | 已冻结的职责边界与执行链规范 |
| [docs/architecture/DATA_OWNERSHIP.md](DATA_OWNERSHIP.md) | 每张表的写入权威与已知多写入方缺陷 |
| [docs/architecture/INTEGRATION_MAP.md](INTEGRATION_MAP.md) | 真实进程/DB/事件/失败路径 |
| [docs/architecture/STATE_MACHINE_INVENTORY.md](STATE_MACHINE_INVENTORY.md) | 所有状态枚举、转换与守卫缺口 |
| [docs/architecture/RISK_REGISTER.md](RISK_REGISTER.md) | 编号风险（R-001…R-038）与阶段归属 |
| 本文 | 目录、模块、路由、表、脚本、测试的**完整路径地图** |

阅读顺序建议：§0 一页结论 → §2 目录职责 → §4 数据所有权 → §5 主链路 →
其余按需查。不要从 §9 的"缺口"开始读，那里是结论而非入口。

---

## 0. 一页结论

Veynod 是**一个产品、三个有界上下文、一个 PostgreSQL 集群三个数据库**的单仓：

```text
用户 / 浏览器 / CLI / MCP 客户端
      │  只登录产品面
      ▼
AgentGraph（唯一对外入口，api:8000）
  身份 · 租户 · 项目 · 产品任务 · 审批 · 文档 · 审计 · SSE · MCP · Next.js 控制台
      │  产品任务是唯一产品身份；执行态只经 task_lifecycle 写入
      ▼
Agent Runtime（内部，runtime-api:8002 + runtime-worker）
  Agent Run / Step / Checkpoint / 租约 / 委派 / ModelGateway / KernelToolRuntime
      │  12 个显式 Kernel capability，服务身份；提交后进 WAITING_EXTERNAL
      ▼
Veynod Kernel（内部，kernel-api:8001 + kernel-gate）
  DAG · 写集锁 · Worktree · Patch Submission · 5 阶段确定性门禁 · Merge CAS · Evidence
      │
      ▼
kernel_events → event_outbox → event_inbox → 投影 → REVIEWING → 人类 DONE
```

三条硬约束（代码与数据库约束都在）：

1. 一个产品任务最多一个活动 Run（`uq_collab_tasks_active_run` 部分唯一索引）。
2. `collab_tasks` 的**执行驱动**状态只由 `AgentGraph/app/services/task_lifecycle.py` 写入；
   claim/release/handoff/review/cancel 等人为动作仍走 `tasks.py`（ADR-015 的边界）。
3. 没有 Kernel 持久证据（与其 commit 匹配的 gate_results）就不推进任务；缺证据不是 PASS。

数据库按 bounded context 拆成 `veynod` / `veynod_kernel` / `veynod_runtime`，**不跨库外键**，
只靠 `collab_tasks.correlation` 里的 ID 关联。Kernel 与 Runtime 不发布到宿主机，只有 `api` 对外。

`main` 上仍然并行存在三条代码交付路径（§9.1 详述）：Diagram chunk 走 host runner + 人工 gate/merge；
产品任务经 KernelToolRuntime 走 Kernel 门禁；`CODE_ENGINE_LEGACY_DIRECT=1` 保留旧的 AgentGraph 直连
Kernel admin 建任务。Diagram 与 Kernel 在 `main` 上**尚未绑定**（`diagrams.py` 全文无 `kernel` 字样）。

---

## 1. 仓库顶层

```text
E:\Codes\veynod\
├── AgentGraph\              产品控制面（FastAPI + Next.js 静态导出 + MCP + CLI）
├── agent_runtime\           Durable Agent Runtime（Run/Step/Checkpoint/RunStore/KernelToolRuntime）
├── veynod_kernel\           确定性执行内核（FastAPI + Gate Worker + Rust core + syscall 面）
├── contracts\               跨域 ID / 事件 / 错误 / 幂等契约（无框架、无数据库）
├── deploy\                  统一部署（Dockerfile.kernel/.runtime、环境模板、initdb、代理样例、VPS compose）
├── docs\                    架构、ADR、审计、协议、验收、运维（§8）
├── scripts\                 e2e / 容量 / 混沌 / 恢复演练 / Rust 校验 / 进程守护
├── docker-compose.yml       一套本地/生产编排（db + kernel-api/gate + runtime-api/worker + api）
├── PROJECT_ARCHITECTURE.md  重构前总览（已被 UNIFIED_ARCHITECTURE 取代）
├── PROJECT_ARCHITECTURE_DETAILED.md  逐模块旧版详解（部分章节已过期）
├── README.md                产品入口
└── veynod_platform\         DEPRECATED 早期网关原型，不再部署
```

规模（`main`，仅源码行）：

| 区域 | 文件 | 行数 |
|---|---:|---:|
| `AgentGraph/app/` | 148 | 74,148 |
| `AgentGraph/app/services/` | 87 | 47,306 |
| `AgentGraph/app/api/` | 21 | 9,015 |
| `AgentGraph/dashboard/` | 41 | 15,752 |
| `AgentGraph/tests/` | 102（100 个 test_*.py） | 24,824 |
| `agent_runtime/` | 27 | 7,397 |
| `veynod_kernel/app/` | 50 | 15,868 |
| `veynod_kernel/tests/` | 29（28 个 test_*.py） | 5,818 |
| `veynod_kernel/rust/` | 5 | 2,346 |
| `contracts/` | 7 | 560 |
| `scripts/` | 12 | 2,910 |

行数是量级参考，不是质量或完成度指标。

---

## 2. 目录与模块职责

### 2.1 AgentGraph（产品控制面）

入口装配 `AgentGraph/app/main.py`：注册中间件、挂载路由、`/mcp`、静态控制台、健康检查与 SEO 端点。
`AgentGraph/app/api/__init__.py` 为空，路由由 `main.py` 逐个 `include_router`（`main.py:357-398`）。

```text
AgentGraph/
├── app/
│   ├── main.py                  FastAPI 装配、lifespan、后台循环、CORS、静态站点
│   ├── config.py                全部环境变量（Kernel/Runtime 地址、RLS、租约、限额；无统一前缀）
│   ├── database.py              async engine/session、`AgentGraph/sql/init.sql` + `AgentGraph/sql/migrations/`（77 个增量）、schema 版本
│   ├── tenant.py                RLS 会话上下文（`app.workspace_id` / `app.tenant_scope` / lineage stamp）
│   ├── auth.py                  Principal、AuthKind、RoleScope、token 与人类会话校验、能力矩阵
│   ├── models.py                48 张 ORM 表
│   ├── schemas*.py              Pydantic 请求/响应
│   ├── errors.py / error_registry.py / versioning.py / protocol_middleware.py
│   ├── middleware.py            请求上下文、指标、并发与数据库准入
│   ├── events.py                进程内 fan-out + PG LISTEN
│   ├── actor/身份辅助：actors.py、addresses.py、ids.py、capabilities.py、stamp_context.py、warnings_context.py
│   ├── api/                     REST 路由（逐文件见 §7.1）
│   ├── services/                领域服务（逐文件见 §6）
│   ├── engine/                  文档黑板/切片/嵌入/合并/格式化/Notion 适配
│   ├── mcp_server/server.py     113 个 MCP 工具 + 3 个 resource（FastMCP）
│   ├── templates/               Jinja 页面：root.html、code.html、agent_runs.html、projects.html
│   ├── static/                  favicon、og 图
│   ├── root_page.py / demo_page.py / docs_site.py / seo_geo.py / agent_guide.py
├── dashboard/                   Next.js 控制台（`output: "export"`，`basePath: "/dashboard"`）
│   ├── app/page.tsx             单页控制台（Diagram / Loop / Documents / Settings）
│   ├── app/components/          23 个视图组件（diagram-view、loop-view、task-board、documents…）
│   ├── app/docs/                公开文档站
│   └── app/{login,connect,deploy,oauth,growth,cli}/  独立页面
├── cli/                         `npx veynod`（init/listen/service）与 runner.mjs
├── desktop/                     Windows Tauri 客户端（已冻结，不参与主链路）
├── protocol/                    协议 TS 包
├── sql/                         init.sql + migrations（v2_to_v3、v3_2…v3_78、v3_hardening）
├── tests/                       100 个 test_*.py（含 conftest 共 102 个文件）
├── docs/                        产品/设计/安全/PRD/部署闸门文档
├── scripts/                     deploy_gate、benchmark、文档生成
└── eval/retrieval/              检索评测语料与基线
```

`main.py` 的 lifespan 在 API 进程内起大量后台循环：租约 reaper、webhook、外部动作、索引、
访问日志、token 用量、情况评估、邮件、事件 SLA、blob GC、presence、runner 放置调度，
受 flag 控制的 Kernel 事件投影（`CODE_PROJECTION_ENABLED`，另有 `CODE_PROJECTION_EVENT_ENABLED` 兜底）、
`EVENT_DISPATCH_ENABLED` inbox 投影、`AGENT_RUNTIME_PROJECTION_ENABLED` Run 投影与备份调度。
单进程承载全部调度是当前架构事实，也是容量与故障隔离的约束点。

关键边界：`AgentGraph/app/services/code_engine.py` 是**唯一**直接对 Kernel 管理 API 说话的 AgentGraph
模块；其余服务通过它、`kernel_delivery`（仅存在于后续分支）或 `task_lifecycle` 间接交互。

### 2.2 Agent Runtime（执行循环）

```text
agent_runtime/
├── api.py                      内部 FastAPI（`X-Runtime-Key`），Run 生命周期与事件
├── store.py                    RunStore：run/step/checkpoint/event/outbox/幂等/租约/外部等待/kernel 绑定
├── loop.py                     LoopController.step：一次模型决策 → 可选工具 → checkpoint
├── states.py                   RunState 枚举 + 转换矩阵
├── worker.py                   RuntimeWorker：带 fence 的单步执行
├── worker_main.py              进程入口：模型网关、工具模式（kernel/http）、池、维护循环、metrics:8031
├── pool.py / maintenance.py    受管 worker 池与后台维护
├── gateway.py                  HTTPModelGateway（OpenAI 兼容）+ HTTPToolRuntime
├── tools/kernel.py             KernelToolRuntime：12 个 capability，唯一代码执行适配器
├── tool_context.py             工具执行上下文（tenant/workspace/project/repo/kernel binding）
├── context.py                  有预算的上下文组装
├── quota.py / router.py        租户配额与 Provider 回退路由
├── wakeups.py                  Kernel 事件 → runtime_wakeups（持久游标）
├── delegation.py               父子 Run 委派
├── migrations/                 001…010
└── tests/                      7 个测试文件
```

Runtime 只写 `agent_runtime.*`，从不直接写 `collab_tasks`。等价地，它通过事件让 AgentGraph 投影。

### 2.3 Veynod Kernel（确定性执行内核）

```text
veynod_kernel/
├── app/
│   ├── main.py                  FastAPI 装配：syscall 路由 + api_router + engine 依赖覆盖
│   ├── runtime.py               lifespan：schema 初始化、对账、租约 reaper、API 平面守卫
│   ├── worker.py                独立 Gate Worker 进程入口
│   ├── config.py                全部 `VN_*` 参数（池、租约、准入、沙箱、命令、容量）
│   ├── database.py              引擎与会话、genesis + 迁移引导
│   ├── security.py              生产配置安全检查（弱 key、local 沙箱）
│   ├── auth.py                  `vnk_` runner token（只存 SHA-256）、绑定 workspace、token 缓存
│   ├── models.py                ORM：workspaces/code_artifacts/dag_tasks/task_dependencies/symbol_edges/kernel_events/agent_tokens
│   ├── api/                     管理面、runner 面、runtime 服务面、DAG 只读面、cell、code intel
│   ├── engine/                  scheduler（Rust/SQL 双路径）、lock algebra、mutator、task_commands、workspace_context
│   ├── services/                submission、gate worker、gate sandbox、verification gate、merge coordinator、git、worktree pool、event bus/hub、outbox、artifact sync/snapshot、edge rebuild、cell registry、task bootstrap/factory/evidence、metrics
│   └── mcp_server/syscalls.py   6 个 syscall + v2 异步提交 + `/mcp` JSON-RPC 桥
├── rust/veynod_core/            PyO3：scheduler_core、ast、locks、sandbox
├── sql/                         v4_001…v4_016
├── cli/bin/                     runner.mjs、veynod.mjs
├── scripts/                     bench、migrate_db、build_rust、smoke
├── dashboard-lite/              单文件只读 DAG 画布（默认关闭）
├── docs/                        架构、异步提交、门禁、合并、横向扩容、容量、压测
└── tests/                       28 个 test_*.py（含 conftest 共 29 个文件）
```

### 2.4 契约层

```text
contracts/
├── identifiers.py      IdentityChain、UUID 校验、correlation id
├── events.py           版本化 EventEnvelope、EVENT_TYPES、producer 注册表
├── errors.py           跨域 ErrorCode 与可重试性
├── idempotency.py      规范化 SHA-256 幂等键
├── tests/test_contracts.py
└── README.md
```

`main` 上**三个服务都还没有导入这个包**（`grep "from contracts"` 零命中）；它是 Phase 1 的词汇表，不是运行期依赖（§9.3）。

### 2.5 已废弃与外围

| 路径 | 状态 |
|---|---|
| `veynod_platform/` | DEPRECATED（ADR-018）。SQLite 网关原型，`/v1/platform/code-runs` 4 个端点 + `veynod_platform/templates/index.html`，仅作参考 |
| `AgentGraph/desktop/` | 冻结（D-1, 2026-10-30）。Tauri 客户端与 sidecar |
| `AgentGraph/open-source-repo/` | npm 发布包内容 |
| `AgentGraph/protocol/` | 协议 TS 定义与测试 |
| `.run/` | 本地运行/压测产物，不入库 |

---

## 3. 进程、端口与网络

| 进程 | 命令 | 端口 | 对外 | 说明 |
|---|---|---:|---|---|
| `api` | `uvicorn app.main:app`（`AgentGraph/Dockerfile`） | 8000 | **是** | 唯一产品入口；容器发布 `API_PORT:8000` |
| `runtime-api` | `uvicorn agent_runtime.api:app` | 8002 | 否 | `X-Runtime-Key` 保护 |
| `runtime-worker` | `python -m agent_runtime.worker_main` | metrics 8031（回环，`RUNTIME_WORKER_METRICS_PORT`） | 否 | 池化 Run 执行 |
| `kernel-api` | `uvicorn app.main:app` | 8001 | 否 | syscall / admin / runner / runtime 面 |
| `kernel-gate` | `python -m app.worker --workers N` | metrics 8030（回环，`--metrics-port`） | 否 | 独立门禁与合并协调进程 |
| `db` | `pgvector/pgvector:pg16` | 容器 5432 | 否 | 三个数据库 |

网络：单 `internal` bridge，只有 `api` 发布端口。VPS 编排额外有 `web` 网络给 Caddy（`deploy/vps/docker-compose.unified.yml`），
Kernel/Runtime 不接入 `web`。

健康语义：

| 端点 | 通过条件 | 用途 |
|---|---|---|
| `GET /healthz` | 进程存活 | liveness（带备份/演练时间戳） |
| `GET /readyz` | 数据库可达 + 嵌入配置 | readiness |
| `GET /healthz/deep` | 数据库、Runtime、Kernel 每项为 `ok`（未配置可为 `not_configured`） | 部署验收；任一已配置平面失败即 503 |
| `GET kernel:8001/healthz` | Kernel 存活与 `core.available` | 内核能力探测 |
| `gate:8030/metrics`、`runtime:8031/metrics` | — | 门禁积压与 worker 池快照 |

---

## 4. 数据所有权

一个集群三个数据库，由 `deploy/initdb/01-databases.sh` 初始化；各服务启动时执行自己的幂等迁移
（`scripts/migrate_all.sh` 可先在部署前统一跑完）。

### 4.1 `veynod`（产品控制面，AgentGraph 独占）

`AgentGraph/sql/init.sql` + 77 个增量迁移（145 条建表语句；48 张 ORM 表）。按域分组：

| 域 | 主要表 |
|---|---|
| 身份/租户 | `workspaces`、`tenants`、`tenant_policies`、`tenant_confirmations`、`memberships`、`human_users`、`human_sessions`、`agent_tokens`、`agent_directory`、`leases`、`commander_grants`、`grant_snapshots`、`constitution_acks`、`access_audit` |
| 产品任务 | `collab_tasks`、`projects`、`project_repositories`、`kernel_bindings`、`lifecycle_projections`、`runner_hosts`、`runner_placements` |
| 计划层 | `diagrams`、`diagram_chunks`、`diagram_changes` |
| 文档/黑板 | `shared_documents`、`document_versions`、`document_chunks`、`document_index`、`document_refs`、`document_blobs`、`blobs`、`blob_uploads`、`blob_upload_parts`、`namespaces`、`document_proposals`、`proposal_votes`、`log_entries`、`context_slots` |
| 事件/投递 | `workspace_events`、`timeline_streams`、`event_subscriptions`、`event_inbox`、`runtime_projection_cursors`、`code_projection_cursors`、`webhook_rules`、`webhook_deliveries`、`email_outbox`、`poke_deliveries`、`inbox_acks` |
| 质量/审批 | `quality_gate_runs`、`quality_circuit`、`quality_releases`、`audits`、`state_audit_diffs`、`audit_samples`、`audit_view_marks`、`change_claims`、`defect_escapes`、`poison_marks`、`deploy_gate_runs`、`deploy_gate_audit`、`deploy_gate_state` |
| 预算/计量 | `workspace_budgets`、`budget_limits`、`budget_buckets`、`budget_backoff`、`budget_circuits`、`usage_ledger`、`rate_limit_windows`、`principal_metrics`、`runtime_usage_metered`、`tenant_runtime_limits` |
| 治理/隐私 | `path_policies`、`taint_edges`、`recalls`、`recall_nodes`、`doc_derivations`、`rights_requests`、`rights_request_events`、`rights_exports`、`redaction_log`、`incidents`、`incident_nodes`、`incident_notices`、`incident_timeline`、`admin_access_sessions`、`support_access_grants`、`tenant_shares`、`share_links`、`share_link_accesses`、`tombstones`、`alias_redirects`、`cleanup_grants`、`doc_contention` |
| 消息/协作 | `conversations`、`conversation_members`、`messages`、`message_deliveries`、`message_escalations`、`escalations`、`resources`、`resource_queue`、`situation_alerts`、`platform_alerts`、`platform_releases`、`product_events`、`onboarding_progress`、`agent_onboarding`、`agent_presence`、`agent_aliases` |
| 运维 | `backups`、`restore_runs`、`restore_drills`、`exports`、`export_archives`、`imports`、`idempotency_records`、`schema_migrations` |

任务状态的多写入方（9+）与逐表权威判定的细节见 [DATA_OWNERSHIP.md §2](DATA_OWNERSHIP.md)。

### 4.2 `veynod_kernel`（执行内核）

16 个迁移，21 张表：`workspaces`、`code_artifacts`、`dag_tasks`、`task_dependencies`、`symbol_edges`、
`symbol_graph_versions`、`task_snapshots`、`kernel_events`、`agent_tokens`、`patch_submissions`、
`merge_intents`、`merge_coordinators`、`merge_conflicts`、
`admission_permits`、`scheduler_authorities`、`edge_rebuild_jobs`、`event_outbox`、`delivery_records`、
`cells`、`repository_placements`、`schema_migrations`。

关键约束：

- `dag_tasks.status` 枚举含 `BLOCKED/READY/LEASED/VERIFYING/SUSPENDED/COMMITTED/FAILED/CANCELLED/SPLIT_ARCHIVED`。
- `uq_dag_tasks_runtime_key (workspace_id, runtime_request_key)`：一个重试 Run 不会建出第二个 DAG 节点。
- `repository_placements.placement_epoch`：仓库迁移后旧 worker 不能完成合并。
- `task_snapshots`（v4_013）把任务钉在 base commit + manifest 哈希上；`delivery_records`（v4_014）
  每次 commit 落一条可审计凭据（含 evidence hash），`merge_conflicts` 持久化敏感文件冲突。
- 门禁的工作租约记录在 `patch_submissions` 的列上（`v4_003_gate_work_leases.sql` 只是迁移名，没有同名表）。
- 迁移 ledger 只覆盖部分文件（R-024）。

### 4.3 `veynod_runtime`（执行循环）

10 个迁移，13 张表：`agent_runs`、`agent_steps`、`run_checkpoints`、`run_events`、`runtime_outbox`、
`runtime_idempotency`、`invocations`、`external_invocations`、`runtime_wakeups`、`runtime_wakeup_cursors`、
`runtime_tool_audit`、`kernel_leases`、`kernel_tasks`。

`agent_runs` 带 `lease_token_hash` 与 `lease_epoch`；每次 claim 递增 epoch 作为 fencing token。

### 4.4 跨库关联

没有外键。唯一关联载体是 `collab_tasks.correlation` JSONB：

```text
tenant_id → workspace_id → project_id → task_key
  → correlation.run_id / trace_id / kernel_workspace_id / kernel_task_id / submission_id / commit_sha
    → Kernel dag_tasks.task_id → patch_submissions.submission_id → merge_intents.merge_id → HEAD
```

`kernel_delivery_leases` 表在 `main` 上还不存在（后续分支引入），它把 lease token 从返回浏览器的 correlation 里剥离。

---

## 5. 端到端主链路

### 5.1 产品任务 → Run → Kernel（唯一受支持链）

```text
1  POST /v1/tasks                      AgentGraph 建产品任务
2  POST /v1/tasks/{key}/run            task_lifecycle.start_run
     - 终态/审查态拒绝；已有活动 Run 幂等返回
     - 解析 kernel_bindings（workspace+project+repo → Kernel 实例/workspace）
     - 生成 trace_id，Runtime POST /v1/runs（X-Runtime-Key）
     - Runtime /start，回锁 collab_tasks 写 active_run_id + correlation
3  RuntimeWorker.claim_run             fenced lease（epoch）
4  LoopController.step                 ContextBuilder → model.decide
5  KernelToolRuntime.execute
     create_task          → Kernel POST /v1/runtime/tasks（request_key 幂等）
     pull_task_bundle     → DAG 节点 + write_set 锁 + worktree
     read_file / query_symbol_signature
     submit_patch         → POST /v2/syscalls/submit_patch → 202
6  deferred                            external_invocations 持久化，Run → WAITING_EXTERNAL，释放租约
7  Gate Worker                         5 阶段门禁 → merge_coordinator（CAS）
8  kernel_events → outbox → event_inbox → runtime_wakeups
9  Worker.consume_wakeups              finalize_external，Run 恢复 RUNNING
10 LoopController                      读 checkpoint + 门禁证据；失败则回喂模型修复
11 Run COMPLETED                       task_lifecycle.project_run → collect_evidence（必须匹配 commit）
     → quality.record_gate_result（CI/TESTS）→ claim/handoff → REVIEWING
12 人类审批                            REVIEWING → DONE
```

### 5.2 Kernel 门禁五个阶段

`GateStage`（`veynod_kernel/app/mcp_server/syscalls.py:85`）与 `verification_gate._run_attempt`：

1. `WRITE_SET_BOUNDARY` — patch 应用、空 patch、symlink、写集越界。
2. `AST_SCOPE_DIFF` — 从真实 base/patched 文件推导符号范围，不信任 runner 的 `touched_symbols`。
3. `CONTRACT_HASH_GUARD` — 冻结契约哈希。
4. `STATIC_TYPECHECK` — pyright。
5. `UNIT_TEST_SUITE` — pytest。

门禁在 `veynod_kernel/app/services/worktree_pool.py` 借出的 detached worktree 上跑；`gate_sandbox.py` 支持 `docker`（只挂 worktree、
只读 root、tmpfs、无网络、CPU/内存/PID 限制）与 `local`（生产需显式 `VN_ALLOW_LOCAL_GATE_IN_PROD`）。
通过后 `merge_coordinator.py` 在每仓库单一 coordinator 的 fence 下做 Git `update-ref` CAS，并落 `delivery_records`。

### 5.3 Diagram chunk 链（`main` 上与 Kernel 不连通）

```text
draft → confirm（人工）→ freeze（人工）→ dispatch
dispatch 建 collab_tasks（DG-<diagram>-<chunk>）并置 chunk=dispatched
host runner.mjs 领取 placement，模型改代码 → diagram submit（文件+commit+diff）
沙箱 CI（diagram ci）→ placement 把 review 步骤放到"没做过它的" reviewer host
  → 该 host 调 diagram(action="review")（实现者本人不能 gate）
placement 再把 merge 步骤放到 host → diagram(action="merge")，纯 JSONB 状态翻写，不做 Git
merge 末尾 _release_waiting 解锁下游并自动派发
```

Diagram 的 gate/merge 也是 MCP `diagram` 工具的动作（`AgentGraph/app/mcp_server/server.py:2117`），所以这里的"人工"
实际是"reviewer 主机上的模型按 placement 执行相应 action"，人只做 confirm/freeze 和例外干预。

`allowed_write_paths` 在这一链上只用于 `_outside_scope` 的 fnmatch 和跨图文件锁，**不进 Kernel 写集**。

---

## 6. AgentGraph 服务模块（`AgentGraph/app/services/`）

| 域 | 文件 |
|---|---|
| 身份/租户 | accounts、tenants、members、tree、sharing、rights、operator_privacy、access_log、lineage、attestation、directory、agent_merge、agent_onboarding、token_tree、token_naming、token_budget、grants、policies |
| 任务/协作 | tasks、task_lifecycle、leases、collab、reaper、scheduler、runners、reviews、quality、flow、diagrams、automation |
| 执行适配 | code_engine（Kernel 管理 API）、agent_runtime（Runtime 适配） |
| 事件/投递 | events、event_dispatch、inbox、inbox_hints、pokes、presence、messaging、webhooks、outbound、digest、timeline、links、logs |
| 文档/检索 | documents、blackboard、slots、proposals、retrieval、retrieval_eval、indexer、lifecycle、contention、taint、provenance、recall、blobs、share_links |
| 运维/治理 | audit、budget、usage、backups、exports、export_schedule、external_actions、incidents、situation、alerts、mailer、resources、releases、product_events、growth_metrics、human_onboarding、onboarding、orientation、detail、idempotency、common |

逐文件一句话职责用 `head -6 <file>` 可读到（几乎每个模块首行都有 docstring）。

---

## 7. HTTP / MCP 接口

### 7.1 AgentGraph 路由文件

| 前缀 | 文件 | 说明 |
|---|---|---|
| `/v1`（核心） | `AgentGraph/app/api/v1_routes.py` | 任务、租约、槽、文档、token、agent、proposal、diagram、quality、runner、presence、export、检索 |
| `/v1/tasks` | `AgentGraph/app/api/agent_run_routes.py` | `/execution`、`/trace`、`/run`、`/run/cancel` |
| `/v1/code` | `AgentGraph/app/api/code_routes.py` | `/runs` 列表/创建/读取/sync/audit |
| `/v1`（项目） | `AgentGraph/app/api/project_routes.py` | 项目、仓库绑定、执行图、交付、runtime 观测/限额 |
| `/v1/admin` | `AgentGraph/app/api/admin_routes.py` | 运营：workspace、文档、token、策略、预算、备份、租户、事件、评测 |
| `/v1/human` | `AgentGraph/app/api/human_routes.py` | 控制台人类动作：审核、策略、配额、质量、隐私 |
| `/v1/auth`、`/oauth` | `AgentGraph/app/api/auth_routes.py`、`AgentGraph/app/api/oauth_routes.py` | 人类登录、CLI 设备授权、MCP OAuth |
| `/v1/blobs`、`/v1/documents/*/blobs` | `AgentGraph/app/api/blob_routes.py` | 内容寻址 blob |
| `/v1/deploy-gate`、`/v1/admin/deploy-gate` | `AgentGraph/app/api/deploy_gate_routes.py` | VPS 部署闸门 |
| `/v1/tenants/*`、`/v1/shares` | `AgentGraph/app/api/org_routes.py` | 租户树、共享、访问会话、权利请求 |
| `/v1/stream`、`/v1/inbox/stream` | `AgentGraph/app/api/stream_routes.py`、`AgentGraph/app/api/inbox_stream.py` | SSE |
| `/v1/admin/event-inbox` | `AgentGraph/app/api/event_admin_routes.py` | inbox 统计、重放、手动 dispatch |
| `/.well-known/agentgraph/*` | `AgentGraph/app/api/wellknown_routes.py` | JWKS、撤销表 |
| `/dashboard/*`、`/docs/*`、`/s/*` | `AgentGraph/app/api/*_page.py`、`AgentGraph/app/api/public_docs.py`、`AgentGraph/app/api/share_routes.py` | 页面与公开文档 |

应用级端点还有 `robots.txt`、`sitemap.xml`、`llms.txt`、`llms-full.txt`、`og.png`、`favicon.*`、
`openapi.json`、`api-reference`、`/`、`/zh`、`/demo`、`/healthz`、`/healthz/deep`、`/readyz`。

`/mcp` 挂载 FastMCP（`AgentGraph/app/mcp_server/server.py`，113 工具 / 3 resource）；`MCP_ENABLED` 控制。

`/dashboard/code/`、`/dashboard/agent-runs/`、`/dashboard/projects/` 在 `main` 上都是 302 到 `/dashboard/`；详细页只在 `/dashboard/internal/*`，且需 `INTERNAL_VIEWS_ENABLED=1`（非生产默认开启）。

### 7.2 Kernel 路由

| 面 | 端点 |
|---|---|
| 6 syscall | `POST /v1/syscalls/{pull_task_bundle,request_task_split,escalate_contract_defect,inject_dependency_task,query_symbol_signature}`（全部 POST；`submit_patch` 走下面的 v2） |
| 异步提交 v2 | `POST /v2/syscalls/submit_patch`（202）、`GET /v2/submissions/find`、`GET /v2/submissions/{id}`；`/v1/submissions*` 兼容别名 |
| 管理面 | `/v1/admin/{workspaces,artifacts,tasks,tokens,cells,repositories,metrics,merge-conflicts,event-outbox}` |
| Runner 面 | `/v1/events/stream`、`/v1/runner/heartbeat\|release\|tasks/{id}\|ping` |
| Runtime 服务面 | `/v1/runtime/events`、`/v1/runtime/tasks`、`/v1/runtime/tasks/{id}/evidence\|cancel`；`/v1/runner/tasks/{id}/evidence` 与 `/v1/runner/workspaces/{id}/file` 也接受 runner 身份 |
| 只读 | `/v1/dag`、`/v1/dag/plan`、`/v1/dag/events`、`/dashboard`、`/healthz` |
| MCP | `POST /mcp` |

### 7.3 Runtime 路由

`POST/GET /v1/runs`、`/transition`、`/start`、`/pause`、`/resume`、`/cancel`、`/checkpoint`、
`/delegate`、`/children`、`/collect`、`/steps`、`/external`、`/wakeups`、`/checkpoints`、`/events`；
`/v1/events`、`/v1/events/head`、`/v1/stats`、`/v1/usage/{tenant}`、`/v1/quota/{tenant}`、
`/v1/provider-health`、`/healthz`。除 `/healthz` 外全部要求 `X-Runtime-Key`。

---

## 8. 部署、运维与文档

### 8.1 部署资产

| 路径 | 内容 |
|---|---|
| `docker-compose.yml` | db + kernel-api + kernel-gate + runtime-api + runtime-worker + api，健康条件启动 |
| `deploy/.env.unified.example` | 必填密钥清单（无默认凭证） |
| `deploy/Dockerfile.kernel` | 多阶段；`BUILD_RUST=1` 编译 PyO3 core；pyright 固定版本；生产健康检查 |
| `deploy/Dockerfile.runtime` | Runtime 镜像 |
| `AgentGraph/Dockerfile` | 控制台构建 + 非 root（uid 10001）API 镜像 |
| `deploy/initdb/01-databases.sh` | 建三个数据库 |
| `deploy/proxy/*` | Caddy / nginx / Traefik 的 SSE 不断连样例 |
| `deploy/vps/docker-compose.unified.yml` | VPS 单机编排（复用既有 db 容器，只有 api 接 `web`） |

### 8.2 运维与验收文档

`docs/PRODUCTION_OPERATIONS.md`（诊断入口 `/v1/tasks/{key}/trace`、健康表、已部署实例）、
`docs/UNIFIED_OPERATIONS.md`（部署形态、首次部署、升级、回滚、备份、故障）、
`docs/CAPACITY_BASELINE.md`、`docs/CHAOS_TEST_MATRIX.md`、
`docs/PRODUCTION_QUALIFICATION_REPORT.md`、`docs/SECURITY_QUALIFICATION_REPORT.md`、
`docs/UNIFIED_ACCEPTANCE_REPORT.md`。

VPS 部署禁忌：不要对 unified 文件 `down -v` 或 `up --remove-orphans`（数据库容器归旧 compose 管）。

### 8.3 ADR

`docs/adr/ADR_INDEX.md` 列 ADR-001…018（Proposed）与 2026-11-01 已接受的 ADR-013…018
（Runtime/Kernel 融合语义）。正文为 `docs/adr/ADR_013_RUNTIME_KERNEL_FUSION.md` 与 `docs/adr/ADR_019_FENCING_AND_MERGE_MODE.md`。

### 8.4 安全要点

- 生产要求 `ADMIN_TOKEN` ≥ 32 位；Kernel 拒绝已知弱 key（`veynod_kernel/app/security.py`）。
- 生产 `VN_GATE_SANDBOX=docker` 要求非 root 且 `network=none`；`local` 需显式放行。
- Kernel token `vnk_` 只存哈希，可绑定 workspace；`VN_REQUIRE_BOUND_TOKENS=true` 拒绝未绑定 token。
- AgentGraph 有 RLS 代码（`AgentGraph/app/tenant.py`），但 `rls_require_context` 默认 false（R-003 部分缓解）。
- Gate 执行的是 Agent 生成的代码，跨租户 Gate 隔离在 main 上仍属残余风险。

---

## 9. 已知缺口与并行路径

### 9.1 三条代码交付路径并存

| # | 入口 | 执行者 | 进 Kernel？ | 谁判通过 | 谁写 Git |
|---|---|---|---|---|---|
| A | Diagram `dispatch` → placement | `runner.mjs` + 本地 CLI | **否** | reviewer host 调 `diagram review`，再调 `diagram merge` | 外部 PR squash |
| B | `POST /v1/tasks`（带 repo）+ 自动 provision* | host `/kernel/lease` | 是 | Kernel Gate + Merge | Kernel |
| C | `POST /v1/tasks/{key}/run` | Runtime + `KernelToolRuntime` | 是 | Kernel Gate + Merge | Kernel |

\* `main` 上**没有** `kernel_delivery.py`，也**没有**创建时的自动 provision；B 链在 main 上是后续分支的内容。
`CODE_ENGINE_LEGACY_DIRECT=1` 是历史直连路径（AgentGraph 直接调 Kernel admin 建 artifact/task），
其状态由 `task_lifecycle.project_kernel_task`（"legacy code plane"）收敛。

### 9.2 Diagram ↔ Kernel 未绑定

`main` 的 `diagram_chunks` 没有 `kernel_task_id` / `scope_hash` / `merged_commit_sha`；`diagrams.py` 不引用
Kernel；Kernel 完成 commit 不会更新 chunk，chunk 的 `merged` 只由人工 merge 写入。`allowed_write_paths`
不进 Kernel 写集。`main` 的迁移上限是 AgentGraph v3_78、Runtime 010、Kernel v4_016。

### 9.3 契约与事件

`main` 的 `contracts/` 只有 identifiers / events / errors / idempotency 与 `contracts/tests/test_contracts.py`，
没有任何服务导入它。`work_scope.py`、work-item 与 work-scope schema 都是后续分支才加的，
因此"统一 envelope / WorkScope 契约"在 `main` 上属于纯规划，不是运行期事实。
Kernel 用 `kernel_events`/`event_outbox`，AgentGraph 用 `workspace_events`/`event_inbox`，Runtime 用
`run_events`/`runtime_outbox`，三套协议不同；SSE 的 `id` 与 `seq` 不一致（R-016）。

### 9.4 状态权威

AgentGraph 任务状态 9+ 写入方（`tasks.py`、`leases.py`、`collab.py`、`scheduler.py`、`reviews.py`、`diagrams.py`、`audit.py`、`quality.py`、admin 路由）、
Kernel `dag_tasks` 6+ 写入方，均无统一状态命令层与完整转换矩阵；`task_lifecycle` 只覆盖执行驱动的那部分（R-006、R-007）。

### 9.5 其它高优先风险（摘要）

证据合成（R-002）、merge 不复核 lease（R-001）、事件缺 correlation/DLQ（R-016）、
Gate 为 local 沙箱（R-004/R-035）、多仓库单 workspace 语义（R-023）、Kernel 无备份演练（R-025）、
容量在 0.24 tasks/s 饱和（R-021/R-022）。完整 38 条见 [RISK_REGISTER.md](RISK_REGISTER.md)。

### 9.6 容量真话

`docs/CAPACITY_BASELINE.md`：并发 10→250 全部完成，但吞吐稳定在 ~0.24 tasks/s，端到端 P50 随并发线性增长
（10 并发 39 s → 250 并发 1049 s）。瓶颈是 worktree 创建 + 树合并 + CAS + 证据投影，不是 pyright/pytest
（real profile 只慢 10–18%）。1,000 并发未验收。

---

## 10. 测试地图

| 区域 | 命令 | 依赖 |
|---|---|---|
| AgentGraph（100 个 test_*.py） | `cd AgentGraph && TEST_DATABASE_URL=... pytest tests -q` | PostgreSQL + pgvector |
| Runtime（7 文件）+ contracts | `TEST_DATABASE_URL=... pytest agent_runtime/tests contracts/tests -q` | PostgreSQL |
| Kernel（28 个 test_*.py） | `cd veynod_kernel && pytest tests -q` | PostgreSQL、git、pyright、pytest |
| Rust core | `cd veynod_kernel/rust/veynod_core && cargo test --release` | Rust |
| CLI | `cd AgentGraph/cli && npm test`（3 个 `node:test` 文件） | Node ≥ 18.17 |
| 全链路 | `python scripts/e2e_unified.py`（真实三库/Kernel/Gate/Git，脚本化模型） | 全部服务 |
| 真实模型 | `python scripts/e2e_unified.py --real-model` | Provider key |
| 混沌 9 场景 | `python scripts/chaos_suite.py` | 全部服务 |
| 容量 | `python scripts/capacity_unified.py --levels 10,25,50,100 --profile fast` | 全部服务 |
| Rust 镜像验证 | `python scripts/verify_rust_core.py` | Docker |
| 恢复演练 | `python scripts/restore_drill.py --source-prefix veynod_chaos_` | 备份 |

`AgentGraph/docs/PROJECT_MAP.md`（产品面路径指南，标注 3.24.0）与 `docs/architecture/MODULE_ASSET_MAP.md`
提供测试到模块的映射与资产分类（KEEP / REFACTOR / MIGRATE / RETIRE）。

---

## 11. 辅助脚本

| 脚本 | 用途 |
|---|---|
| `scripts/e2e_unified.py` | 真实全链路证明（除模型外全真） |
| `scripts/chaos_suite.py` | SIGKILL、重启、断连等 9 个故障场景 |
| `scripts/capacity_unified.py` | 统一容量压测（fast/real profile） |
| `scripts/restore_drill.py` / `rollback_drill.py` | 备份恢复与回滚演练 |
| `scripts/migrate_all.sh` | 部署前三库迁移 |
| `scripts/runtime_supervisor.py` | Runtime worker 进程守护 |
| `scripts/runtime_tool_server.py` | HTTP tool 协议测试替身 |
| `scripts/scripted_model_server.py` | 确定性 OpenAI 兼容模型（chaos/容量） |
| `scripts/verify_rust_core.py` | Rust 镜像内跑完整门禁与合并 |
| `scripts/backfill_unified.py` | 统一迁移数据回填 |
| `scripts/smoke_platform.py` | Platform 原型冒烟（已退役） |
| `veynod_kernel/scripts/*` | bench_swarm、bench_rust、build_rust、migrate_db、smoke_* |

---

## 12. 快速路径索引

| 想找什么 | 路径 |
|---|---|
| 产品入口装配 | `AgentGraph/app/main.py` |
| 产品配置 | `AgentGraph/app/config.py` |
| 执行驱动状态写入（唯一） | `AgentGraph/app/services/task_lifecycle.py` |
| 任务板 | `AgentGraph/app/services/tasks.py` |
| Diagram 计划层 | `AgentGraph/app/services/diagrams.py` |
| Kernel 管理适配 | `AgentGraph/app/services/code_engine.py` |
| Runtime 适配 | `AgentGraph/app/services/agent_runtime.py` |
| 事件投影 | `AgentGraph/app/services/event_dispatch.py` |
| 质量证据 | `AgentGraph/app/services/quality.py` |
| MCP 工具面 | `AgentGraph/app/mcp_server/server.py` |
| 控制台 | `AgentGraph/dashboard/app/page.tsx`、`app/components/` |
| Runtime 入口 | `agent_runtime/api.py`、`worker_main.py` |
| Run 存储与 fence | `agent_runtime/store.py` |
| Runtime→Kernel 适配 | `agent_runtime/tools/kernel.py` |
| Kernel API 入口 | `veynod_kernel/app/main.py` |
| Gate Worker | `veynod_kernel/app/worker.py`、`veynod_kernel/app/services/gate_worker.py` |
| 五阶段门禁 | `veynod_kernel/app/services/verification_gate.py` |
| 合并协调 | `veynod_kernel/app/services/merge_coordinator.py` |
| 沙箱 | `veynod_kernel/app/services/gate_sandbox.py` |
| 调度器 | `veynod_kernel/app/engine/scheduler.py` |
| Rust 调度核心 | `veynod_kernel/rust/veynod_core/src/scheduler_core.rs` |
| syscall 定义 | `veynod_kernel/app/mcp_server/syscalls.py` |
| Kernel 迁移（v4_001…v4_016） | `veynod_kernel/sql/` |
| 统一编排 | `docker-compose.yml`、`deploy/*` |
| 队列/事件 schema | `veynod_kernel/sql/v4_002_patch_submissions.sql`、`v4_005_admission_outbox_jobs.sql` |

---

## 13. 一句话

**AgentGraph 是唯一产品入口，Runtime 是唯一执行循环，Kernel 是 headless 确定性内核；三库一集群、
无跨库外键、无共享任务表，Kernel/Runtime 不对外。当前真实状态是"执行链已打通、容量约 0.24 tasks/s、
Diagram 与 Kernel 尚未绑定、三条代码路径并存"，不是可宣称千级 SLA 的生产系统。**
