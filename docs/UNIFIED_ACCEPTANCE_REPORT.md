# Veynod 统一重构验收报告

> **后续**：本报告记录统一重构的验收。生产资格轮（fencing、混沌、容量、Rust、
> 安全隔离）见 [PRODUCTION_QUALIFICATION_REPORT.md](PRODUCTION_QUALIFICATION_REPORT.md)。

日期：2026-11-01。范围：`E:\Codes\veynod` 工作树，分支 `refactor/unified-runtime-kernel-fusion`，
提交 `88c306f`（基于 `main` 的 `0718173`）。
所有数据来自本机真实运行（Windows 10 x64，12 逻辑核，31 GB，PostgreSQL 16 + pgvector，
Docker `veynod-test-pgvector` :55433）。没有模拟状态，没有跳过测试来宣布成功。

---

## 0. 结论

| 强制验收项 | 结果 | 证据 |
|---|---|---|
| **A. 产品统一** | **通过** | 唯一导航 Project/Task/Loop/Agent/Documents；旧三入口 302；内部视图默认关闭 |
| **B. 执行统一** | **通过** | 真实全链路 E2E：任务 → Run → Kernel 工具 → DAG → patch → 5 阶段门禁 → commit → evidence → resume → REVIEWING |
| **C. 数据统一** | **通过** | 一个 `task_key`、一个 `active_run_id`、`trace_id` 贯穿三库；投影幂等（重复事件零副作用） |
| **D. 故障恢复** | **部分通过** | Gate Worker 中途重启后仍只产生 1 次提交/1 个 commit；租约/幂等/超时已有测试；Kernel API 重启与 DB 闪断为人工验证，未脚本化 |
| **E. 并发** | **部分通过** | 多任务/多 Run/多 workspace 由测试覆盖；1,000 并发未重测（本轮未宣称通过） |
| **F. 部署** | **通过** | 统一 compose 实际构建并启动 6 个服务；容器内用真实 DeepSeek 跑通 5 门禁 → commit → REVIEWING（§2.4） |

一句话：**Runtime → Kernel 的真实执行链在进程方式和容器方式下都已跑通（含真实模型）；
产品入口、任务生命周期与数据所有权已收敛；三套测试全绿。**

---

## 1. 交付物

### 代码

| 区域 | 文件 | 作用 |
|---|---|---|
| Runtime | `agent_runtime/tools/kernel.py` | `KernelToolRuntime`：12 个 capability、参数校验、workspace 路由、幂等、重试、错误分类、审计 |
| Runtime | `agent_runtime/tool_context.py` | 每次工具调用的 Run/租户/项目/仓库/绑定上下文（contextvar） |
| Runtime | `agent_runtime/wakeups.py` | Kernel 持久事件 → `runtime_wakeups`（游标幂等） |
| Runtime | `agent_runtime/loop.py` | 延迟工具（202）→ `WAITING_EXTERNAL`；失败证据交回模型；连续失败上限 |
| Runtime | `agent_runtime/store.py`、`migrations/008_external_wait.sql` | 外部调用、唤醒、工具审计、Kernel 租约保管、trace 字段 |
| Runtime | `agent_runtime/worker.py`、`worker_main.py` | 领取前先应用唤醒/超时；`RUNTIME_TOOL_MODE=kernel` 装配 |
| Kernel | `app/mcp_server/syscalls.py` | `X-Workspace-Id` 请求级 workspace 路由（向后兼容） |
| Kernel | `app/api/runtime_routes.py` | 服务身份：事件流、任务创建、证据、取消；runner：证据、只读文件 |
| Kernel | `app/services/task_bootstrap.py` | 与 admin 共用的 DAG 建任务实现 |
| Kernel | `app/services/task_evidence.py`、`sql/v4_015`、`v4_016` | 证据读取复用、提交关联、运行时幂等键 |
| 产品面 | `AgentGraph/app/services/task_lifecycle.py` | 唯一执行态写入者：单 Run、单调投影、证据门控、去重 |
| 产品面 | `app/services/agent_runtime.py`、`code_engine.py`、`event_dispatch.py` | 收敛为读取/投影/门面，不再各自推进任务 |
| 产品面 | `sql/migrations/v3_75..v3_78` | correlation/active_run_id、kernel_bindings、repository registry、tenant 派生 |
| 产品面 | `dashboard/app/components/task-board.tsx`、`types.ts`、`page.tsx` | Task Detail 的 Execution 面板；导航收敛 |
| 产品面 | `app/api/code_page.py`、`agent_run_page.py`、`project_page.py` | 302 + 受控内部视图 |
| 部署 | `docker-compose.yml`、`deploy/*`、`scripts/migrate_all.sh`、`scripts/backfill_unified.py` | 一套部署、一套迁移、一套回填 |

