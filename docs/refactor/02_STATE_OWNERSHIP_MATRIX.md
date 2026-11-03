# Phase 0 状态所有权矩阵

基准：`d37b7813d865abe50cf2c9e0f5ebb856e36af4de`。表中的“当前写入者”是代码事实；“建议所有者”来自目标规范，尚未实现。

| 状态 / 数据 | 当前写入者与证据 | 当前冲突或边界 | 建议所有者 |
|---|---|---|---|
| Diagram / Chunk DAG、依赖、写入路径 | `AgentGraph/app/services/diagrams.py:100-174,523-657` | `allowed_write_paths` 可为空，Chunk 与 Kernel Task 无强外键 | Diagram 是计划与冻结范围所有者 |
| Product Task / `active_run_id` | `AgentGraph/app/services/diagrams.py:723-766`；`AgentGraph/app/services/task_lifecycle.py:310-344`；`AgentGraph/app/services/tasks.py:678-746` | Runtime Run 与 Host claim 两套占有关系；`claim_task` 不检查 `active_run_id` | 单一执行所有权协调器 |
| Host Placement | `AgentGraph/app/services/scheduler.py:362-416,791-812` | Scheduler 排除现有 Run，但没有跨入口的原子占有协议 | Scheduler 仅分配已授权的执行尝试 |
| Product Task Lease | `AgentGraph/app/services/tasks.py:698-740`、`AgentGraph/app/services/leases.py:97-113` | 与 Runtime、Kernel Lease 不同域 | 单一 attempt 的可追踪租约映射 |
| Runtime Run / Runtime Lease | `agent_runtime/store.py:261-339,676-754`、`agent_runtime/worker.py:46-90` | Runtime Run 对 Product Task 的关联由 `active_run_id` 与 `product_task_id` 承载 | Runtime 独占推理状态 |
| Kernel Task / Write Set / Kernel Lease | `agent_runtime/tools/kernel.py:410-451,453-505`；`veynod_kernel/app/api/runtime_routes.py:145-207`；`veynod_kernel/app/mcp_server/syscalls.py:764-789` | Kernel 有真实写集检查，但写集由 Runtime 工具参数给出，未见 Diagram 路径绑定 | Kernel 独占代码执行边界 |
| Host Worktree / Git PR | `AgentGraph/cli/bin/runner.mjs:397-445,623-684` | 独立于 Kernel Worktree、Gate 和 Merge | 迁移期受控适配器 |
| Kernel Patch / Gate / Merge | `veynod_kernel/app/mcp_server/syscalls.py:764-895`；`veynod_kernel/app/services/gate_worker.py:75-150`；`veynod_kernel/app/services/merge_coordinator.py:214-352`；`veynod_kernel/app/services/verification_gate.py:451-481` | 有实际 Git 合并和持久队列；与 Diagram Gate/Merge 并行存在 | Kernel 独占验证与代码合并 |
| Diagram Submission / CI / Gate / Merge record | `AgentGraph/app/services/diagrams.py:850-895,955-1005,1100-1159,1466-1557` | 接受调用者提供的结果和 artifact；其 merge 是状态记录，不是 Git 合并 | Diagram 只投影 Kernel 已确认的事实 |
| Kernel / Runtime 事件与产品投影 | `veynod_kernel/app/services/event_bus.py:61-75`；`AgentGraph/app/services/event_dispatch.py:57-100,340-388`；`AgentGraph/app/services/code_engine.py:691-729` | 持久 inbox 存在；Kernel 事件投影未调用 Diagram 状态更新 | 事件驱动的幂等产品与 Diagram 投影 |

## 已确认关系

`diagram_chunks.task_key` 指向 Product Task 的自然键，见 `AgentGraph/app/services/diagrams.py:649-656,723-766`。`collab_tasks.active_run_id` 指向一个 Runtime Run，见 `AgentGraph/app/services/task_lifecycle.py:326-344` 与 `AgentGraph/sql/migrations/v3_75_unified_runtime.sql:16-21`。Runtime 的 `kernel_tasks` 表按 `run_id` 记录 Kernel Task，见 `agent_runtime/migrations/009_kernel_tasks.sql:8-18` 与 `agent_runtime/tools/kernel.py:438-449`。Host placement 用 `task_id/task_key` 指向 Product Task，见 `AgentGraph/sql/migrations/v3_61_runner_placements.sql:13-40`。未发现从 Chunk 到 Kernel Task 的强制唯一关联或统一 attempt 记录；这是仓库搜索和上述建表、创建路径的静态结论，生产数据关系 **NOT VERIFIED**。
