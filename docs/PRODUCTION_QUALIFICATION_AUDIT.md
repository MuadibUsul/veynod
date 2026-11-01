# Veynod 生产资格审计（Phase 0）

日期：2026-11-01。分支 `production/qualification`，基于 `main@fb398ba`（PR #86 已合并统一重构）。
方法：读实际代码 + 跑现有测试，不以旧报告为准。所有结论带文件与行号。

---

## 1. 当前真实状态

### 1.1 代码与运行

| 项 | 事实 |
|---|---|
| main HEAD | `fb398ba Refactor/unified runtime kernel fusion (#86)` |
| 统一重构 | 已合入 main；本轮基线包含 `KernelToolRuntime`、`WAITING_EXTERNAL`、`task_lifecycle`、定向租约、UI 收敛 |
| 合并差异 | main 缺失 `9b4f6dd` 的两个前端修复（status-pill tone、execution 样式），已在本分支恢复 |
| 生产部署 | 未授权本轮部署；VPS 编排稿在 `deploy/vps/docker-compose.unified.yml`（已 `compose config` 校验，未上线） |
| 本地镜像 | `veynod-api:9b4f6dd` 332MB / `veynod-kernel:9b4f6dd` 602MB（BUILD_RUST=0）/ `veynod-runtime:9b4f6dd` 207MB |

### 1.2 核心调用链（按代码）

```
AgentGraph  POST /v1/tasks/{key}/run            app/api/agent_run_routes.py
  -> task_lifecycle.start_run                   app/services/task_lifecycle.py:233
       -> Runtime POST /v1/runs                 agent_runtime/api.py
       -> Runtime POST /v1/runs/{id}/start
       -> collab_tasks.correlation / active_run_id
Runtime Worker                                 agent_runtime/worker.py:46
  -> RunStore.claim_run                         store.py:1046
  -> LoopController.step                        loop.py:108
       -> ModelGateway.decide                   gateway.py
       -> KernelToolRuntime.execute             tools/kernel.py
            -> Kernel /v1/syscalls/*            mcp_server/syscalls.py
            -> Kernel /v1/runtime/tasks         api/runtime_routes.py
       -> RunStore.record_step / transition / checkpoint / defer_external
Kernel GateWorker                              services/gate_worker.py
  -> DeterministicVerificationGate              5 阶段
  -> MergeCoordinator -> git commit -> Evidence
  -> kernel_events + event_outbox
Runtime KernelEventWatcher                     wakeups.py:66
  -> runtime_wakeups -> RunStore.consume_wakeups store.py:1346
AgentGraph event_dispatch                      app/services/event_dispatch.py
  -> task_lifecycle.project_run_event          :981
       -> collect_evidence -> quality.record_gate_result -> REVIEWING
```

### 1.3 数据所有权（代码强制）

| 对象 | 唯一写入者 | 证据 |
|---|---|---|
| Product Task | `task_lifecycle` | 其余模块只读；`scheduler` 跳过 `active_run_id` 非空任务（`services/scheduler.py:413`） |
| Agent Run | `RunStore` | `agent_runtime/store.py` |
| Kernel DAG/Task | Kernel scheduler/gate | 独立 schema |
| Gate evidence | Kernel | `/v1/runtime/tasks/{id}/evidence`、`/v1/admin/tasks/{id}/evidence` |
| Approval | AgentGraph | `tasks.handoff_task` |

---

## 2. 已验证能力（有测试或 E2E 证据）

| 能力 | 等级 | 证据 |
|---|---|---|
| Runtime→Kernel 全链路（脚本模型） | E2E VERIFIED | `scripts/e2e_unified.py`：5 门禁 PASS、commit、REVIEWING |
| 真实 DeepSeek 决策链 | E2E VERIFIED（单次） | 9 次调用、两次参数自修复、`get_task_evidence`、202 恢复 |
| 容器化统一部署 + 真实模型 | PRODUCTION-LIKE VERIFIED（本机 compose） | `/healthz/deep` 200；50s 到 REVIEWING |
| Gate Worker 重启不重复提交 | CHAOS VERIFIED（单场景） | `--restart-gate`：1 submission / 1 commit / 1 delivery |
| 202 延迟执行 + 事件唤醒 | INTEGRATION VERIFIED | `agent_runtime/tests/test_external_wait.py`（4 例） |
| 定向 Kernel 节点租约 | INTEGRATION VERIFIED | `test_kernel_tool.py::test_pull_without_create_is_refused` 等 |
| 产品任务单调/幂等投影 | INTEGRATION VERIFIED | `test_task_lifecycle.py` |
| Kernel 异步提交/门禁/合并 | UNIT+INTEGRATION VERIFIED | Kernel 136 tests |
| 单实例 workspace 路由（`X-Workspace-Id`） | INTEGRATION VERIFIED | `test_integration.py` 跨 workspace 403 |
| UI 收敛 | UNIT VERIFIED（tsc + 302 测试） | `test_unified_ui.py` |

