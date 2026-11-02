# Phase 0 执行路径清单

基准：本地 `d37b781`。入口并不等于真实执行器；下表只记录代码可见路径。

| 入口 | 任务 / 领取 | Worktree / Lease | Patch / Gate / Merge | 回写 |
|---|---|---|---|---|
| Diagram `freeze/dispatch` | `diagrams.py:544-657,686-782` 创建 `DG-*` Product Task；Scheduler `scheduler.py:392-497` 发 Host work placement | Host Runner `AgentGraph/cli/bin/runner.mjs:397-445,623-684` 建本地 Git Worktree；Agent 经 `tasks.py:678-746` 领取 Product Lease | Agent 提交分支/PR；Host review/merge prompt `runner.mjs:205-257` 调 Diagram Gate、queue check、merge record | `diagrams.py:1514-1557,1647-1668` 直接标记 merged、解锁下游 |
| 手工 REST/MCP `create_task` | `AgentGraph/app/mcp_server/server.py:803-880` → `AgentGraph/app/services/tasks.py:509-677` 建普通 Product Task；可经 Scheduler 放置或显式 `/run` | Scheduler→Host Product Lease，或 Runtime Run→Kernel Lease，取决于后续入口 | Host 可走 PR/任务 review；Runtime 可走 Kernel v2 提交 | Product Task 状态由任务 API 或 Run/Kernel 投影更新；无 Diagram Chunk 时无 Diagram 回写 |
| Product Task `/run` / `/agent-run` | `AgentGraph/app/api/agent_run_routes.py:59-74` → `agent_runtime.py:208-231` → `task_lifecycle.py:234-394` 建 Runtime Run | Runtime Worker `agent_runtime/worker.py:46-90` 领取 Run Lease；Kernel 工具 `agent_runtime/tools/kernel.py:410-505` 建 Kernel Task、领取 Kernel Lease | `kernel.py:541-585` 提交 `/v2/syscalls/submit_patch`；Kernel Gate Worker 与 Merge Coordinator 真正验证/合并 | `event_dispatch.py:340-360` → Product Task；`task_lifecycle.py:774-830` 需要 Kernel evidence 才进审核；不见 Diagram 更新 |
| Code Engine `/code/runs` | `AgentGraph/app/services/code_engine.py:273-370` 先建 Product Task，默认启动 Runtime Run | 同上 | 同上；`CODE_ENGINE_LEGACY_DIRECT` 可改为直接创建 Kernel Task `code_engine.py:308-350` | `code_engine.py:691-729` 的旧 Kernel 投影只按 `input_context.kernel.task_id` 匹配，默认 Runtime 路径依赖 `task_lifecycle` 投影 |
| Kernel 原生 CLI Runner / 直接 Kernel API | `veynod_kernel/app/api/runtime_routes.py:145-207` 或 Kernel 管理入口创建 Task；`veynod_kernel/cli/bin/runner.mjs:560-592` 领取 | CLI `runner.mjs:465-483` 建 detached Git Worktree；Kernel Lease 由 Bundle / Engine 管理 | `runner.mjs:629-675` 收集 diff、调用 v2 submit；Kernel Gate / Merge | Kernel 事件；只有存在 Product 关联时才可能投影 Product Task；Diagram 无默认关联 |
| 旧 Kernel v1 同步提交 | `veynod_kernel/app/mcp_server/syscalls.py:990-1023` | Kernel Lease | 默认 `VN_LEGACY_SUBMIT_MODE=reject`，显式 `sync` 才可用，见 `veynod_kernel/app/config.py:263-269` | 迁移兼容路径，当前部署是否启用 **NOT VERIFIED** |

## 入口与重复实现搜索依据

- Diagram API/MCP：`AgentGraph/app/api/v1_routes.py:1951`、`AgentGraph/app/mcp_server/server.py:2116` 调 Diagram merge；`diagrams.py` 本身无 Kernel API 调用。
- Scheduler 同时生成 work、review、merge step：`AgentGraph/app/services/scheduler.py:392-497,555-812`。
- Kernel 同时可由 Runtime 工具、Kernel CLI 和直接 API 驱动：`agent_runtime/tools/kernel.py:410-585`、`veynod_kernel/cli/bin/runner.mjs:465-675`、`veynod_kernel/app/api/runtime_routes.py:145-207`。
- Kernel 事件先写持久 outbox，再由 AgentGraph inbox 消费：`veynod_kernel/app/services/event_bus.py:61-75`、`AgentGraph/app/services/event_dispatch.py:57-165,340-388`。

## 覆盖情况

`AgentGraph/tests/test_diagrams.py:87-1268` 和 `test_loop_steps.py:52-120` 覆盖 Diagram 状态流，但本机因测试数据库条件未满足全部跳过。`veynod_kernel/tests/test_merge_coordinator.py:143-240` 测试 fencing 与崩溃恢复。`AgentGraph/cli/test/runner.test.mjs` 测 Host Runner 自身；未发现一个在本次环境执行的 Diagram → Runtime/Runner → Kernel Gate/Merge → Diagram 下游解锁 E2E。此“未发现”是当前仓库测试清单的静态审计结论，不能代替完整运行验证。
