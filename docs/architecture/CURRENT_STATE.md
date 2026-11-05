# CURRENT_STATE — Veynod Phase 0 现状审计

> 审计日期：2026-10-31（本地时区 UTC-4；部分后台记录为 2026-11-01 UTC）
> 审计范围：`E:\Codes\veynod` 全部源码、迁移、测试、脚本、运行数据
> 审计性质：只读盘点和可执行验证；除用户明确指令的 Kernel 目录替换/重启外，未修改源码与原有文档
> 证据等级：VERIFIED / IMPLEMENTED_NOT_VERIFIED / PARTIAL / DOCUMENTED_ONLY / MISSING / UNKNOWN
>
> **Phase 1/2 update（2026-10-31）：** 本文件是 Phase 0 基线。Phase 1A–1J 已修复多项 P0/P1；Phase 2A–2L 新增 Runtime 持久化、Loop、迁移/API、Worker/租约/Pool、HTTP Model/Tool、Context、委派、配额/路由/对账、AgentGraph 接入。详见 `PHASE1A_REPORT.md`～`PHASE1J_REPORT.md`、`PHASE2A_REPORT.md`～`PHASE2L_REPORT.md`。下文未逐条改写的“MISSING/PARTIAL”以 Phase 0 当时为准。

---

## 0. 一页结论

Veynod 当前是一个**可运行的百级代码协作引擎原型**，不是已经通过生产验收的 Agent Coding Runtime：

- AgentGraph 是唯一产品控制面，负责身份、租户、workspace、产品任务、审批、审计、SSE 和人类控制台。
- Veynod Kernel 是 headless 代码执行引擎，负责仓库、符号级写集、DAG 调度、租约、门禁、合并和 Git commit。
- 两者通过 `AgentGraph/app/services/code_engine.py` 直连；Kernel 没有独立产品登录入口。
- 代码路径是真实的，但当前存在**两条并行的任务路径**：AgentGraph 的 Runner/Loop 任务路径，以及 Kernel 的 code run 路径；后者依赖 `input_context` 映射，且没有自动化测试。
- 100 个任务可以全部 COMMITTED，但当前 1,000ms 提交受理 P99 验收**未通过**；500 个任务也能全部 COMMITTED，但 P99/传输预算同样未通过；1,000 级生产 profile 没有通过记录。
- Kernel 没有 CANCELLED 状态和取消 API；AgentGraph 的取消只改自己的任务表，不会停止 Kernel 的 gate/merge/sandbox。**（Phase 1B 已加入 Kernel CANCELLED/取消 API/传播；仍缺 in-flight sandbox 主动终止和事件投影。）**
- Kernel 没有租户模型；任何有效 runner token 可读任意 workspace/task；`/v1/dag*` 与 `/dashboard` 未鉴权。
- 默认 `local` gate 不是安全沙箱，会在 Kernel 主机上执行 Agent 代码；默认 admin key 和 `0.0.0.0` 文档组合构成高风险链路。
- 平台没有 Project / Repository / CodingSession 一等实体；`project`/`repo` 只是任务上的自由文本。
- 平台没有持久化、平台自有的模型驱动 Agent Loop；模型调用发生在外部 runner 进程中。

**Phase 0 审计交付完成，但“单写入权威、单状态机、可取消、可恢复”的架构门槛未满足。** 不建议直接进入 Phase 2+ 大规模重构；应先完成 Phase 1 契约/身份基础，并修复本文件列出的 P0/P1 风险。

---

## 1. 审计方法与边界

### 1.1 已执行的验证

| 验证 | 命令/方式 | 结果 |
|---|---|---|
| AgentGraph 非集成测试 | `AgentGraph/.venv/Scripts/python.exe -m pytest -q -m "not integration"` | **68 passed, 28 skipped, 448 deselected** |
| Kernel 非集成测试 | `python -m pytest -q -m "not integration"` | **79 passed** |
| Kernel 选定集成测试 | `TEST_DATABASE_URL=...veynod_test pytest tests/test_admission.py tests/test_work_leases.py tests/test_merge_coordinator.py tests/test_edge_outbox.py` | **6 passed** |
| Rust Core 测试 | `cargo test --release` | **14 passed, 1 ignored**（1M 节点测试需 `--ignored`） |
| 2-task 生产 profile | `bench_swarm --profile production --files 2` | **通过**，总耗时 8.2s，P99 预算通过 |
| 100-runner 生产 profile | `bench_swarm --profile production --files 100 --runners 100 --sse-streams 100` | 100/100 COMMITTED，总耗时 126.5s；**验收失败：submit accept P99 = 8015.51ms > 1000ms** |
| 500-runner 生产 profile | 500/500 COMMITTED，657.7s；**验收失败：submit accept P99 = 60,156.82ms；claim transport 3.31%；submit transport/retry 14.24%** | 见 `CAPACITY_BASELINE.md` §4.3 |
| 现场路由/数据库 | `curl :8001/openapi.json`、`psql pg_tables` | 最新 Kernel：28 条路由、15 张产品表 + `_commit_probe`；v2 路由存在 |