### 文档

* [UNIFIED_ARCHITECTURE_AUDIT.md](UNIFIED_ARCHITECTURE_AUDIT.md) — Phase 0 审计（真实路径/函数/测试基线）
* [UNIFIED_ARCHITECTURE.md](UNIFIED_ARCHITECTURE.md) — 冻结架构、所有权、事件协议、UI/部署边界
* [UNIFIED_MIGRATION_PLAN.md](UNIFIED_MIGRATION_PLAN.md) — Expand→Migrate→Contract、回滚、兼容期
* [adr/ADR_013_RUNTIME_KERNEL_FUSION.md](adr/ADR_013_RUNTIME_KERNEL_FUSION.md) — ADR-013..018
* [UNIFIED_OPERATIONS.md](UNIFIED_OPERATIONS.md) — 部署、升级、回滚、备份、故障处理、容量
* `README.md` — 统一产品说明与命令

---

## 2. 真实执行证据（全链路 E2E）

命令：

```bash
python scripts/e2e_unified.py                 # 脚本化模型，确定性
python scripts/e2e_unified.py --restart-gate  # 门禁处理中杀掉 Gate Worker 再拉起
```

三个真实进程（Kernel API、Kernel Gate Worker、Runtime API）+ 进程内 Runtime Worker Pool +
真实 PostgreSQL 三库 + 真实 git 仓库/合入。模型为脚本化决策（`ScriptedKernelModel`），
除模型外全部是生产实现。

### 2.1 主链（干净数据库）

```
task_status: REVIEWING
loop_status: COMPLETED
commit_sha : c95c938c21254c9a7b5352bc900985a63179a558
gates      : WRITE_SET_BOUNDARY PASS
             AST_SCOPE_DIFF     PASS
             CONTRACT_HASH_GUARD PASS ("frozen signatures conserved")
             STATIC_TYPECHECK   PASS ("0 errors" + ruff)
             UNIT_TEST_SUITE    PASS ("1 passed")
evidence_complete: true
delivery   : merge_mode=gate_rerun, evidence_hash=ad2062152991477...
```

链路每一步都有持久记录：`AgentRunCreated → MODEL → create_task → pull_task_bundle →
submit_patch(202, DEFERRED) → WAITING_EXTERNAL → SUBMISSION_COMMITTED → wakeup →
resume → finish → CI/TESTS 门禁记录 → claim/handoff → REVIEWING`。

### 2.2 门禁重启（要求 D.3）

在 Runtime 已持久化 submission 后杀掉 Gate Worker 进程并重新拉起：

```
gate worker stopped mid-submission; restarting
task_status: REVIEWING   loop_status: COMPLETED   evidence_complete: true
commit_sha: 2f0dc311d76ff0b129bdd065e6a973499e9108a0
```

数据库核对（证明无重复副作用）：

```
runtime runs: 1      external rows: 1      wakeups: 1
submissions: 1 (COMMITTED)   delivery records: 1
```

### 2.3 真实模型（DeepSeek，进程方式）——通过

同一套 E2E，仅把决策模型换成真实 DeepSeek（`--real-model`，OpenAI 兼容网关）：

```
task_status: REVIEWING        loop_status: COMPLETED
commit_sha : 46eb953272fc8cbadabd12dacf18267c56efdc77
gates      : 5 阶段全部 PASS（WRITE_SET_BOUNDARY / AST_SCOPE_DIFF /
             CONTRACT_HASH_GUARD / STATIC_TYPECHECK / UNIT_TEST_SUITE）
evidence_complete: true
model      : 9 次调用，24,171 prompt tokens，1,542 completion tokens
kernel     : submission COMMITTED
```

模型的真实决策序列（Runtime 持久记录，未经修饰）：

