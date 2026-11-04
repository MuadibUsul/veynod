# PHASE1H_REPORT — 后台投影与 AgentGraph 镜像重建

> 日期：2026-10-31
> 范围：Phase 1H：AgentGraph 后台 code-run projection + 重建/重启运行镜像
> 状态：源码 + 单测通过；镜像已重建并启动；未提交 git

---

## 1. 本次完成

### 1.1 后台投影循环

`AgentGraph/app/services/code_engine.py` 新增：

- `project_code_runs_once(limit)`：
  - 只扫描 `input_context.code_engine=veynod-kernel` 且状态为 `TODO/READY/IN_PROGRESS` 的产品任务；
  - 按 workspace 批量读取 Kernel task，避免每任务一次 list；
  - 复用 `lifecycle_action` / `_sync_lifecycle`：
    - Kernel CANCELLED → 产品 CANCELLED；
    - Kernel FAILED → 产品 FAILED；
    - Kernel COMMITTED → 真实 gate evidence + REVIEWING；
  - 单任务失败不影响整批；返回扫描/同步/跳过/错误计数。
- `code_projection_loop()`：
  - 默认启用（`CODE_PROJECTION_ENABLED=true`）；
  - 间隔 `CODE_PROJECTION_INTERVAL_SECONDS=30`；
  - 批量 `CODE_PROJECTION_BATCH_SIZE=100`；
  - 异常只记录日志，不退出循环。
- `AgentGraph/app/main.py` lifespan 在后台任务列表中加入 `agentgraph-code-projection`，关闭时统一 cancel。

这样不再需要人手动刷新 Code 页面，取消/失败/完成会在后台同步。

### 1.2 AgentGraph 镜像重建

- `docker compose build api` 成功；
- `docker compose up -d api` 重建并启动；
- 启动日志显示：
  - `applying 3.68.0 migration (v3_68_cancelled_terminal.sql)`
  - `database schema is up to date (v3.68.0)`
  - `Veynod 3.24.0 ready`
- 健康检查 `/healthz` 200，`/readyz` 200，`/dashboard/code/` 200。
- 镜像内已确认存在：
  - `code_projection_loop`
  - `gate_updates_from_evidence`
  - `cancel_kernel_run`

因此 v3_68 CANCELLED 终态、真实 gate evidence、AgentGraph→Kernel cancel 传播、后台投影都已进入线上镜像。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| AgentGraph 非集成 | **76 passed, 28 skipped** |
| Kernel 非集成 | **98 passed** |
| contracts | **5 passed** |
| 线上启动/健康 | health 200、readyz 200、Code 页面 200、v3.68.0 已应用 |

> 没有 AgentGraph 全量集成测试（缺 pgvector）；后台投影循环的真 DB + Kernel 集成没有自动化覆盖，只做了单测映射 + 线上启动检查。

---

## 3. 已知限制

1. 投影是**轮询**（30s），不是事件驱动；Kernel Outbox → AgentGraph consumer 仍未实现。
2. 批量上限 100；大量 code run 时可能滞后。
3. 每个 workspace 仍要调用一次 Kernel list；跨多个 Kernel/workspace 时会线性增加。
4. AgentGraph 仍是单 FastAPI 进程承载所有后台循环。
5. 代码仍未提交 git；`code_engine.py` 在 AgentGraph git 中可能仍是未跟踪文件。
6. AgentGraph 集成测试因 pgvector 缺失没有跑。

---

## 4. 下一步（Phase 1I / Phase 2）

1. 事件驱动投影（Kernel Outbox → AgentGraph consumer），替代轮询；
2. 统一 DagTaskCommandService 的其余命令；
3. 多 workspace/Repository Registry；
4. 1,000 Runner 容量复测；
5. Phase 2 Durable Agent Runtime（Agent Run/Step/Checkpoint/Model Gateway/Tool Runtime）。

---

## 5. 结论

AgentGraph 现在会在后台自动投影 Kernel 的 CANCELLED/FAILED/COMMITTED，并且重建后的线上镜像已经包含 v3_68、真实 gate evidence、取消传播和投影循环。**但投影仍是轮询，不是事件驱动；统一命令层、多租户调度和容量认证仍未完成。**