### 1.2 未执行的验证

- 1,000-runner 当前源码复测（历史产物已存在，见容量文档；耗时约 35 分钟，未在本轮完成）。
- AgentGraph 全量集成测试：本机 `veynod_test` 缺少 `pgvector`，448 个集成测试被 deselected；没有使用生产 AgentGraph 数据库做破坏性测试。
- Docker sandbox 端到端：仓库没有 gate 容器镜像和 Docker sandbox 测试；本轮验证使用 `local`。
- 生产环境、真实多租户、多仓库、故障注入集群：没有相应基础设施。
- 浏览器级 UI 黑盒验收：本轮是架构审计，不做产品 UI 验收。

### 1.3 重要说明

本文件中的“当前”以仓库源码和本轮实测为准，不把任何文档描述当作实现事实。历史容量数据只作为历史证据，不等于当前 SLA。

---

## 2. 仓库与代码规模

| 区域 | 规模（本轮统计） |
|---|---:|
| AgentGraph Python | 139 个文件，70,187 行 |
| AgentGraph Dashboard | 47 个 TS/TSX/CSS 文件，27,302 行 |
| AgentGraph 测试 | 93 个测试文件；`pytest --collect-only` 544 项（448 integration / 96 非集成） |
| AgentGraph migrations | 69 个 |
| AgentGraph ORM 表 | 48 个 `__tablename__` |
| Kernel Python | 40 个文件，12,857 行 |
| Kernel Rust | 5 个源文件，约 2,346 行；15 个测试（14 默认 + 1 ignored） |
| Kernel 测试 | 19 个测试文件，105 项（79 非集成 / 26 集成） |
| Kernel migrations | `v4_001` genesis + `v4_002`–`v4_008` 共 8 个文件 |
| Kernel 产品表 | 15 张 + 1 张测试探针表 `_commit_probe` |
| veynod_platform | 7 个 Python 文件，约 952 行；已退出主链路 |

> 行数只用于量级判断。AgentGraph 的 93 个测试文件不等于 93 个有效端到端路径；Kernel 的 105 项测试中 26 项依赖真实 PostgreSQL。

---

## 3. 当前运行拓扑

### 3.1 本轮替换后的现场

```text
Browser / Agent
      │
      ▼
AgentGraph API :8000（Docker，3.24.0）
  ├─ /v1/*、/v1/human/*、/mcp
  ├─ /v1/code/*  ── code_engine ──┐
  ├─ /dashboard/*（Next.js 静态导出）
  └─ workspace_events / SSE / inbox
                                  │ HTTP + X-Admin-Key
                                  ▼
Veynod Kernel API :8001（最新副本，4.0.0）
  ├─ /v1/admin/*
  ├─ /v1/syscalls/*
  ├─ /v2/syscalls/submit_patch
  ├─ /v2/submissions/*
  ├─ /v1/runner/*、/v1/events/stream
  └─ /v1/dag*、/dashboard（未鉴权）
                                  │
                                  ▼
PostgreSQL 16.15 :55432 / veynod_dev
  ├─ workspaces / code_artifacts / dag_tasks / task_dependencies
  ├─ patch_submissions / gate_work_leases / merge_intents
  ├─ admission_permits / scheduler_authorities / event_outbox
  └─ kernel_events / symbol_edges / agent_tokens / schema_migrations
                                  │
                                  ▼
Kernel Gate Worker（独立进程，:8030 指标）
  ├─ GateWorker × 8
  ├─ MergeCoordinator
  ├─ pyright / ruff / pytest
  ├─ Git worktree / update-ref
  └─ outbox / edge rebuild / worktree pool
```