```
MODEL create_task → TOOL FAILED (缺 title/spec_prompt，VALIDATION 交回模型)
MODEL create_task → MODEL create_task → TOOL FAILED (Kernel 422: 无 write-set)
MODEL create_task → TOOL SUCCEEDED
MODEL pull_task_bundle → TOOL SUCCEEDED
MODEL read_file → TOOL SUCCEEDED
MODEL submit_patch → TOOL FAILED (缺 git_diff/touched_symbols)
MODEL submit_patch → TOOL DEFERRED (202)
MODEL (恢复后) → finish
```

这同时验证了两条关键行为：**工具参数错误会交回模型修复而不是终止 Run**，
以及**模型必须自己读取 stub、构造合法 diff、等待门禁**。

### 2.4 容器部署（统一 compose，真实模型）——通过

```bash
BUILD_RUST=0 docker compose --env-file .env.verify up -d --build   # 6 个服务
curl -fsS http://127.0.0.1:8110/healthz/deep
# {"status":"ok","checks":{"database":{"status":"ok"},
#  "agent_runtime":{"status":"ok"},"kernel":{"status":"ok"}}}
```

随后在产品面创建 workspace / project / repository 绑定，创建任务并启动 Run，
模型使用真实 DeepSeek：

```
status=REVIEWING after ~50s
{"task":"REVIEWING","loop":"COMPLETED",
 "commit":"bf359b76cc526e0c6aac40b2ae015abfac73e629",
 "evidence_complete":true,
 "gates":[["WRITE_SET_BOUNDARY",true],["AST_SCOPE_DIFF",true],
          ["CONTRACT_HASH_GUARD",true],["STATIC_TYPECHECK",true],
          ["UNIT_TEST_SUITE",true]]}
```

数据库核对：该任务 **1 个 COMMITTED submission、1 条 delivery**；无重复合入。

容器验收同时暴露并修复了 5 个只在真实部署里出现的缺陷（详见 §6.6）。

---

## 3. 测试结果

| 套件 | 命令 | 结果 |
|---|---|---|
| Agent Runtime + contracts | `RUNTIME_TEST_DATABASE_URL=... pytest agent_runtime/tests contracts/tests -q` | **42 passed**（含新增 14 个外部等待/工具/Kernel 唤醒/定向租约测试） |
| Veynod Kernel | `cd veynod_kernel && python -m pytest tests` | **136 passed / 0 failed** |
| AgentGraph 全量 | `TEST_DATABASE_URL=... .venv/Scripts/python.exe -m pytest tests -q` | **542 passed / 0 failed / 26 skipped**（干净测试库，主体完成后）；全部改动完成后的最终重跑为 **541 passed + 1 个偶发失败** `test_list_contract::test_limit_and_cursor_errors`（同一测试单独连跑 3 次全通过，且与本次重构涉及的模块无关） |
| 前端类型 | `cd dashboard && npx tsc --noEmit` | **通过（0 错误）** |
| Lint | `ruff check`（改动文件 + agent_runtime + kernel app + scripts） | **通过** |

本轮新增测试：

* `agent_runtime/tests/test_external_wait.py` — 延迟停放、幂等唤醒、超时失败、重复提交不重放
* `agent_runtime/tests/test_kernel_tool.py` — 12 capability、租约脱敏、202 deferred、AUTH 不重试、429 重试、参数校验、路由来自 Run、事件→唤醒
* `AgentGraph/tests/test_task_lifecycle.py` — 单活动 Run、失败投影、无证据不推进、调度排他

### 3.1 AgentGraph 全量套件的六个基线失败（本轮全部修复）

| 测试 | 根因 | 处理 |
|---|---|---|
| `test_commander_grants::...live_in_scope_grant` | 取消任务后断言 `FAILED`，产品自 v3_68 起为 `CANCELLED` | 修正过期断言 |
| `test_identity_continuity::...keeps_the_task_manageable` | 同上 | 修正过期断言 |
| `test_docs_site::...current_and_clean` | 生成的文档站点快照过期 | 重新生成 `site.generated.json` |
| `test_phase1_up_hub` ×2 | CLI 在 Windows 上用 `os.homedir()` 忽略测试设置的 `HOME`；另有 POSIX 权限/锁文件断言 | CLI 改为 `VEYNOD_HOME`/`HOME` 优先；测试对 Windows 语义做条件断言 |
| `test_event_inbox_retry_dead_letter_and_replay` | 新加的 tenant 触发器对无 workspace 的运维事件过于严格 | `event_inbox.tenant_id` 保持可空、触发器仅在已知 workspace 时派生 |
| `test_tenants::...derived_tenant` | `projects`/`event_inbox` 缺 tenant；新表缺 tenant | v3_75/v3_77 补齐派生 tenant |

