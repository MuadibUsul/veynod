# Veynod 项目架构与路径

> 更新时间：2026-10-31  
> 根目录：`E:\Codes\veynod`  
> 当前产品形态：**AgentGraph 控制面 + Veynod Kernel 代码引擎**  
> 关键原则：**一个产品入口，两个 bounded context；不合并数据库 schema；Kernel 无产品 UI。**

---

## 0. 一页结论

```text
用户 / Agent
   │  只登录 AgentGraph
   ▼
AgentGraph（8000）
   ├─ 身份、租户、workspace、task、文档、审计、SSE、控制台
   ├─ /v1/code/*  →  code_engine（AgentGraph 内部模块）
   └─ code_engine
          │  KERNEL_URL / KERNEL_ADMIN_KEY / KERNEL_WORKSPACE_ID
          ▼
Veynod Kernel（8001）
   ├─ API：任务、租约、artifact、v2 submission
   ├─ Gate Worker：pyright / ruff / pytest / Git
   ├─ Merge Coordinator / Admission / Outbox
   └─ Rust SchedulerCore / AST / Locks
```

- **AgentGraph 是产品事实**：用户、任务、审批、审计、控制台。
- **Kernel 是代码执行事实**：仓库、AST 契约、符号锁、门禁、commit。
- **Platform 原型已退出主链路**：`veynod_platform/` 仅保留作参考。
- **Kernel 没有登录、没有 dashboard 入口**；8001 的 DAG 只作 debug。
- **当前不是 1,000 并发生产 SLA**：100 Runner 已验证；1,000 Runner 仍有连接层和 Gate 吞吐问题，详见 Kernel `docs/CAPACITY_1000.md`。

---

## 1. 根目录总览

```text
E:\Codes\veynod\
├── AgentGraph\                  # 产品控制面（Veynod 3.24.0）
│   ├── app\                     # FastAPI 控制面 + 代码引擎适配层
│   ├── dashboard\               # Next.js 人类控制台
│   ├── cli\                     # veynod CLI / runner
│   ├── sql\                     # 69 个迁移
│   ├── tests\                   # 93 个测试文件
│   └── docs\                    # 产品/架构/安全/PRD 文档
│
├── veynod_kernel\               # headless 代码引擎（Veynod Kernel 4.0）
│   ├── app\                     # API、worker、gate、merge、Rust bridge
│   ├── rust\veynod_core\        # tree-sitter / lock algebra / scheduler
│   ├── cli\                     # Kernel runner
│   ├── sql\                     # v4 schema + 8 个迁移
│   ├── tests\                   # 20 个测试文件
│   └── docs\                    # 架构、v2 提交、Gate Worker、容量
│
├── veynod_platform\             # 早期独立网关原型（已不在主链路）
├── .run\                        # 本地 demo/benchmark 运行数据，不是源码
├── scripts\                     # 根目录辅助脚本（Platform 原型 smoke）
├── README.md                    # 使用入口
└── PROJECT_ARCHITECTURE.md      # 本文档
```

### 规模参考

| 区域 | 规模 |
| :--- | ---: |
| AgentGraph Python | ~70,200 行 |
| AgentGraph dashboard | ~27,300 行 TS/TSX/CSS |
| AgentGraph 测试 | ~23,800 行 / 93 个测试文件 |
| AgentGraph migrations | 69 个 |
| Kernel Python | ~12,900 行 |
| Kernel Rust | ~2,350 行 |
| Kernel 测试 | ~4,900 行 / 20 个测试文件 |
| Kernel SQL migrations | 8 个（v4_001 → v4_008） |

行数只用于量级判断，不是质量指标。

---

## 2. 产品边界