---

## 3. 缺陷（按优先级，含证据）

### P0-1 Run 写入没有 fencing：旧 Worker 可以覆盖新 Worker（**真实正确性缺陷**）

事实：
- `RunStore.claim_run`（`agent_runtime/store.py:1046-1097`）只覆盖 `lease_token_hash/lease_expires_at`，
  **没有递增任何 epoch**。
- `transition`（`:553`）、`checkpoint`（`:658`）、`record_step`（`:760`）、`defer_external`（`:1172`）、
  `add_usage`（`:928`）都不校验当前租约，只有 loop 在写之前调用一次
  `assert_lease`（`loop.py:103`）。
- 因此存在窗口：Worker A 的模型调用耗时 30s，租约过期；Worker B 领取并开始执行；A 返回后
  在一次 guard 与一次写之间租约失效，A 的 `record_step` / `checkpoint` /
  `transition(COMPLETED|FAILED)` 仍会落库。

影响：
- A 可以把 B 正在执行的 Run 直接置为 `COMPLETED/FAILED`（`transition` 只校验状态机与 version，
  不校验租约），B 的后续写入得到 version 冲突或写入 A 的 checkpoint 历史。
- Checkpoint 可能被旧 Worker 注入，恢复时从错误状态继续。
- 与任务书 §4.1 的竞态完全一致；当前只靠 guard 的“检查后使用”无法消除。

结论：**必须加 epoch fencing**（Phase 1 修复）。

### P0-2 恢复语义依赖“租约过期”，但没有旧 Worker 自终止保证

`worker._heartbeat`（`worker.py:84-101`）在 `renew_lease` 失败时只是 `return`；
`LoopController.step` 只有在下一个 guard 点才察觉。配合 P0-1，任何不经过 guard 的写都能落库。
修复需与 P0-1 同一机制解决（写时校验 epoch）。

### P1-3 `start_run` 在跨服务调用中途失败会留下孤儿 Run

`task_lifecycle.start_run`（`:233-320`）先 `Runtime POST /v1/runs`，再 `start`，最后写本地库。
若第二步或本地 commit 失败：Runtime 侧存在一个已启动、没有产品任务指向的 Run，
会被 Worker 领取执行（`product_task_id` 只是字符串，没有归属校验）。
同时 `SELECT ... FOR UPDATE`（`:249`）跨越两次 HTTP 调用，任务行被锁住的时长取决于 Runtime 延迟。

影响：孤儿执行、资源浪费；高并发下任务行锁竞争。

### P1-4 Kernel 事件 watcher 只认全局 `KERNEL_URL`（多实例路由缺失）

`worker_main.py` 用 `KERNEL_URL` 构造唯一 `KernelEventSource`；
`KernelEventWatcher.poll_once`（`wakeups.py:79-91`）从 `RunStore.list_kernel_workspaces()`
拿到的是 workspace id，但请求始终发往同一个 base URL，并只用全局 `KERNEL_SERVICE_KEY`。
而 Run 的 `kernel` 绑定（`agent_runs.kernel`）已经携带 `base_url` 与 `credential_ref`
（`KernelToolRuntime._binding_from_run`，`tools/kernel.py`）。

影响：一个项目绑定到第二个 Kernel 实例时，它的事件永远不会被消费 → Run 永久
`WAITING_EXTERNAL`；同时 `collect_evidence` / `execution_view` 也用全局
`settings.kernel_url`（`code_engine._engine()`），跨实例取不到证据。
**不能一边声明支持多 Kernel，一边只实现单实例消费。**

### P1-5 `read_file` 的 `rev` 未校验：git 参数注入

`/v1/runner/workspaces/{ws}/file`（`api/runtime_routes.py:311-341`）接受任意 `rev`，
交给 `GitRepository.read_file`（`services/git_ops.py:304-322`）拼成
`git show {rev}:{path}`（`:317`）。
`rev` 以 `-` 开头时会被 git 当作选项（例如 `--output=<file>` 可写文件），
runner token 的持有者（包括模型通过 `KernelToolRuntime.read_file` 传参）可以越界写/读。
`path` 有 `..` 校验，`rev` 没有。

### P1-6 Gate `local` 沙箱与 Kernel 共享容器文件系统与网络

`gate_sandbox.sanitized_environment`（`services/gate_sandbox.py:59-76`）只清洗环境变量；
`_run_local`（`:115`）在同一容器的临时目录里执行仓库自带的测试命令。
恶意仓库的 `pytest`/`conftest.py` 可以读 `/repos` 下**其他 workspace 的 bare repo**、
访问内网服务（包括 `kernel-api`、`runtime-api`、db）。
`VN_ALLOW_LOCAL_GATE_IN_PROD=1` 使这条路径在生产可用。