### 3.2 旧进程与替换记录

审计开始时，:8001 上运行的是 **2026-10-31 10:51:53** 启动的旧进程：

- OpenAPI 只有 **23** 条路径，没有 `/v2/*`；
- `veynod_dev` 只有 genesis 的 8 张表，没有 `patch_submissions`、`gate_work_leases`、`merge_intents`、`admission_permits`、`event_outbox`、`scheduler_authorities`、`schema_migrations`；
- 没有 `app.worker` 进程在 8030 上；
- 因此旧进程即使收到 v2 请求也会 404，且 v2 提交不会被门禁处理。

按用户明确指令，本轮执行了以下**运行环境变更**（不属于源码重构）：

1. 停止旧 :8001 进程；
2. 将旧目录整体移动到项目外备份：`E:\Codes\backup_veynod_kernel_20261031-2230`；
3. 从 `E:\Codes\veynod_kernel` 复制最新副本到 `E:\Codes\veynod\veynod_kernel`（两份源码递归对比为 **0 差异**，排除 `.run`/缓存/`target`）；
4. 用最新副本重启 Kernel API：**28** 条路由，含 `/v2/syscalls/submit_patch`、`/v2/submissions/*`；
5. 启动独立 Gate Worker（8 workers，:8030）；
6. 自动应用迁移：`schema_migrations` 记录 `v4_002`–`v4_005`；`v4_006`/`v4_007`/`v4_008` 未写入版本表但实际建表/索引已生效（见数据文档的迁移缺陷）。

替换后现场：Kernel health 返回 `4.0.0`，Rust core available；`/v2/syscalls/submit_patch` 无 token 返回 401（不再是 404）；AgentGraph :8000 仍为 3.24.0。

### 3.3 AgentGraph 进程内后台循环

`AgentGraph/app/main.py:100-220` 在 lifespan 中启动：事件总线、租约回收、webhook 投递、外部动作、索引、access log、token usage、situation、mailer、incident SLA、blob GC、presence、scheduler。AgentGraph 是**单 FastAPI 进程**，没有 1,000 活跃 Agent 的控制面压测证据。

### 3.4 Kernel 进程内后台循环

`veynod_kernel/app/runtime.py:177-210` 在 API lifespan 中启动：schema/migration、bootstrap token、workspace HEAD 对账、Rust core 预热、lease reaper、scheduler event sync、edge rebuild、outbox。Gate/Merge 不在 API 进程，必须在 `app.worker` 中运行。

---

## 4. 能力现状矩阵

### 4.1 产品控制面

| 能力 | 状态 | 证据与说明 |
|---|---|---|
| 人类账号、会话、OAuth | VERIFIED | `AgentGraph/app/auth.py`、`app/services/accounts.py`、`app/api/auth_routes.py`；有人类会话和 token 哈希 |
| Tenant / Workspace / Membership / RBAC | VERIFIED（AgentGraph 内） | `app/models.py`、`app/tenant.py`、RLS 迁移；跨租户 fuzz 测试 `tests/test_tenants.py` |
| 产品任务 `collab_tasks` | VERIFIED | 11 状态，claim/handoff/review/rework/escalate/cancel；但状态写入分散在 9 个模块 |
| 审批 / 人类主权 | VERIFIED | `AWAITING_HUMAN`/`ESCALATED_TO_HUMAN`、`WAITING_HUMAN`、human-only DONE；`tests/test_security_hardening.py` |
| 审计 | PARTIAL | `state_audit_diffs`/`audits` 有记录，但无 append-only 触发器；DSAR 可重写事件 payload |
| 文档 / 黑板 / 状态槽 | VERIFIED | `shared_documents`、`context_slots`、版本、文档 chunk；有 113 个 MCP 工具 |
| 事件 / SSE / inbox | PARTIAL | `workspace_events` 由触发器在同一事务写入；poll 至少一次、可恢复；SSE 使用 `row.id` 当 `seq`，与 poll 的身份不一致，且有 500 条重放上限 |
| Webhook | VERIFIED with gap | HMAC、幂等键、重试、DEAD；没有 DEAD 重放 API |
| 预算 / 计量 | PARTIAL | `workspace_budgets`、`usage_ledger`；未证明对所有模型调用强制执行 |
| Runner / Loop 放置 | VERIFIED | `runner_hosts`、`runner_placements`、`app/services/scheduler.py`；CLI runner 真实执行外部模型进程 |
| 持久化模型驱动 Agent Loop | MISSING | 没有 model gateway、checkpoint、tool invocation 持久化；只有任务 continuation 文档和 24h MCP 幂等记录 |
| Project / Repository / CodingSession | MISSING | 没有一等实体；`project`/`repo` 是 `collab_tasks` 自由文本字段 |
| Code 页面 | PARTIAL | FastAPI 静态页 `/dashboard/code/`；只有创建/列表/JSON 详情，没有 diff review、execution graph、delivery approval |