| 能力 | 归属 |
| :--- | :--- |
| 人类账号、登录、租户、RLS | **AgentGraph** |
| workspace、membership、RBAC | **AgentGraph** |
| 产品任务 `collab_tasks`、审批、审计 | **AgentGraph** |
| 文档、状态槽、预算、Webhook | **AgentGraph** |
| 控制台、SSE、人类 Code 页面 | **AgentGraph** |
| 仓库、worktree、bare Git | **Kernel** |
| CodeArtifact、AST 契约哈希 | **Kernel** |
| 符号级写集锁、DAG 调度 | **Kernel** |
| 四阶段确定性门禁 | **Kernel** |
| Git `update-ref` / tree merge | **Kernel** |
| 代码任务映射 | **AgentGraph `input_context`** |
| 代码执行生命周期 | **Kernel task + AgentGraph task 双状态** |

**没有合并**：

- 数据库 schema；
- 任务表；
- 租约表；
- 事件总线；
- 两个 dashboard；
- 两套登录。

---

## 3. 运行时拓扑

### 3.1 进程

```text
Browser / Agent
      │
      ▼
AgentGraph API（Docker :8000）
  ├─ /v1/code/* → code_engine
  ├─ /v1/tasks、/v1/documents、/v1/quality/*
  ├─ /dashboard/*
  └─ SSE / audit / outbox
      │ HTTP（内部管理 API）
      ▼
Kernel API（:8001）
  ├─ /v1/admin/*      创建 workspace/artifact/task
  ├─ /v1/syscalls/*   前 5 个原语
  ├─ /v2/syscalls/submit_patch
  ├─ /v2/submissions/*
  └─ /v1/events/stream
      │
      ▼
PostgreSQL :55432（当前本地测试库）
      │
      ▼
Kernel Gate Worker（独立进程）
  ├─ app.worker
  ├─ GateWorker × N
  ├─ MergeCoordinator
  ├─ pyright / ruff / pytest
  └─ Git worktree / update-ref
```

### 3.2 当前本地运行状态

| 服务 | 地址 | 说明 |
| :--- | :--- | :--- |
| AgentGraph | `http://127.0.0.1:8000` | Docker，健康检查 3.24.0 |
| Kernel API | `http://127.0.0.1:8001` | 本地 uvicorn，健康检查 4.0.0，已加载 v2 路由 |
| PostgreSQL | `127.0.0.1:55432` | 测试库 |
| Kernel Gate Worker | 无固定端口 | 独立进程；本机已启动 `--workers 8` |

> **重要**：Kernel v2 只负责接受并持久化提交；门禁在独立 worker 中执行。  
> 如果只启动 `uvicorn app.main:app`，`POST /v2/syscalls/submit_patch` 会返回 202，但任务会停留在 `QUEUED`，不会被门禁处理。  
> 生产部署必须把 `python -m app.worker` 作为独立服务；本机已启动 8 个 worker。

`8010` 不是当前 Veynod 主链路；Platform 原型已停用，且该端口可能被其他本地服务占用。

---

## 4. AgentGraph 架构

### 4.1 分层

```text
app/main.py                  FastAPI 装配、lifespan、路由挂载
app/config.py                环境变量、Kernel/平台配置
app/auth.py                  Principal、token、RBAC、人类会话
app/models.py                36 张表的 ORM
app/schemas.py               Pydantic 请求/响应
app/api/                     REST 路由
app/services/                业务服务
app/engine/                  黑板/合并/格式化引擎
app/mcp_server/              MCP 工具面
dashboard/                   Next.js 控制台（静态导出）
sql/ + migrations            数据库 schema
cli/                         veynod CLI / runner
```

### 4.2 Code 集成文件