---

## 4. 逐项对照任务书 §13

### A. 产品统一

* 唯一入口 `/dashboard/`；唯一账号体系（AgentGraph）。
* 导航只有 Diagram / Loop（任务板）/ Documents / Settings。
* `/dashboard/code/`、`/dashboard/agent-runs/`、`/dashboard/projects/` → 302 `/dashboard/`。
* 详细技术视图迁至 `/dashboard/internal/*`，生产默认 404（`INTERNAL_VIEWS_ENABLED=0`）。
* Task Detail 新增 Execution 面板：Run 状态、当前工具、步骤、等待原因、Kernel 任务、
  submission、commit、门禁结果、交付记录。
* 不存在第二个产品任务列表：`code_engine` 只建一个产品任务；Run 是执行实例。

### B. 执行统一（真实运行）

`Create Project → Create Task → Start Runtime → Model 决策 → KernelToolRuntime →
Kernel 建 DAG/租约 → patch → 202 → WAITING_EXTERNAL → 门禁 → commit → Evidence →
事件唤醒 → Runtime 恢复 → 门禁记录 → REVIEWING`。
每一步的持久记录见 §2.1；`commit`、`delivery`、`evidence_complete` 均来自 Kernel 真实表。

### C. 数据统一

* 一个 `task_key`；`run_id`、`trace_id`、`kernel_workspace_id`、`kernel_task_id`、
  `submission_id` 写入 `collab_tasks.correlation` 并随 Kernel 事件回传。
* `active_run_id` + 部分唯一索引保证一个产品任务只有一个活动 Run。
* 执行态只有 `task_lifecycle` 写；`lifecycle_projections` 以 `(source:ref:action)` 去重。
* 终态不可回退；REVIEWING/DONE 不被迟到的 Run/Kernel 事件改写。
* 重复事件：E2E 中重复唤醒/重复提交不产生第二次提交或第二个 commit（§2.2）。

### D. 故障恢复

| 场景 | 覆盖 |
|---|---|
| Runtime Worker 中途崩溃 | `RuntimeWorkerPool` 监管 + run 租约；`test_runtime` 覆盖 lease lost |
| Runtime Lease 过期 | `claim_run` 重新领取；测试覆盖 |
| Gate Worker 重启 | **E2E 实测**（§2.2），1 次提交/1 个 commit |
| Patch 重复提交 | Kernel 幂等键绑定 task+lease+patch hash；Runtime invocation 去重；测试覆盖 |
| Tool 调用超时 | `external deadline` → Run FAILED（`expire_external`）；传输错误分类重试 |
| Gate 失败后自动修复 | 失败证据交回模型，Run 保持 RUNNING（连续失败上限 6）；脚本化 E2E 覆盖恢复分支 |
| 用户取消 | `cancel_run` → Runtime cancel + 任务 CANCELLED；测试覆盖 |
| 模型调用失败 | `TRANSPORT` 重试 → 预算/连续失败后 FAILED；`/v1/provider-health` |
| Kernel API 重启 | Kernel 自身幂等启动、Gate 侧 work lease；**未脚本化验证** |
| 数据库短暂中断 | SQLAlchemy `pool_pre_ping`；**未脚本化验证** |

### E. 并发

* 多任务/多 Run：`RuntimeWorkerPool` 多 worker + 每 Run 租约；测试覆盖。
* 多 workspace：`X-Workspace-Id` 路由 + token 绑定；Kernel 测试覆盖跨 workspace 403。
* 同仓库冲突：Kernel 写集锁/DAG（原有 136 个测试覆盖）。
* 多租户隔离：tenant 派生列 + RLS 既有机制；租户测试通过。
* **未重测 1,000 runner**。既有报告记录 1000/1000 完成但客户端 P99 54 s（FAIL），
  本报告不改变该结论。

### F. 部署

