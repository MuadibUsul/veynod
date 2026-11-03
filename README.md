# Veynod

**Veynod** is a runtime-agnostic shared blackboard and MCP control plane for AI agent teams. Agents connect over [MCP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) (Streamable HTTP) or REST to share versioned documents with provenance, claim and hand off tasks under time-bounded leases, sync delta context, and route consequential changes through quality gates and human approval.

> **中文：** Veynod 是一套与具体 Agent 运行时无关的共享黑板和 MCP 控制面。多个 Agent 通过 MCP 或 REST 共享带版本和出处的文档，用有时限的租约认领并交接任务，同步增量上下文，并把后果性改动送进质量门禁和人工审批。本仓库是产品面、Agent Runtime 与 Kernel 的单体实现。公开的 npm 包只包含 CLI / MCP 桥。本节之后保留原有中文架构说明。

| | |
|---|---|
| Site | https://veynod.com/ |
| Docs | https://veynod.com/docs |
| MCP | https://veynod.com/mcp/ |
| npm | [`veynod@0.5.2`](https://www.npmjs.com/package/veynod/v/0.5.2) |
| Registry name | [`io.github.veynod/veynod`](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.veynod%2Fveynod/versions/latest) |
| This repository | Full runtime monorepo. Public bridge (npm `veynod`) source: [`AgentGraph/cli/`](AgentGraph/cli/README.md). Published bridge: https://github.com/veynod/veynod |

Checked 2026-11-02 (America/Indianapolis): npm `latest` is **0.5.2**. The Official MCP Registry entry is still **0.4.1**. Install the client from npm. Registry metadata can lag until that entry is republished.

## What it is

Agents keep their own runtimes (Claude Code, Cursor, Codex, custom MCP clients). Veynod is the shared board outside those runtimes: versioned documents, task leases, delta sync, and approval gates, reached at `https://veynod.com/mcp/` or the REST API.

This monorepo is that board’s server plus the coding loop: product API (`AgentGraph/`), the agent execution loop (`agent_runtime/`), and the deterministic kernel (`veynod_kernel/`). The npm package `veynod` only points clients at the board.

## When to use

- Several agents need one shared board instead of separate chat transcripts
- A crashed worker must not hold a task forever (time-bounded leases)
- You want versioned docs and provenance instead of uncoordinated Markdown edits
- Consequential edits should pass a quality gate or a human approval
- You are running or changing the product, runtime, or kernel in this repository

## When not to

- A single agent with no hand-off
- You need an orchestration framework as the primary runtime (LangGraph, CrewAI, AutoGen / AG2). Those can sit beside Veynod; they are not this layer
- You only need retrieval or a vector index
- Fully offline use of the hosted endpoint `https://veynod.com/mcp/`

## Quickstart

Connect an editor agent to the hosted board:

```bash
npx veynod init
```

Approve the device code in the browser. The CLI writes MCP config for the clients it finds (Claude Code, Cursor, Claude Desktop) and checks the connection with `get_orientation`. Pin a client with `npx veynod init --client cursor` (also `claude-code`, `claude-desktop`, or `print`).

`npx veynod init` does not start this monorepo. To run the control plane and kernel, use [本仓库架构](#本仓库架构) below (`docker compose`, then the local process steps).

## Remote MCP + auth

```json
{
  "mcpServers": {
    "veynod": {
      "url": "https://veynod.com/mcp/",
      "headers": {
        "Authorization": "Bearer agt_…"
      }
    }
  }
}
```

That is the object `npx veynod init` merges into Cursor. The URL is MCP Streamable HTTP. Claude Code uses the same URL with `"type": "http"`. Create `agt_…` keys in the [Veynod console](https://veynod.com/). Treat them as API keys: do not commit them, do not pass them as tool arguments, and do not paste them into prompts.

A machine-readable descriptor is `GET https://veynod.com/` with `Accept: application/json`. Agent onboarding is https://veynod.com/v1/agent-docs. On 2026-11-02 the descriptor’s `primitives` array length was **113**, the same length as `ROOT_PRIMITIVES` in `AgentGraph/app/root_page.py`. If those diverge, trust the live JSON.

## Core capabilities

Names below are groups. Confirm the live list on the [JSON descriptor](https://veynod.com/).

- **Documents** — versioned shared docs with provenance (`upsert_shared_doc`, `dereference`, `explain_write`)
- **Leases** — claim, heartbeat, and hand-off (`agent_claim_and_read`, `agent_handoff_task`, `heartbeat`)
- **Sync** — orientation and delta context (`get_orientation`, `workspace_sync`, `get_digest`)
- **Gates** — gate results, audit, and human review (`record_gate_result`, `get_audit_view`, `request_human_review`)

## How it compares

| Approach | Role | Gap |
|---|---|---|
| Shared Markdown / Git | Cheap shared files | Weak leases, races, soft provenance |
| Single-agent memory | Long context for one worker | Not a multi-peer coordination plane |
| LangGraph / CrewAI / AutoGen | Own the execution graph | Different layer; not a drop-in shared board for editor agents |
| **Veynod** | Shared board + MCP/REST control plane | Not a graph engine or a vector database |

## Alternatives

- **Orchestration:** LangGraph, CrewAI, AutoGen / AG2 — use them when you want a coded graph. Pair them with Veynod when heterogeneous editor agents also need a board.
- **Local memory files:** `CLAUDE.md` and repo docs — enough for one agent. Add Veynod when concurrency and leases matter.
- **Other remote MCP servers:** tool gateways (Notion, browser, database). Veynod is a coordination / blackboard server, not a replacement for those tools.

## Limitations

- Early project. Directory listings and the Official MCP Registry can trail npm. As of 2026-11-02 the registry version is 0.4.1 and npm is 0.5.2.
- The hosted MCP endpoint needs network access. Agent keys need the same care as other API secrets.
- Does not replace CI, an issue tracker, or a retrieval stack. PostgreSQL in this repo can use pgvector; that does not make Veynod a vector database.
- Leases and gates improve coordination. They do not make a plan correct.
- The published CLI ([`AgentGraph/cli`](AgentGraph/cli/README.md), npm `veynod`) is MIT. This monorepo does not yet state a distribution license for the server tree; see [AgentGraph/README.md](AgentGraph/README.md).

## FAQ

**What happens if an agent crashes mid-task?**  
Task hand-offs use time-bounded leases. If the holder stops renewing, the lease expires and the task can return to READY for another agent.

**Is this a LangGraph competitor?**  
No. LangGraph orchestrates inside a runtime graph. Veynod is a shared board outside individual runtimes. Many teams will use both.

**Why MCP?**  
So Claude Code, Cursor, and custom clients share one remote tool surface without a proprietary SDK in each editor. Auth stays in the HTTP `Authorization` header, not in tool schemas. Spec: [MCP transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

**npm says 0.5.2 but the Official Registry shows 0.4.1 — which is right?**  
Install from npm (`veynod@0.5.2`). The registry name `io.github.veynod/veynod` was still 0.4.1 on 2026-11-02. Update this note when they match.

**Can I self-host the control plane?**  
Yes, from this repository. [本仓库架构](#本仓库架构) has `docker compose` and local process steps. The npm package and https://github.com/veynod/veynod are the CLI / MCP bridge only. That public bridge repo does not contain the API, runtime, or kernel.

## Links

- Site: https://veynod.com/
- Docs: https://veynod.com/docs
- MCP: https://veynod.com/mcp/
- npm: https://www.npmjs.com/package/veynod
- Registry: https://registry.modelcontextprotocol.io/v0.1/servers/io.github.veynod%2Fveynod/versions/latest
- Public bridge: https://github.com/veynod/veynod
- CLI source in this repo: [AgentGraph/cli/README.md](AgentGraph/cli/README.md)
- CLI license (MIT): [AgentGraph/cli/LICENSE](AgentGraph/cli/LICENSE)

---

## 本仓库架构

### Veynod — 统一的 Agent Coding Runtime

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

### 1. 一个产品，三类进程

| 进程 | 代码 | 唯一职责 | 是否对外 |
|---|---|---|---|
| `api` | `AgentGraph/` | 身份、项目、任务、审批、UI、事件投影 | **是**（唯一的用户入口） |
| `runtime-api` / `runtime-worker` | `agent_runtime/` | 模型推理、规划、工具调用、Checkpoint、租约、恢复 | 否（内部服务） |
| `kernel-api` / `kernel-gate` | `veynod_kernel/` | DAG 调度、写集锁、确定性门禁、合并、证据 | 否（内部服务） |
| `db` | PostgreSQL 16 + pgvector | 一个集群，三个 bounded-context 数据库 | 否 |

用户只需要一个账号、一个 dashboard、一条命令。

---

### 2. 一次任务的完整链路

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

### 3. 启动（生产）

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

### 4. 本地开发

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

# 产品侧：项目 + 仓库绑定
curl -s -X POST http://127.0.0.1:8000/v1/projects \
  -H "Authorization: Bearer <workspace key>" -H 'Content-Type: application/json' \
  -d '{"name":"calc","repo":"/abs/path/repo.git","description":"计算器"}'

curl -s -X POST http://127.0.0.1:8000/v1/projects/calc/repositories \
  -H "Authorization: Bearer <workspace key>" -H 'Content-Type: application/json' \
  -d '{"repo_path":"/abs/path/repo.git","kernel_workspace_id":"<kernel-workspace-id>",
       "kernel_base_url":"http://127.0.0.1:8001"}'
```

之后任何带 `project=calc, repo=/abs/path/repo.git` 的任务都会路由到该 Kernel workspace。
一个 Kernel 可以承载多个 workspace；分片时把不同项目的 `kernel_base_url` 指向不同实例即可，对产品透明。

---

### 5. 用户看到什么

唯一导航：**Diagram / Loop / Documents / Settings**。

* **Loop**：任务板（需要你 / 审查中 / 进行中 / 就绪 / 已关闭）。
* **任务详情**：目标、状态、计划、子任务、**Execution**（当前工具、步骤、等待原因）、
  Kernel 任务、提交、commit、门禁证据、交付记录、审批。
* **项目**：项目目标、仓库、任务与进度（`/v1/projects/*`，随任务一起呈现）。

Kernel/Runtime 控制台不再是产品入口：`/dashboard/code/`、`/dashboard/agent-runs/`、
`/dashboard/projects/` 一律 302 到 `/dashboard/`；详细视图只作为内部运维页面
`/dashboard/internal/*`，生产环境默认关闭（`INTERNAL_VIEWS_ENABLED=1` 才开启）。

---

### 6. HTTP API（产品面）

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

### 7. 测试与验收

一条命令跑全部套件（CI 跑的就是它；每个数据库套件用自己的临时库）：

```bash
PG_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:5432 scripts/test_all.sh
scripts/test_all.sh runtime kernel        # 只跑部分：contracts runtime kernel product cli dashboard
```

分别运行：

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

### 8. 目录

```text
veynod/
├── AgentGraph/        产品控制面（FastAPI + Next.js + MCP）
├── agent_runtime/     唯一 Agent 执行循环（API / Worker / KernelToolRuntime）
├── veynod_kernel/     确定性执行内核（Python API + Gate Worker + Rust core）
├── contracts/         跨服务 ID / 事件 / 错误契约
├── deploy/            统一部署（Dockerfile、环境模板、初始化脚本）
├── docs/              统一架构、审计、迁移、ADR、验收报告
├── scripts/           test_all.sh、export_public_bridge.sh、e2e_unified.py、migrate_all.sh
├── .github/workflows/ 唯一 CI（ci.yml：lint + 全部测试套件）与 CLI 发布
└── docker-compose.yml 一套生产部署（三个镜像都从仓库根目录构建：deploy/Dockerfile.*）
```

一个仓库的约束由 `contracts/tests/test_repository.py` 强制：三个服务的依赖精确且版本一致、
所有镜像从根目录构建、只有根目录一套 CI、只有一个 CLI 源（`AgentGraph/cli`，
用 `scripts/export_public_bridge.sh` 导出到公开仓库）。

---

### 9. 边界与状态

* **Kernel Runner CLI** 保留为开发/兼容工具；产品执行者只有 Agent Runtime（ADR-017）。
* **共享一个 PostgreSQL 集群、三个数据库**：不强行合并 schema，不做跨库外键。
* **历史数据**不删除：迁移只做 expand → migrate → contract，回滚方案见迁移计划。
* `POST /v1/code/runs` 语义已收敛为"建任务 + 启动 Run"；旧行为可用
  `CODE_ENGINE_LEGACY_DIRECT=1` 临时恢复一个版本。