| 路径 | 作用 |
| :--- | :--- |
| `AgentGraph/app/services/code_engine.py` | **核心适配层**：直接调用 Kernel admin API，创建 artifact/task；读取 Kernel 状态；同步 gate；claim/handoff AgentGraph task |
| `AgentGraph/app/api/code_routes.py` | `/v1/code/*` 路由；人类会话/workspace key 鉴权；直接调用 `code_engine` |
| `AgentGraph/app/api/code_page.py` | 服务 `/dashboard/code/` 静态页面 |
| `AgentGraph/app/templates/code.html` | 人类 Code 页面；读取 `agentgraph.session_token` |
| `AgentGraph/dashboard/app/page.tsx` | 侧边栏 Code 入口 |
| `AgentGraph/app/config.py` | `KERNEL_URL`、`KERNEL_ADMIN_KEY`、`KERNEL_WORKSPACE_ID`、`KERNEL_REPO_PATH` |
| `AgentGraph/docker-compose.yml` | 把 Kernel 环境传给 API 容器 |

### 4.3 Code run 生命周期

```text
1. 用户/Agent 调 AgentGraph POST /v1/code/runs
2. code_engine 调 Kernel:
   - POST /v1/admin/artifacts（target stub + test，FROZEN）
   - POST /v1/admin/tasks（IMPLEMENT_BODY）
3. AgentGraph 创建 collab_tasks：
   - 状态 READY
   - input_context.code_engine = "veynod-kernel"
   - input_context.kernel = {workspace_id, task_id, base_url}
4. Kernel Runner 拉任务、改代码、提交 v2 patch
5. Kernel Gate Worker 跑 4 阶段门禁并 commit
6. AgentGraph GET /v1/code/runs/{task}:
   - 读取 Kernel task
   - COMMITTED → quality gate 回写 CI/TESTS PASS
   - claim → handoff REVIEWING
7. 人类/审查者在 AgentGraph 做最终 DONE
```

**当前限制**：Kernel 运行期间 AgentGraph task 仍为 `READY`；只有 commit 后 code_engine 才 claim → `REVIEWING`。失败回写、取消、重试仍未完全闭环。