### 4.2 Kernel 执行面

| 能力 | 状态 | 证据与说明 |
|---|---|---|
| Repository / Workspace / Artifact | VERIFIED（单 workspace 语义） | `workspaces.repo_path`、`code_artifacts`；一个 Kernel 进程默认一个 workspace |
| DAG 任务与依赖 | VERIFIED | `dag_tasks`、`task_dependencies`、`TaskStatus` 9 状态 |
| 符号级写集锁 | VERIFIED | Rust `locks.rs`/`scheduler_core.rs` + SQL GIN 重叠检查；`tests/test_lock_algebra.py`、`test_integration.py` |
| 乘性调度 / READY 队列 | VERIFIED（Rust 可选） | Rust core 决策 + PostgreSQL 单行 fence；SQL fallback 可用 |
| 准入 / 429 / Retry-After | VERIFIED | `admission_permits`、`pg_try_advisory_xact_lock`、队列深度；`tests/test_admission.py` |
| Lease / Fencing（claim/work） | PARTIAL | claim/work lease 有 fencing；heartbeat 未检查过期；SPLIT/ESCALATE/INJECT 只检查 status；merge 不复核 task lease |
| v1 同步提交 | DEPRECATED | 默认 410；`VN_LEGACY_SUBMIT_MODE=sync` 可在 API 进程内跑 gate/merge |
| v2 异步提交 | VERIFIED（本机最新实例） | 202 + `patch_submissions` + 独立 worker；幂等键绑定 task/lease/patch SHA；`submission_service.py` |
| Gate Worker | VERIFIED（local） | 8 workers、work lease、heartbeat、fencing、重试；Docker sandbox 未验证 |
| 四阶段门禁 | VERIFIED（AST/契约/测试）；静态检查未在测试中执行 | `verification_gate.py:447-634`；测试普遍设 `VN_SKIP_TYPECHECK/LINT=true` |
| Merge Coordinator | VERIFIED（CAS/串行化/对账） | `git update-ref <new> <old>`、per-workspace coordinator lease、Git-first/DB-first reconcile；`tests/test_merge_coordinator.py` |
| 多仓库 | MISSING | syscalls 不带 workspace_id；默认/最老 workspace；无 Repository Registry/Cell |
| 多租户 | PARTIAL（Phase 1C） | token 可绑定 workspace；runner 读面/SSE 拒绝跨 workspace；`/v1/dag*` 需 admin key；dashboard 默认关闭；syscall 仍单 workspace，旧 token 未绑定 |
| 取消 | PARTIAL（Phase 1B） | Kernel CANCELLED + cancel API + 下游/submission/intent 终止 + merge fence；AgentGraph 传播；仍缺 in-flight sandbox 主动终止、取消事件投影 |
| 事件 / Outbox | PARTIAL | `kernel_events` + `event_outbox` + pg_notify；仓库内没有 LISTEN consumer；无 DLQ/retention/correlation ID |
| Rust 权威性 | PARTIAL | Rust 是可选决策路径；`scheduler_authorities.fencing_token` 不 fence 任何写；SQL fallback 是一等路径 |

### 4.3 旧路径 / 重复路径

| 路径 | 结论 |
|---|---|
| AgentGraph `diagrams` / chunk 队列 / merge 记录 | 只编排任务和存文件/CI 元数据，不执行代码；与 Kernel code run 并存 |
| AgentGraph `deploy_gate`（v1） | 2026-11-05 删除；部署走 v2（`deploys.py`，docs/DEPLOYS.md） |
| AgentGraph `flow` | 只读投影，不编排 |
| AgentGraph `code_engine` → Kernel | 真实代码执行路径，但**未跟踪、零测试**、gate 证据是合成的 |
| Kernel v1 sync submit | 过渡路径，API 进程内跑 gate/merge，默认 410 |
| Kernel v2 async submit | 目标路径，独立 worker；本轮已在新实例启用 |
| `veynod_platform` | 已退出主链路；AgentGraph/Kernel 无引用；只剩 smoke 脚本和残留命名 |