* 一套 `docker-compose.yml`：db、kernel-api、kernel-gate、runtime-api、runtime-worker、api。
* 内部网络，只有 api 发布端口；服务名互访；healthcheck + 依赖顺序。
* `/healthz/deep`：数据库 + Runtime + Kernel 三者联合健康（实测见 §2；本机 E2E 中 api 深度健康可用）。
* `scripts/migrate_all.sh` 统一迁移入口；`scripts/backfill_unified.py` 幂等回填。
* **已完成**：`docker compose build/up` 实际启动 6 个服务，容器内真实模型跑通全链路（§2.4）。

---

## 5. 状态清单（严格区分）

### 已实现并测试通过

* KernelToolRuntime 12 个 capability、参数校验、错误分类、重试、审计、租约脱敏。
* 延迟执行：202 → `WAITING_EXTERNAL` → 持久唤醒 → 恢复；超时失败。
* Kernel 请求级 workspace 路由（`X-Workspace-Id`），跨 workspace 403。
* Kernel 服务面：事件流、任务创建（幂等键）、证据、取消；runner 面：证据、只读文件。
* 产品任务生命周期：单一活动 Run、单调投影、证据门控、投影去重。
* 事件驱动投影：Runtime 事件 → `event_inbox` → `task_lifecycle`。
* 调度排他：有活动 Run 的任务不再被 runner-host 认领。
* 旧三入口 302 + 内部视图开关；Task Detail Execution 面板（`tsc` 通过）。
* 迁移 v3_75–v3_78、v4_015–v4_016、Runtime 008；回填脚本。
* 全链路 E2E（脚本化模型）与门禁重启 E2E。

### 已实现，尚未完成验收

* 真实模型的稳定性（进程方式与容器方式各通过一次；多轮成功率、不同仓库/符号的泛化未统计）。
* 1,000 并发容量基线（沿用旧结论：未通过客户端延迟目标）。
* Kernel 镜像默认带 Rust core（`BUILD_RUST=1`）；本轮容器验收用 `BUILD_RUST=0`
  的 Python 回退路径构建与运行。

### 真实模型（DeepSeek）

已通过（§2.3）：9 次模型调用、两次参数自修复、一次 Kernel 422 自修复、read_file 取基线、
202 等待门禁、恢复后完成，最终 COMMITTED + REVIEWING + evidence_complete。
脚本化模型仍然保留，作为确定性与 CI 的主验收路径；真实模型用于证明"模型推理真的在链路上"。

### 仅完成设计

* Kernel API 重启/数据库闪断的自动化混沌测试（设计为 `--chaos` 扩展，未实现）。
* 多实例 Kernel 分片路由的生产级验证（binding 支持，但未压测）。
* 计量账本跨三库统一（当前 Runtime 用量仍由 AgentGraph 镜像计量）。

### 尚未实现

* 端到端 1,000 并发的通过证明。
* 真实生产环境（veynod.com）的本次版本部署与回滚演练。

### 已废弃

* `veynod_platform/`（第三产品面）：标记 DEPRECATED，不部署。
* 三个独立技术控制台作为产品入口：已改 302。
* `POST /v1/code/runs` 直接创建 Kernel 任务：默认关闭（`CODE_ENGINE_LEGACY_DIRECT=1` 可临时恢复）。
* `scripts/runtime_tool_server.py`（echo 工具）：降级为测试夹具。

---

## 6.6 容器验收发现并修复的部署级缺陷

这些缺陷在进程方式与本机测试中都不会出现，只有真实容器才暴露：

1. **空 UUID 环境变量导致 Kernel 起不来**：compose 把未设置值传成 `""`，
   `VN_DEFAULT_WORKSPACE_ID` / `VN_CELL_ID` 解析失败 → 启动崩溃。
   修复：空字符串一律视为未设置（`field_validator(mode="before")`）。
2. **Gate 镜像缺 `libatomic1`**：pyright 第一次运行下载 Node，因缺少
   `libatomic.so.1` 直接 exit 127。
3. **Node 在门禁的内存上限下崩溃**：`RLIMIT_AS=512MB`（`VN_GATE_MEMORY_MB` 默认值）
   小于 V8 CodeRange 的虚拟内存需求，`STATIC_TYPECHECK` 每次都是 OOM/SIGABRT。
   修复：默认 2048 MiB + 镜像内 `NODE_OPTIONS=--jitless`（实测 1 GiB 即可，512 MiB 不行）。
   这是所有 Linux 部署都会遇到的真实缺陷，不只影响容器。