### 4.4 Code API

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/dashboard/code/` | 人类 Code 页面 |
| GET | `/v1/code/runs` | 列出 code runs |
| POST | `/v1/code/runs` | 创建 AgentGraph task + Kernel task |
| GET | `/v1/code/runs/{task}` | 读取/刷新/同步 |
| POST | `/v1/code/runs/{task}/sync` | 强制刷新并同步 |
| GET | `/v1/code/runs/{task}/audit` | AgentGraph gate/audit 视图 |

### 4.5 AgentGraph 数据

- Code run 不新建表；映射放在 `collab_tasks.input_context`。
- `quality_gate_runs` 保存 CI/TESTS evidence。
- `state_audit_diffs` / `workspace_events` 保存审计与事件。
- AgentGraph 仍有自己的 Diagram/Chunk/Loop/Runner；**代码任务应只走 Kernel 一条路径**，旧代码路径需要继续收口。

---

## 5. Kernel 架构

### 5.1 进程模型

```text
Kernel API（uvicorn app.main:app）
  ├─ /v1/admin/*        workspace/artifact/task
  ├─ /v1/syscalls/*     pull/split/escalate/inject/query
  ├─ /v2/syscalls/submit_patch
  ├─ /v2/submissions/*
  └─ /v1/events/stream

Kernel Worker（python -m app.worker）
  ├─ GateWorker × N
  ├─ MergeCoordinator
  ├─ pyright/ruff/pytest
  ├─ Git worktree
  └─ outbox / edge jobs

PostgreSQL
  ├─ dag_tasks / code_artifacts / task_dependencies
  ├─ patch_submissions / gate_work_leases
  ├─ admission_permits / scheduler_authorities
  ├─ merge_intents / merge_coordinators
  ├─ edge_rebuild_jobs / event_outbox
  └─ kernel_events / symbol_edges
```

### 5.2 目录与职责

```text
veynod_kernel/
├── app/
│   ├── main.py                  FastAPI API 装配
│   ├── worker.py                独立 Gate Worker 进程
│   ├── runtime.py               API lifespan、EventHub、edge/outbox
│   ├── config.py                环境变量
│   ├── database.py              async engine/session、schema bootstrap
│   ├── models.py                核心 ORM（7 张表）
│   ├── auth.py                  vnk token
│   ├── engine/
│   │   ├── scheduler.py         claim、admission、authority、SQL/Rust 路径
│   │   ├── scheduler_core.py    Rust core 加载/增量同步
│   │   ├── kernel_service_impl.py 6 syscall 实现
│   │   ├── mutator.py           SPLIT/ESCALATE/INJECT
│   │   ├── lock_algebra.py      写集锁
│   │   └── rust_bridge.py       PyO3 桥
│   ├── services/
│   │   ├── submission_service.py  v2 202 接受/幂等/状态
│   │   ├── gate_worker.py       GateWorker 工作租约/门禁执行
│   │   ├── gate_sandbox.py      local/docker 沙箱
│   │   ├── merge_coordinator.py 合并队列/CAS/串行化
│   │   ├── verification_gate.py 四阶段门禁
│   │   ├── artifact_snapshot.py 不可变 Git/AST 快照
│   │   ├── artifact_sync.py     制品/符号边同步
│   │   ├── edge_rebuild.py      持久化 edge rebuild 队列
│   │   ├── outbox.py            outbox → pg_notify → SSE
│   │   ├── event_bus.py / event_hub.py 事件与 fan-out
│   │   ├── git_ops.py           Git/worktree/cat-file batch
│   │   ├── worktree_pool.py     gate worktree 池
│   │   ├── request_timing.py    请求阶段计时
│   │   └── runtime_metrics.py   运行时指标
│   ├── api/
│   │   ├── admin_routes.py      workspace/artifact/task/token
│   │   ├── runner_routes.py     SSE/heartbeat/release
│   │   └── dag_routes.py        DAG/健康检查
│   └── mcp_server/syscalls.py   6 syscall + v2 提交路由 + MCP
├── rust/veynod_core/src/
│   ├── scheduler_core.rs        权威内存图/锁索引/READY 堆
│   ├── ast.rs                   tree-sitter/签名/引用/全仓扫描
│   ├── locks.rs                 锁代数
│   └── sandbox.rs               命令监督
├── sql/                         v4_001 → v4_008
├── cli/bin/runner.mjs           v2 runner
├── dashboard-lite/index.html    debug DAG 画布
├── tests/                       20 个测试文件
└── docs/
    ├── ARCHITECTURE.md
    ├── ASYNC_SUBMISSIONS.md
    ├── GATE_WORKER.md
    ├── MERGE_COORDINATOR.md
    ├── HORIZONTAL_SCALE.md
    ├── CAPACITY_1000.md
    └── BENCH_ACCEPTANCE.md
```

### 5.3 提交协议 v2

```text
Runner
  POST /v2/syscalls/submit_patch
    → 202 {submission_id, status_url}
    → patch_submissions QUEUED

Gate Worker
  FOR UPDATE SKIP LOCKED 领取
  → work lease + fencing
  → 4 阶段门禁
  → MergeCoordinator
  → COMMITTED / FAILED

Runner
  GET /v2/submissions/{submission_id}
  → status / task_status / commit_sha / gate_results / error_detail
```

旧 `/v1/syscalls/submit_patch` 默认 410，提供迁移指引；`VN_LEGACY_SUBMIT_MODE=sync` 只作为过渡。

### 5.4 横向扩容与准入

- `scheduler_authorities`：每 workspace 一个 authority lease；只有 holder 使用 Rust core，其他实例走 SQL/advisory 路径；DB 单行 fence 是最终事实。
- `admission_permits`：claim 前按 gate/merge 队列深度发许可，超过阈值返回 429 + Retry-After。
- `VN_ADMISSION_GATE_QUEUE_LIMIT=256`、`VN_ADMISSION_MERGE_QUEUE_LIMIT=128`。
- `VN_SINGLE_KERNEL_CLAIM=false` 强制所有实例走 SQL/advisory，适合多进程/多主机。

### 5.5 数据表

```text
workspaces
code_artifacts
dag_tasks
task_dependencies
symbol_edges
kernel_events
agent_tokens
patch_submissions
gate_work_leases
merge_intents
merge_coordinators
admission_permits
scheduler_authorities
edge_rebuild_jobs
event_outbox
schema_migrations
```

> ORM `models.py` 仍只列 7 张核心表；v2/admission/merge/outbox 表由迁移和原生 SQL 使用。

### 5.6 关键配置

```text
VN_DB_POOL_SIZE=20
VN_DB_MAX_OVERFLOW=40
VN_SUBMISSION_WORKERS=8
VN_SUBMISSION_POLL_SECONDS=0.25
VN_WORK_LEASE_SECONDS=300
VN_WORK_HEARTBEAT_SECONDS=30
VN_WORKER_POLL_SECONDS=1.0
VN_GATE_SANDBOX=local | docker
VN_ADMISSION_GATE_QUEUE_LIMIT=256
VN_ADMISSION_MERGE_QUEUE_LIMIT=128
VN_ADMISSION_MODE=reject | defer
VN_ADMISSION_CLAIM_SLOTS=32
VN_SCHEDULER_AUTHORITY_LEASE_SECONDS=30
VN_EDGE_REBUILD_LEASE_SECONDS=120
VN_OUTBOX_POLL_SECONDS=0.1
VN_GATE_WORKTREE_POOL_SIZE=8
VN_FAST_FORWARD_MERGE=false
VN_SINGLE_KERNEL_CLAIM=true
```

---

## 6. 两个 bounded context 的契约

### AgentGraph → Kernel

| 调用 | 用途 |
| :--- | :--- |
| `POST /v1/admin/workspaces` | 创建代码 workspace（通常每仓库一个） |
| `POST /v1/admin/artifacts` | 注册 target stub / test artifact（FROZEN） |
| `POST /v1/admin/tasks` | 创建 Kernel `dag_task` |
| `GET /v1/admin/tasks` | 读取 Kernel 任务状态/commit |
| Runner `POST /v2/syscalls/submit_patch` | 提交代码补丁 |
| Runner `GET /v2/submissions/{id}` | 轮询门禁/合并终态 |

### Kernel → AgentGraph

| 调用 | 用途 |
| :--- | :--- |
| `POST /v1/quality/gate-results` | 回写 CI/TESTS PASS + evidence |
| `POST /v1/tasks/{key}/claim` | code_engine 接管任务 |
| `POST /v1/tasks/{key}/handoff` | 进入 REVIEWING，附 commit/gate brief |
| `GET /v1/quality/tasks/{key}/audit-view` | 人类审计视图 |

### 身份

- 用户/Agent 只持有 AgentGraph 的 session / token；
- AgentGraph 用内部配置的 `KERNEL_ADMIN_KEY` 调用 Kernel 管理面；
- Kernel 不直接暴露给最终用户；
- Kernel `vnk_` 只给 Kernel Runner / 内部服务。

---

## 7. 数据库与部署

### 7.1 数据库

| 数据库 | 内容 |
| :--- | :--- |
| AgentGraph `agentgraph` | 用户、租户、workspace、任务、文档、审计、SSE、预算、Webhook |
| Kernel `veynod` / `veynod_dev` | artifact、dag_task、patch_submissions、gate、merge、admission、outbox |

**当前是独立数据库/独立 schema，不做跨库 FK。**  
如果未来同库，建议：

```text
一个 Postgres
  ├── schema agentgraph
  └── schema kernel
```

保留表边界，不把 36 张表硬合成一套。

### 7.2 AgentGraph 部署

```bash
cd E:/Codes/veynod/AgentGraph
docker compose up -d --build
```

需要给 API 容器配置：

```text
KERNEL_URL=http://host.docker.internal:8001
KERNEL_ADMIN_KEY=<admin key>
KERNEL_WORKSPACE_ID=<kernel workspace id>
KERNEL_REPO_PATH=<bare repo path>
```

### 7.3 Kernel 部署

```bash
cd E:/Codes/veynod/veynod_kernel

# API
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001

# Gate Worker（必须）
python -m app.worker --workers 8 --database-url "$DATABASE_URL"
```

生产建议：

- API 与 worker 分容器；
- 多 API 实例 + 负载均衡；
- Gate Worker 按 CPU/仓库分片；
- PostgreSQL 连接池/PgBouncer；
- Gate sandbox 使用 Docker/K8s，不用 local。

---

## 8. 测试与验证

### AgentGraph

```bash
cd E:/Codes/veynod/AgentGraph
pytest -q
ruff check .
```

- 93 个测试文件；
- 文档口径约 204 项测试；
- 本次集成改动未跑全量 AgentGraph 套件；已验证 Code 页面、代理、任务生命周期。

### Kernel

```bash
cd E:/Codes/veynod/veynod_kernel

pytest -q -m "not integration"
pytest -q   # 需要 TEST_DATABASE_URL

cargo test --release
```

- 非 integration：79 passed（更新文档口径）；
- integration：25 passed（更新文档口径）；
- 20 个测试文件；
- 真实 Postgres/Git/pytest 场景。

### 当前已验证的集成链路

```text
AgentGraph task: code-284425ab8f1343fb
Kernel task:     7a086cfe-7230-40e1-95b3-4a7714feb13f
Kernel commit:   13607c9f0231164500dc9e576fdf2a3f57a95307
AgentGraph status: REVIEWING
Gates: CI PASS, TESTS PASS
```

> 该验证发生在 Kernel 更新前/更新过程中；更新后 API 已重启并加载 v2 路由，8-worker Gate Worker 已启动，但尚未重跑 direct code run，需补一次更新后验证。

---

## 9. 当前容量与限制

| 维度 | 现状 |
| :--- | :--- |
| 100 Runner / 生产 profile | 100/100 COMMITTED，accept P99 6.84s |
| 500 Runner / 生产 profile | 478/500，端到端约 0.35 commit/s |
| 1,000 SSE | 1000/1000 建立，0 SSE 错误 |
| 1,000 Runner / dev v9 | 1000/1000 COMMITTED，但 22.7% claim / 14.4% submit transport 错误 |
| 1,000 Runner / production | 881/1000，119 非终态；未验收 |
| Gate 瓶颈 | 8 workers 最优（12 CPU）；1,000 时 gate_pool_wait p50 约 191s |
| 多仓库 | 未解决：一个 Kernel 实例仍主要对应一个 workspace/repo |
| AgentGraph 控制面 | 单 FastAPI 进程；未做 1,000 活跃 Agent 压测 |

**结论**：当前是 **百级代码协作引擎**，不是千级生产 SLA。  
1,000 需要：分布式 Gate Worker、更多 CPU、仓库分片、API 横向扩容、连接层/负载均衡、PgBouncer。

---

## 10. 已知缺口

1. **Gate Worker 未纳入 AgentGraph 部署**
   - 必须在 Kernel 侧启动 `python -m app.worker`；
   - 否则 v2 提交停留在 QUEUED。

2. **Kernel 更新后验证尚未重跑**
   - API 已重启并加载 v2 路由；Gate Worker 已启动；
   - 仍需重跑一次 AgentGraph direct code run，确认 v2 + worker + merge 全链路；
   - 生产环境需确认 `v4_002` → `v4_008` 迁移全部应用。

3. **AgentGraph task 运行中状态未同步**
   - Kernel LEASED/VERIFYING 时 AgentGraph task 仍 READY；
   - 只有 COMMITTED 后才 claim → REVIEWING；
   - 失败/取消/重试未完全回写。

4. **多仓库/多租户**
   - Kernel syscall 仍以默认 workspace 为主；
   - 企业多仓库需要每仓库一个 Kernel 或 syscall 带 workspace_id。

5. **两条代码路径仍存在**
   - AgentGraph 自己的 Diagram chunk/merge queue；
   - Kernel code run；
   - 必须明确“代码任务只走 Kernel”。

6. **安全**
   - Gate 会执行 Agent 代码；
   - 生产必须 Docker/K8s 沙箱、网络隔离、资源限制；
   - Kernel admin API 不能暴露公网。

7. **容量**
   - 1,000 并发不是当前 SLA；
   - 需要单 Cell 容量模型、压测和分片。

---

## 11. 快速路径索引

| 想找什么 | 路径 |
| :--- | :--- |
| AgentGraph 入口 | `AgentGraph/app/main.py` |
| AgentGraph 配置 | `AgentGraph/app/config.py` |
| AgentGraph 任务服务 | `AgentGraph/app/services/tasks.py` |
| **Kernel 核心引擎适配** | `AgentGraph/app/services/code_engine.py` |
| **Code API** | `AgentGraph/app/api/code_routes.py` |
| **Code 页面** | `AgentGraph/app/templates/code.html` |
| **Code 侧边栏** | `AgentGraph/dashboard/app/page.tsx` |
| AgentGraph 控制台 | `AgentGraph/dashboard/` |
| AgentGraph 迁移 | `AgentGraph/sql/migrations/` |
| Kernel API 入口 | `veynod_kernel/app/main.py` |
| **Kernel Worker** | `veynod_kernel/app/worker.py` |
| **v2 提交** | `veynod_kernel/app/services/submission_service.py` |
| **Gate Worker** | `veynod_kernel/app/services/gate_worker.py` |
| **Gate Sandbox** | `veynod_kernel/app/services/gate_sandbox.py` |
| **Merge Coordinator** | `veynod_kernel/app/services/merge_coordinator.py` |
| 四阶段门禁 | `veynod_kernel/app/services/verification_gate.py` |
| 准入/横向 API | `veynod_kernel/app/engine/scheduler.py` |
| Rust 调度核心 | `veynod_kernel/rust/veynod_core/src/scheduler_core.rs` |
| AST/切片 | `veynod_kernel/rust/veynod_core/src/ast.rs` |
| Kernel 迁移 | `veynod_kernel/sql/` |
| Kernel 容量 | `veynod_kernel/docs/CAPACITY_1000.md` |
| Kernel v2 协议 | `veynod_kernel/docs/ASYNC_SUBMISSIONS.md` |
| Gate Worker 文档 | `veynod_kernel/docs/GATE_WORKER.md` |
| 横向扩容文档 | `veynod_kernel/docs/HORIZONTAL_SCALE.md` |
| 平台原型（已退出） | `veynod_platform/` |

---

## 12. 推荐阅读顺序

1. 本文档 §0–§6；
2. `AgentGraph/README.md`：产品面；
3. `AgentGraph/app/services/code_engine.py`：Kernel 如何成为核心引擎；
4. `veynod_kernel/README.md`：代码引擎；
5. `veynod_kernel/docs/ASYNC_SUBMISSIONS.md`：v2 提交；
6. `veynod_kernel/docs/GATE_WORKER.md`：门禁执行；
7. `veynod_kernel/docs/MERGE_COORDINATOR.md`：合并队列；
8. `veynod_kernel/docs/HORIZONTAL_SCALE.md`：多 API/准入；
9. `veynod_kernel/docs/CAPACITY_1000.md`：容量真相；
10. `AgentGraph/docs/PROJECT_ARCHITECTURE.md`：产品全量架构。

---

## 13. 一句话

> **AgentGraph 是唯一产品入口，Kernel 是 headless 代码引擎；两者通过 `code_engine` 直连，不合并 schema。当前架构能支撑百级代码协作，并为横向扩容留了 API/worker/admission/outbox 基础，但 1,000 并发生产 SLA 尚未验收。**