---

## 5. 关键集成链路（当前真实行为）

### 5.1 Code run 创建

```text
POST AgentGraph /v1/code/runs
  -> require task:create
  -> 校验 repo_path == KERNEL_REPO_PATH
  -> Kernel POST /v1/admin/artifacts（stub + test，FROZEN）
  -> Kernel POST /v1/admin/tasks（IMPLEMENT_BODY）
  -> AgentGraph create collab_task:
       status READY
       input_context.code_engine = "veynod-kernel"
       input_context.kernel = {workspace_id, task_id, base_url}
```

证据：`AgentGraph/app/services/code_engine.py:135-211`、`AgentGraph/app/api/code_routes.py`。

### 5.2 Kernel 执行

```text
Runner POST /v1/syscalls/pull_task_bundle
  -> Kernel claim: READY -> LEASED，写 base_commit_sha、lease_token_hash、lease_expires_at
  -> Runner 在 worktree 改代码
  -> Runner POST /v2/syscalls/submit_patch（202）
  -> patch_submissions QUEUED
  -> Gate Worker claim: QUEUED -> RUNNING，work_lease_id
  -> 四阶段门禁：写集边界 / AST 范围 / 契约哈希 / 静态检查 / 测试
  -> merge_intents PENDING
  -> Merge Coordinator：per-workspace lease + git update-ref CAS
  -> dag_task VERIFYING -> COMMITTED；workspace head 更新；下游 in_degree 递减
  -> Runner GET /v2/submissions/{id} 轮询终态
```

证据：`veynod_kernel/app/engine/scheduler.py`、`app/services/submission_service.py`、`app/services/gate_worker.py`、`app/services/verification_gate.py`、`app/services/merge_coordinator.py`。

### 5.3 AgentGraph 状态同步

```text
GET AgentGraph /v1/code/runs/{task}
  -> 读 Kernel task
  -> 仅当 Kernel status == COMMITTED 且 AgentGraph task 未在 REVIEWING/HANDOFF/AWAITING_HUMAN/DONE
  -> quality.record_gate_result(CI PASS, TESTS PASS)  # 硬编码 counts {passed:4, failed:0}
  -> claim AgentGraph task
  -> handoff REVIEWING
```

证据：`AgentGraph/app/services/code_engine.py:273-330`。

**关键限制：**

- Kernel 运行期间 AgentGraph task 一直是 `READY`；
- 只有 COMMITTED 才同步；失败、取消、重试、Gate 失败不会回写；
- CI/TESTS PASS 不是读取 Kernel `gate_results`，而是状态推导 + 硬编码；
- AgentGraph cancel 不通知 Kernel；
- code_engine 没有测试。

### 5.4 两套 Runner

| Runner | 执行什么 | 与 Kernel 关系 |
|---|---|---|
| AgentGraph CLI `cli/bin/runner.mjs` | 领取 AgentGraph placement，启动 claude/codex/自定义命令，在 worktree 工作，报告 finished/failed | 不直接调用 Kernel；不处理 code_engine |
| Kernel CLI `cli/bin/runner.mjs` | claim Kernel task，创建 worktree，启动模型，提交 v2 patch，轮询 submission | 直接使用 Kernel；默认 API URL 是 `127.0.0.1:8000`（文档/部署易错） |

---

## 6. 测试与容量事实

### 6.1 本轮测试

- AgentGraph 非集成：68 passed / 28 skipped / 448 deselected。28 skip 主要来自 Windows 不支持的 deploy-gate-runner 测试。
- Kernel 非集成：79 passed。
- Kernel 选定集成：6 passed（admission、work leases、merge coordinator、edge outbox）。
- Rust：14 passed，1 ignored。
- **没有跑的**：AgentGraph 448 个集成测试（缺 pgvector）、Kernel 其余 20 个集成测试、1,000-runner 复测、Docker sandbox。

### 6.2 容量结论（详见 `CAPACITY_BASELINE.md`）