影响：多租户下代码与凭据暴露。修复优先级取决于是否多租户生产；若单租户可接受，
必须在文档与验收中明确标记为已知限制，并给出 docker sandbox 方案。

### P1-7 任务证据读取与执行视图忽略绑定

`task_lifecycle.collect_evidence` 与 `execution_view` 调用
`code_engine._engine()`（`AgentGraph/app/services/code_engine.py:165`），
固定使用 `settings.kernel_url` + `settings.kernel_admin_key`。
绑定到其他 Kernel 实例的任务会读错实例（404 或错误证据）。

### P2-8 `create_task` 幂等键只来自模型提供的 tool key

`KernelToolRuntime._do_create_task` 用 `f"{product_task_id}:{idempotency_key}"` 作为
`runtime_request_key`。模型若换一个 key 再调一次，会创建第二个 DAG 节点；
旧节点留在 READY，可能被兼容的 Kernel Runner CLI 领取。
建议增加 `(run_id, spec 摘要)` 去重，或明确“一个 Run 的默认节点”语义。

### P2-9 单实例约束缺少显式拒绝

Kernel 在 `VN_DEFAULT_WORKSPACE_ID` 为空时回落到“最早 workspace”
（`kernel_service_impl._default_workspace_id`），多 workspace 部署下这会让
未带 `X-Workspace-Id` 的旧调用静默落到错误仓库。建议生产启动时要求显式配置，
或至少打 warn + health 字段。

### P2-10 容量、Rust、混沌、安全隔离、恢复演练均未验证

- Rust 正式构建（BUILD_RUST=1）未跑；容器验收用 Python 回退。
- 1,000 并发未重测；既有记录为 FAIL（客户端 P99 54s）。
- 混沌矩阵只有 Gate Worker 重启一项。
- 多租户隔离没有专门的自动化用例（仅有 workspace header 403 单测）。
- 备份恢复未演练。

### P3-11 文档口径不一致

`KernelToolRuntime` 能力数量在 README/架构/验收报告中出现过 9/11/12 三种说法；
实际注册表（`agent_runtime/tools/kernel.py::CAPABILITIES`）为 **12**
（create_task、pull_task_bundle、query_symbol_signature、read_file、submit_patch、
get_submission、get_task_evidence、get_task_status、cancel_task、request_task_split、
escalate_contract_defect、inject_dependency_task）。

---

## 4. 现有测试基线（本机，PostgreSQL 16 + pgvector :55433）

```bash
cd AgentGraph && TEST_DATABASE_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/agentgraph_test \
  .venv/Scripts/python.exe -m pytest tests -q          # 542 passed / 0 failed / 26 skipped（干净库）
cd veynod_kernel && python -m pytest tests -q          # 136 passed
RUNTIME_TEST_DATABASE_URL=... python -m pytest agent_runtime/tests contracts/tests -q   # 42 passed
cd AgentGraph/dashboard && npx tsc --noEmit            # 0 errors
python scripts/e2e_unified.py                          # REVIEWING + 5 gates + commit
```

已知偶发：`tests/test_list_contract.py::test_limit_and_cursor_errors` 在整库高负载下
曾失败一次，单独连跑 3 次通过；与本轮改动无关，但需在容量阶段留意。

---

## 5. 本轮执行计划（对应任务书 Phase）

| Phase | 工作 | 退出条件 |
|---|---|---|
| 1 | epoch fencing（P0-1/P0-2）、start_run 补偿（P1-3）、rev 校验（P1-5） | 新增 fencing 测试；旧 Worker 写全部 `LeaseLost` |
| 2 | 多 Kernel 事件源路由 + 每源凭据（P1-4）；事件证据按绑定读取（P1-7）；混沌套件 | 两实例事件各自消费；12 项混沌矩阵有真实结果 |
| 3 | BUILD_RUST=1 镜像 + 运行路径证明 + Python/Rust 一致性 | 容器 `/healthz` core.available=true 且真实执行走 Rust |
| 4 | 分层容量（Runtime / Kernel / 统一）10→1000 | `CAPACITY_BASELINE.md`，未达标记 FAIL |
| 5 | 多租户隔离与攻击面测试；gate 隔离结论 | `SECURITY_QUALIFICATION_REPORT.md`，无未处置 High |
| 6 | `task_key`→全链路 trace；指标；恢复演练 | 一条命令还原一个任务的全部事实 |
| 7 | DeepSeek 多场景矩阵（预算上限） | 成功率/成本/修复次数统计 |
| 8 | 文档校准、能力数量统一、最终报告 | 验收矩阵逐项 PASS/FAIL |

---

## 6. 不在本轮范围

- 重新设计统一架构（任务书 §2 明确禁止）。
- 把 Kernel 的逐任务测试搬进模型循环之外（Gate 语义不变）。
- 未授权的生产部署。