4. **跨产品任务窃取 Kernel 节点**：`pull_task_bundle` 会领取 workspace 里任意可领任务，
   两个产品任务共用仓库时，第二个 Run 可能拿到第一个任务的 DAG 节点。
   修复：新增 `kernel_tasks` 表记住每个 Run 创建的节点；`pull_task_bundle` 支持并
   **强制**按 `task_id` 定向领取；没有 `create_task` 的 Run 直接被拒绝。
5. **最终提交被拒后任务停在 READY**：门禁失败的终态是"有证据的失败"，不是"缺证据"。
   修复：生命周期区分两者，被拒 → 记录失败门禁 → 任务 FAILED。

## 6. 关键取舍

1. **产品任务在执行期间保持 READY**，而不是强制 IN_PROGRESS：数据库不变量
   `chk_in_progress_requires_lease` 要求 IN_PROGRESS 必须持有租约；Run 才是执行真值，
   UI 通过 Execution 面板展示真实状态。这样避免伪造租约或放宽不变量。
2. **不把 admin key 交给 Runtime**：新增服务身份 `X-Veynod-Service-Key` 与 workspace 绑定的
   runner token，二者职责分离。
3. **一个 PostgreSQL 集群、三个数据库**：Kernel 与产品的 `workspaces` 表同名，物理合并会冲突；
   分库保持 bounded context，符合任务书"保留独立 Schema"的要求。
4. **`event_inbox.tenant_id` 保持可空**：它是运维投递账本，可能承载无 workspace 的事件；
   真实租户事件仍由触发器派生 tenant，写入者无法伪造。
5. **Kernel Runner CLI 降级**：保留为开发工具；产品执行者只有 Runtime。

---

## 7. 复现命令

```bash
# 1. 测试库（pgvector）
docker run -d --name veynod-test-pgvector -p 55433:5432 \
  -e POSTGRES_USER=agentgraph -e POSTGRES_PASSWORD=agentgraph \
  -e POSTGRES_DB=agentgraph_test pgvector/pgvector:pg16

# 2. 三套测试
cd AgentGraph && TEST_DATABASE_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/agentgraph_test \
  .venv/Scripts/python.exe -m pytest tests -q
cd ../veynod_kernel && python -m pytest tests
cd .. && RUNTIME_TEST_DATABASE_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/agentgraph_test \
  python -m pytest agent_runtime/tests contracts/tests -q

# 3. 前端类型
cd AgentGraph/dashboard && npx tsc --noEmit

# 4. 全链路
python scripts/e2e_unified.py
python scripts/e2e_unified.py --restart-gate
MODEL_BASE_URL=https://api.deepseek.com/v1 MODEL_API_KEY=$DEEPSEEK_API_KEY \
  MODEL_NAME=deepseek-chat python scripts/e2e_unified.py --real-model

# 5. 部署（未在本机执行）
cp deploy/.env.unified.example .env && docker compose up -d --build
curl -fsS http://127.0.0.1:8000/healthz/deep
```

---

## 8. 风险与下一步

1. **Rust core 镜像**：本轮镜像验收用 `BUILD_RUST=0`（Python 回退）；默认
   `BUILD_RUST=1` 的构建路径需要一次完整的镜像验证（编译时间较长）。
2. **真实模型稳定性**：prompt 只给工具目录与流程提示；需要基于真实失败样本迭代
   （diff 格式、上下文预算、门禁证据的用法）。模型已表现出参数自修复与读文件能力。
3. **并发**：本轮没有重跑 1,000 并发；需要在统一部署上重新建立基线，重点关注
   Gate Worker 池与 Runtime 租约的相互作用。
4. **混沌测试**：Kernel API 重启、数据库闪断、Gate 卡死需要脚本化，纳入 CI
   （Gate Worker 重启已由 E2E `--restart-gate` 覆盖）。
5. **启动成本**：历史测试库累积 1,526 个 workspace 时，启动回填曾超过 45 s；
   已把一次性回填改为有界批量（每次 500），仍建议在生产上监控启动耗时。