- 2-task production smoke：通过（8.2s）。
- 100-task production：100/100 COMMITTED，126.5s；**accept P99 8015.51ms，超过 1000ms 预算，验收失败**。
- 本轮 500-task production：500/500 COMMITTED，657.7s；但 accept P99 60.2s、claim transport 3.31%、submit transport/retry 14.24%，**验收失败**。
- 历史 500：478/500，P99 64.5s，未通过（历史窗口/配置与本轮不同）。
- 历史 1,000 production：881/1,000，119 非终态，accept P99 69.5s，未通过。
- 历史 1,000 dev：1000/1000 但 22.7% claim / 14.4% submit 传输错误。
- **当前真实结论：100/500 任务能完成，但当前验收标准下没有通过的 100/500/1,000 生产 profile；不能宣称 1,000 Runner 生产 SLA。**

### 6.3 指标口径

必须区分：

- Connected Agents：SSE/连接数；
- Active Agent Runs：真正在执行 Agent Loop 的 Run；
- Concurrent Executions：正在执行工具/代码的任务；
- Concurrent Repository Mutations：正在修改同一 Repository 的任务；
- Concurrent Gate Jobs；
- Concurrent Merges。

当前 benchmark 的 `runners` 是客户端并发 worker 数，`sse-streams` 是连接数；1,000 SSE 建立不等于 1,000 个活跃 Agent。

---

## 7. 主要发现（按严重度）

### P0 — 安全与正确性阻断

1. **merge 不复核 task lease，可能在任务租约过期/被重排后仍推进 Git。**
   Merge Coordinator 只续自己的 workspace lease（`merge_coordinator.py:262-266`）；`merge_verified` 不检查 task 的 `VERIFYING`/`lease_token_hash`（`verification_gate.py:414-444`）；`on_task_committed` 要求 `VERIFYING`（`scheduler.py:629-631`）。若 Git 已推进但 DB 未更新，任务/下游状态可能与 main 分叉，且现有 reconcile 不修复这个方向。
   分类：VERIFIED 风险；修复前不应开放生产 merge。

2. **AgentGraph 的 gate 证据是合成的。**
   `code_engine._sync_lifecycle` 只根据 Kernel status==COMMITTED 写 CI/TESTS PASS，`counts` 硬编码 `{passed:4, failed:0}`、`gates_passed:2`，不读取 Kernel `gate_results`（`code_engine.py:285-302`）。这违反“Gate 结果可追溯”和“不能把状态当证据”。
   分类：VERIFIED 缺陷。

3. **Kernel 无租户模型、无取消。**
   `agent_tokens` 无 workspace 列（`models.py:256-271`）；任意有效 runner 可读任意 task（`runner_routes.py:316-337`）、订阅任意 workspace（`runner_routes.py:85-138`）；`/v1/dag*`、`/dashboard` 未鉴权（`dag_routes.py:20`）。`TaskStatus` 没有 CANCELLED（`models.py:54-63`），无 cancel API；AgentGraph cancel 只改自己的表（`tasks.py:1687-1759`）。
   分类：MISSING / PARTIAL。

4. **默认 local gate 是主机执行，不是沙箱。**
   `gate_sandbox` 默认 `local`（`config.py:294-316`），用 shell 在 Kernel 主机上跑 pyright/ruff/pytest；无网络/文件系统隔离；AgentGraph 默认 `KERNEL_ADMIN_KEY=dev-admin`（`AgentGraph/app/config.py:36`），Kernel `.env.example` 默认 `change-me`，文档示例 binding `0.0.0.0`。组合风险：猜到默认 admin key → 创建 workspace → 在主机执行代码。
   分类：VERIFIED 配置风险；Docker 模式 IMPLEMENTED_NOT_VERIFIED。

### P1 — 架构与交付阻断

5. **没有单一任务状态机，也没有单写入权威。**
   AgentGraph `collab_tasks.status` 由 `tasks.py`、`leases.py`、`collab.py`、`scheduler.py`、`reviews.py`、`diagrams.py`、`audit.py`、`quality.py`、admin 路由等 9+ 个模块写；DB 只约束 IN_PROGRESS lease / REVIEWING brief / DONE-FAILED 不可变，不校验合法转换，CANCELLED 不是终态。AgentGraph cancel 写 `FAILED`，子任务/图表路径写 `CANCELLED`。
   分类：PARTIAL；Phase 1 必须先建立转换矩阵与单一命令入口。

