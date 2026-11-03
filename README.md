# Veynod — 统一的 Agent Coding Runtime

> 一个产品入口，一套任务体系，一个 Agent 执行循环，一个确定性的执行内核。

Veynod 让用户提出目标、创建项目、组织任务；Agent Runtime 理解目标、规划步骤、调用 Kernel
改代码、跑门禁、处理失败、恢复执行；Kernel 负责 DAG 调度、写集锁、确定性门禁与原子合入；
人类在同一个控制台里审批与交付。

```
USER
 │
 ▼
Veynod 产品面（AgentGraph：身份 / 租户 / 项目 / 任务 / 审批 / 文档 / 审计 / UI）
 │   唯一产品任务，唯一执行链
 ▼
Agent Runtime（唯一的模型推理与执行循环：Run / Step / Checkpoint / Lease / Delegate）
 │        ModelGateway                KernelToolRuntime
 │              │                          │
 │              ▼                          ▼
 │        模型提供方                  Veynod Kernel（headless）
 │                              Rust SchedulerCore / DAG / 写集锁 / AST
 │                              Task Leasing / Worktree / Patch
 │                              5 阶段确定性门禁 / Merge / Git CAS / Evidence
 │                                          │
 └────────── Durable Events ◄───────────────┘
             （kernel_events → event_inbox → runtime_wakeups → 投影 → REVIEWING → DONE）
```

架构与决策：[docs/UNIFIED_ARCHITECTURE.md](docs/UNIFIED_ARCHITECTURE.md)、
[docs/adr/ADR_013_RUNTIME_KERNEL_FUSION.md](docs/adr/ADR_013_RUNTIME_KERNEL_FUSION.md)。
审计与迁移：[docs/UNIFIED_ARCHITECTURE_AUDIT.md](docs/UNIFIED_ARCHITECTURE_AUDIT.md)、
[docs/UNIFIED_MIGRATION_PLAN.md](docs/UNIFIED_MIGRATION_PLAN.md)、
[docs/UNIFIED_ACCEPTANCE_REPORT.md](docs/UNIFIED_ACCEPTANCE_REPORT.md)。

---

## 1. 一个产品，三类进程

| 进程 | 代码 | 唯一职责 | 是否对外 |
|---|---|---|---|
| `api` | `AgentGraph/` | 身份、项目、任务、审批、UI、事件投影 | **是**（唯一的用户入口） |
| `runtime-api` / `runtime-worker` | `agent_runtime/` | 模型推理、规划、工具调用、Checkpoint、租约、恢复 | 否（内部服务） |
| `kernel-api` / `kernel-gate` | `veynod_kernel/` | DAG 调度、写集锁、确定性门禁、合并、证据 | 否（内部服务） |
| `db` | PostgreSQL 16 + pgvector | 一个集群，三个 bounded-context 数据库 | 否 |

用户只需要一个账号、一个 dashboard、一条命令。

---

## 2. 一次任务的完整链路

```
Create Project → Create Task → POST /v1/tasks/{key}/run
  → AgentGraph 生成 trace_id、绑定 Kernel 仓库、创建唯一 Agent Run
  → Runtime Worker 领取 Run
  → 模型决定：create_task / pull_task_bundle / read_file / submit_patch …
  → Kernel 创建或取得 DAG 节点、分配写集与上下文
  → 模型产出 patch，Runtime 提交（202 + submission_id），Run 进入 WAITING_EXTERNAL 并释放租约
  → Kernel Gate Worker 执行 5 阶段确定性门禁 → 合并 → 生成 Evidence
  → SUBMISSION_COMMITTED 事件 → runtime_wakeups → Run 恢复
  → 门禁失败则把证据交回模型继续修复；通过则 Run COMPLETED
  → AgentGraph 记录 CI/TESTS 证据 → 任务 REVIEWING
  → 人类审批 → DONE
```

关键约束（由代码强制）：

* 一个产品任务最多一个活动 Run（`collab_tasks.active_run_id` + 部分唯一索引）。
* `collab_tasks` 的执行态只由 `AgentGraph/app/services/task_lifecycle.py` 写入。
* 有活动 Run 的任务不会被 runner-host 调度再次认领。
* 等待门禁期间 Runtime Worker 不占用租约；唤醒来自持久事件，不是定时轮询。
* 没有 Kernel 证据就不推进任务：缺证据不是 PASS。

---

## 3. 启动（生产）

```bash
cp deploy/.env.unified.example .env      # 填入密钥
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/healthz/deep
```

`/healthz/deep` 只有在数据库、Agent Runtime、Kernel 都可用时才返回 200：
API 起来了但执行链不可用时，部署会显示为不健康。

