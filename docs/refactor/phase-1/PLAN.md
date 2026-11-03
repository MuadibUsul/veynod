# Phase 1 实施计划与范围

基准为 GitHub main `88626e3`，在独立分支 `codex/phase1-execution-contracts` 实施。Phase 0 文档保留其 `d37b781` 审计基准。遵循目标规范 §18、§27：本阶段只建立任务/范围/事件契约和 Write Scope 贯通，不提前接管 Diagram 状态投影或统一 Scheduler。

1. 对照 `kernel_delivery.py`、Runtime 工具、Kernel 创建/领取/提交路径确认字段含义。
2. 定义三个版本化 JSON Schema 与依赖无关的 WorkScope 解析器。
3. 兼容 Diagram `allowed_write_paths` 和普通 Product Task 旧写集，保留旧 API 行为。
4. Kernel 以新增可空字段保存 Task WorkScope 与 Lease Snapshot；重复创建拒绝换范围。
5. Submission 与 Gate 在实际 Diff 上验证路径、符号和 hash；旧无范围任务走原检查，scoped 任务拒绝旧无范围 Lease。
6. 以单元、组件及临时 PostgreSQL + 真 Git Worktree/Gate Worker 集成测试验证；记录未接线项。

涉及目录：`contracts/`、`AgentGraph/app/services/kernel_delivery.py` 与 `task_lifecycle.py`、`agent_runtime/tools/kernel.py`、`veynod_kernel/app/{api,engine,services,mcp_server,models.py}`、`veynod_kernel/sql/`、`deploy/Dockerfile.kernel` 和对应测试。本阶段的数据库变更仅增加可空列、约束及不可变触发器；不回填旧任务、不修改现有 Git 仓库或生产环境。