6. **Kernel 状态机有不可达/缺失状态。**
   `SUSPENDED` 无代码写入（enum + 文档残留）；无 CANCELLED；上游 FAILED 不会递减下游 in_degree，下游可能永久 BLOCKED；heartbeat 可复活已过期但未被 reaper 的 lease（`scheduler.py:723-745` 无 `lease_expires_at > NOW()`）。
   分类：PARTIAL / MISSING。

7. **平台没有 Project/Repository/CodingSession 实体，也没有持久化 Agent Loop。**
   AgentGraph 顶层是 Workspace；`project`/`repo` 是 `collab_tasks` 自由文本（`models.py:573-574`）。没有 model gateway/checkpoint/tool invocation 持久化；模型调用在外部 runner 进程（`cli/bin/runner.mjs`）。
   分类：MISSING。

8. **Code run 路径未跟踪、零测试，且与 AgentGraph 旧路径并存。**
   `code_engine.py`、`code_routes.py`、`code_page.py`、`code.html` 在 AgentGraph git 中是 `??` 未跟踪文件；`tests/` 无任何 `/v1/code` 或 `code_engine` 引用。AgentGraph 仍有 diagrams/chunk/merge queue、deploy gate、flow 等路径。
   分类：IMPLEMENTED_NOT_VERIFIED / PARTIAL。

9. **Kernel 多仓库/多租户/Cell 未实现。**
   syscalls 不带 workspace_id，默认 pin 或最老 workspace（`kernel_service_impl.py:93-123`）；无 Repository Registry、Placement、Cell、Scheduler Partition。一个 Kernel 进程主要服务一个 repo。
   分类：MISSING。

10. **事件协议不一致、无跨域投影。**
    AgentGraph `workspace_events` 是事务触发器 outbox；SSE 用 `row.id` 当 `seq`，poll 用 `workspace_events.seq`；SSE 重放上限 500，事件身份不同。Kernel `kernel_events` + `event_outbox` 写 pg_notify，但仓库内没有 LISTEN consumer；没有 DLQ/retention/correlation_id/causation_id。AgentGraph 与 Kernel 之间没有事件总线，只有 HTTP 轮询/同步。
    分类：PARTIAL。

### P2 — 质量、可运维性、文档

11. **Kernel 迁移版本表不完整。** `v4_006`、`v4_007`、`v4_008` 实际建表/建索引，但不写 `schema_migrations`；`schema_migrations` 只有 v4_002–v4_005 四行。升级审计不能仅靠版本表。
12. **测试盲区。** code_engine 零测试；没有 AgentGraph↔Kernel 契约测试；Kernel 没有 admin-auth 负向测试、token 撤销测试、多 workspace 隔离测试、取消测试；Docker sandbox 无测试；tree-merge 无端到端测试。
13. **文档漂移。** AgentGraph 文档说 36 张表（实际 48）、88/100 个 MCP 工具（实际 113）、204 项测试（实际收集 544）、35 个组件/colony.tsx（实际 23 个组件、无 colony）；Kernel 文档部分数字与当前收集数不一致。
14. **旧路径/死代码。** `veynod_platform` 不在主链路；`_scheduler_sync_loop` 的 Rust+advisory 分支不可达；`SUSPENDED`、`CONFLICT`、Rust `run_shell` 文档路径未使用。
15. **容量验收口径已经加严，但历史“100 通过”不满足当前 P99。** `full-100` 记录 7,057ms，且旧产物没有 P99 检查；当前 100-runner 实测 8,015.51ms 失败。

---

## 8. 证据分类台账

| 分类 | 代表结论 |
|---|---|
| VERIFIED | Kernel 符号锁、v2 异步接受、work lease fencing、merge CAS/对账、AgentGraph 任务/租约/事件 poll、RLS 测试、Rust 14 测试、2-task smoke |
| IMPLEMENTED_NOT_VERIFIED | Docker sandbox、pyright/ruff 在集成测试中的真实执行、tree-merge 端到端、code_engine、1,000-runner 当前复测 |
| PARTIAL | 任务状态机、事件协议、lease/fencing 覆盖、契约验证、依赖影响、快照不可变性、取消、多仓库 |
| DOCUMENTED_ONLY | 8-vs-16 gate worker A/B、部分 100-runner 历史数字（54.2s/58.0s）、Rust 1M 节点耗时、Platform 当前 smoke |
| MISSING | Kernel 取消、租户隔离、Project/Repository/CodingSession、持久化 Agent Loop、DLQ、correlation IDs、policy/security gate、delivery record |
| UNKNOWN | 生产环境行为、真实多租户竞争、跨网络故障、1,000 级 Soak、AgentGraph 全量集成当前通过状态 |