迁移由各服务启动时幂等执行；也可以先统一执行：

```bash
./scripts/migrate_all.sh
```

---

## 4. 本地开发

```bash
# 1. PostgreSQL（三库）
docker run -d --name veynod-dev -p 55432:5432 \
  -e POSTGRES_USER=agentgraph -e POSTGRES_PASSWORD=agentgraph \
  pgvector/pgvector:pg16

# 2. Kernel（:8001）
cd veynod_kernel && DATABASE_URL=postgresql+asyncpg://... VN_AUTH_MODE=dev \
  VN_ADMIN_API_KEY=dev-admin python -m uvicorn app.main:app --port 8001
python -m app.worker --workers 2 --database-url postgresql+asyncpg://...

# 3. Runtime（:8002）
RUNTIME_DATABASE_URL=... RUNTIME_API_KEY=dev-runtime \
  python -m uvicorn agent_runtime.api:app --port 8002
RUNTIME_TOOL_MODE=kernel KERNEL_URL=http://127.0.0.1:8001 \
  KERNEL_RUNTIME_TOKEN=vnk_... KERNEL_SERVICE_KEY=... \
  RUNTIME_MODEL_URL=... RUNTIME_MODEL_API_KEY=... RUNTIME_MODEL_NAME=... \
  python -m agent_runtime.worker_main

# 4. 产品面（:8000）
cd AgentGraph && DATABASE_URL=... ADMIN_TOKEN=dev-admin \
  AGENT_RUNTIME_URL=http://127.0.0.1:8002 AGENT_RUNTIME_KEY=dev-runtime \
  KERNEL_URL=http://127.0.0.1:8001 KERNEL_ADMIN_KEY=dev-admin \
  .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

产品入口：<http://127.0.0.1:8000/dashboard/>。Kernel 与 Runtime 没有用户页面。

### 把一个仓库接入项目

```bash
# Kernel 侧：建 workspace，发一个绑定该 workspace 的 runner token
curl -s -X POST http://127.0.0.1:8001/v1/admin/workspaces \
  -H 'X-Admin-Key: dev-admin' -H 'Content-Type: application/json' \
  -d '{"name":"calc","repo_path":"/abs/path/repo.git","main_branch":"main"}'

curl -s -X POST http://127.0.0.1:8001/v1/admin/tokens \
  -H 'X-Admin-Key: dev-admin' -H 'Content-Type: application/json' \
  -d '{"label":"runtime","workspace_id":"<kernel-workspace-id>"}'

# 生产部署先用 KERNEL_WORKSPACE_BINDINGS 登记产品工作区 UUID 与对应的
# Kernel workspace、URL、凭据引用，再用 OWNER/ADMIN 登录令牌绑定。
# 开发环境可用 KERNEL_WORKSPACE_ID 指定单个默认 Kernel workspace。
# URL/凭据还须在产品 API 和 Runtime Worker 的服务端白名单中。
curl -s -X POST http://127.0.0.1:8000/v1/projects \
  -H "Authorization: Bearer <owner session>" -H 'Content-Type: application/json' \
  -d '{"name":"calc","repo":"/abs/path/repo.git","description":"计算器"}'

curl -s -X POST http://127.0.0.1:8000/v1/projects/calc/repositories \
  -H "Authorization: Bearer <owner session>" -H 'Content-Type: application/json' \
  -d '{"repo_path":"/abs/path/repo.git","kernel_workspace_id":"<kernel-workspace-id>",
       "kernel_base_url":"http://127.0.0.1:8001"}'
