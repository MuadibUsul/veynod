# Phase 2 实施计划

基准：`b8355c0`；分支 `codex/phase2-diagram-kernel`。按目标规范 §19、§27，仅处理 Diagram Chunk 到 Kernel 交付、证据投影和下游解锁。Phase 3 的多设备 Adapter/统一 Attempt 所有权不在本阶段冒充完成。

## 涉及模块与次序

1. 增加契约/数据库测试，覆盖冻结版本、单一 Kernel 绑定、旧 Diagram Gate/Merge 拒绝、重复/乱序投影、丢事件对账，以及真实 Chunk→Kernel Commit→下游解锁。
2. 为 Diagram 增加不可变版本快照和 Kernel 绑定字段/约束；旧图保持 legacy 模式，新图只有在活动 Kernel binding 存在时选择 Kernel 交付。
3. Diagram 派发写持久化任务和 Chunk Ready 事实；Kernel Task 创建以 Product Task 键幂等，绑定到 Diagram 版本、Chunk 和 Product Task；Kernel Lease/Worktree 与 WorkScope 由 Phase 1 路径承担。
4. Kernel 证据通过现有 outbox/inbox 触发，按 Kernel Task ID、提交 SHA、scope hash 在同一 AgentGraph 事务投影 Gate/Merge；复用 Diagram 的下游解锁逻辑。旧 Diagram submit/gate/merge/queue 接口对 Kernel-owned Chunk 拒绝，避免双 Merge。
5. 周期对账扫描未终结的 Kernel-owned Chunk，补投丢失的 Gate/Merge 事件；失败证据留在原 Product Task/Run 的关联记录并保持重试可见。
6. 跑最小测试、隔离数据库与真实 Git 集成，以及可运行的广回归；把剩余限制写入本阶段报告。

## 数据迁移、兼容和风险

AgentGraph 增量迁移只增加快照表和 Chunk 的可空 Kernel 关联列/约束，不回填旧 Diagram，不转移旧任务的交付权。Kernel 表不改，仍以 Phase 1 的 WorkScope/Lease 和既有 Gate/Merge 为权威。外部 Kernel 调用和 AgentGraph 事务不能原子提交，因此绑定重试与对账必须幂等。旧 Diagram 流程在没有 Kernel binding 时继续；已有人工 Gate/Merge 的历史记录不伪造 Kernel 证据。当前机器缺少 AgentGraph 的 `pgvector`，需在完整测试依赖环境运行其数据库测试。

## 验收

至少一个真实 Git 仓库、真实 Kernel API/Gate Worker 和 AgentGraph Diagram 的 Chunk，从冻结、派发、Kernel Lease/Patch/Commit 到 Diagram merged、下游自动派发；重复事件/对账不重复合并或派发。若环境或实现尚不能跑通，不标记 Phase 2 已通过。