---

## 9. Phase 0 交付结论与门槛

### 9.1 Phase 0 已完成的部分

- 全量代码/测试/迁移/脚本盘点；
- 运行时拓扑与真实调用链；
- 数据表写入方与状态机清单；
- 并发/租约/门禁/合并审计；
- 安全边界审计；
- 当前容量复测（2-task、100-task、500-task；1,000 未复测）；
- 目标架构映射、风险登记、ADR 索引、路线图。

### 9.2 Phase 0 门槛判定

按计划文档的验收条件：

| 条件 | 判定 |
|---|---|
| 所有核心模块有归属 | 通过（见 MODULE_ASSET_MAP） |
| 核心表有写入权威 | **未通过**：任务状态、lease、inbox 光标等存在多写入方 |
| 核心状态转换有负责人 | **未通过**：AgentGraph/Kernel 均无单一 FSM 命令入口 |
| 所有主链路有调用图 | 通过（见 INTEGRATION_MAP） |
| 已验证能力有测试基线 | 部分通过：任务/租约/门禁/合并有测试；code_engine/跨域契约缺失 |
| 未完成能力明确标记 | 通过（本文件与风险登记） |
| 没有把文档当实现 | 通过（本轮以源码/实测为准） |
| 没有开始大规模破坏性迁移 | 通过（未改源码；仅按用户指令替换 Kernel 运行副本并重启） |

**结论：Phase 0 审计交付完成；架构就绪门槛未通过。** 在 Phase 1 完成“统一身份/契约、单一任务命令入口、取消与事件协议、P0 安全修复”之前，不应进入 Phase 2+ 的大规模实现或宣称生产可用。

### 9.3 明确不宣称

- 不宣称 1,000 Runner 生产 SLA；
- 不宣称 v2 已在生产环境验证（仅本机最新实例验证）；
- 不宣称 Docker sandbox 可用；
- 不宣称 AgentGraph 全量测试通过；
- 不宣称 Agent Runtime/Agent Orchestration 已实现；
- 不宣称多租户/多仓库/Execution Cell 已实现；
- 不宣称本次完成了“整个项目重构”（本轮严格只做 Phase 0）。

---

## 10. 本轮变更清单

| 变更 | 类型 | 说明 |
|---|---|---|
| 新增 `docs/architecture/*.md` | 文档 | Phase 0 交付物 |
| 新增 `docs/adr/ADR_INDEX.md` | 文档 | ADR 索引 |
| 停止旧 :8001 进程 | 运行态 | 用户指令；旧进程为 10:51 启动的旧版本 |
| 旧 Kernel 目录移到 `E:\Codes\backup_veynod_kernel_20261031-2230` | 运行态/备份 | 可回滚；保留历史 `.run` 证据 |
| 从 `E:\Codes\veynod_kernel` 复制到 `E:\Codes\veynod\veynod_kernel` | 运行态 | 源码递归对比 0 差异 |
| 启动最新 Kernel API + Gate Worker | 运行态 | v2 路由与表已生效 |
| 未修改任何原有文档/源码 | — | 遵守“不要改原文档” |

---

## 11. 下一步（等待授权）

严格按计划文档，Phase 0 完成后应停止，等待架构审查和 Phase 1 授权。Phase 1 的最小范围建议见 `REFACTOR_ROADMAP.md`：

1. 统一 ID/任务身份与状态转换契约；
2. 统一事件 envelope（event_id、version、correlation/causation）；
3. 取消/重试/超时语义；
4. Kernel 任务 lease 在 merge 临界区的复核；
5. 去掉合成 gate 证据，读取真实 gate_results；
6. 安全默认值（admin key、sandbox、DAG 鉴权）；
7. code_engine 契约测试与纳入版本控制。

在这些完成前，不启动 Phase 2 的 Agent Runtime 大规模实现，也不做数据库合并或微服务拆分。