```

之后任何带 `project=calc, repo=/abs/path/repo.git` 的任务都会路由到该 Kernel workspace。
一个 Kernel 可以承载多个 workspace；多实例需在产品 API 配置 `KERNEL_ADMIN_KEYS` 或
`KERNEL_ALLOWED_BASE_URLS`，并通过 `KERNEL_WORKSPACE_BINDINGS` 逐工作区登记目标。

---

## 5. 用户看到什么

唯一导航：**Diagram / Loop / Documents / Settings**。

* **Loop**：任务板（需要你 / 审查中 / 进行中 / 就绪 / 已关闭）。
* **任务详情**：目标、状态、计划、子任务、**Execution**（当前工具、步骤、等待原因）、
  Kernel 任务、提交、commit、门禁证据、交付记录、审批。
* **项目**：项目目标、仓库、任务与进度（`/v1/projects/*`，随任务一起呈现）。

Kernel/Runtime 控制台不再是产品入口：`/dashboard/code/`、`/dashboard/agent-runs/`、
`/dashboard/projects/` 一律 302 到 `/dashboard/`；详细视图只作为内部运维页面
`/dashboard/internal/*`，生产环境默认关闭（`INTERNAL_VIEWS_ENABLED=1` 才开启）。

---

## 6. HTTP API（产品面）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/tasks` | 创建产品任务（唯一任务身份） |
| POST | `/v1/tasks/{key}/run` | 启动该任务唯一的 Agent Run（`/agent-run` 为兼容别名） |
| GET | `/v1/tasks/{key}/run` | 读取 Run 并做一次幂等投影 |
| POST | `/v1/tasks/{key}/run/cancel` | 取消 Run 与任务 |
| GET | `/v1/tasks/{key}/execution` | 任务详情的统一执行视图（Loop/Code/Verification/Delivery） |
| GET/POST | `/v1/projects`、`/v1/projects/{p}/repositories` | 项目与仓库（Kernel workspace）绑定 |
| GET | `/v1/tasks/{key}/trace` | 一个 task_key → 三个平面的事实 + 合并时间线（运维诊断入口） |
| GET | `/healthz/deep` | 数据库 + Runtime + Kernel 的联合健康 |

Kernel 内部接口（用户不可见）：6 个 syscall、`/v2/syscalls/submit_patch`、
`/v2/submissions/*`、`/v1/runtime/*`（服务身份）、`/v1/runner/*`（runner 身份）。

---

## 7. 测试与验收

```bash
# Runtime（真实 Postgres）
TEST_DATABASE_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/agentgraph_test \
  python -m pytest agent_runtime/tests contracts/tests -q

# Kernel（真实 Postgres + Rust 可选）
cd veynod_kernel && python -m pytest tests -q

# 产品面（需要 pgvector）
cd AgentGraph && TEST_DATABASE_URL=... .venv/Scripts/python.exe -m pytest tests -q

# 全链路（真实 Kernel + Gate + Git + Runtime，模型为脚本化决策）
python scripts/e2e_unified.py
```

生产资格（本轮加固）见：
[PRODUCTION_QUALIFICATION_REPORT.md](docs/PRODUCTION_QUALIFICATION_REPORT.md)、
[CAPACITY_BASELINE.md](docs/CAPACITY_BASELINE.md)、
[CHAOS_TEST_MATRIX.md](docs/CHAOS_TEST_MATRIX.md)、
[SECURITY_QUALIFICATION_REPORT.md](docs/SECURITY_QUALIFICATION_REPORT.md)、
[PRODUCTION_OPERATIONS.md](docs/PRODUCTION_OPERATIONS.md)。

```bash
python scripts/chaos_suite.py            # 9 个真实故障场景（SIGKILL / 重启 / 断连）
python scripts/capacity_unified.py --levels 10,25,50,100 --profile fast
python scripts/verify_rust_core.py       # Rust 镜像里跑通完整门禁与合并
python scripts/restore_drill.py --source-prefix veynod_chaos_
python scripts/e2e_unified.py --real-model
```

本轮关键保证：Run 写入使用 lease epoch fencing（旧 Worker 无法覆盖新 Worker）；
Worker 在工具调用中死亡后 Run 可被接管恢复；一个任务在门禁中不会排队第二个提交；
用户取消会传播到 Kernel。多任务同仓库部署请设置 `VN_FAST_FORWARD_MERGE=true`
（树合并 + CAS，见 ADR-020）。

---

## 8. 目录

```text
veynod/
├── AgentGraph/        产品控制面（FastAPI + Next.js + MCP）
├── agent_runtime/     唯一 Agent 执行循环（API / Worker / KernelToolRuntime）
├── veynod_kernel/     确定性执行内核（Python API + Gate Worker + Rust core）
├── contracts/         跨服务 ID / 事件 / 错误契约
├── deploy/            统一部署（Dockerfile、环境模板、初始化脚本）
├── docs/              统一架构、审计、迁移、ADR、验收报告
├── scripts/           e2e_unified.py、migrate_all.sh、backfill_unified.py
├── docker-compose.yml 一套生产部署
└── veynod_platform/   DEPRECATED：早期第三产品原型，不再部署
```

---

## 9. 边界与状态

* **Kernel Runner CLI** 保留为开发/兼容工具；产品执行者只有 Agent Runtime（ADR-017）。
* **共享一个 PostgreSQL 集群、三个数据库**：不强行合并 schema，不做跨库外键。
* **历史数据**不删除：迁移只做 expand → migrate → contract，回滚方案见迁移计划。
* `POST /v1/code/runs` 语义已收敛为"建任务 + 启动 Run"；旧行为可用
  `CODE_ENGINE_LEGACY_DIRECT=1` 临时恢复一个版本。
