# Phase 1 未解决风险

| 编号 | 风险与证据 | 后续阶段 |
|---|---|---|
| R1 | `attempt_id` 只在契约中定义，当前没有统一活动 Attempt 的物理所有权行；Host claim 与 Runtime start_run 仍可竞争。见 Phase 0 G02、`AgentGraph/app/services/tasks.py:689-723` 与 `task_lifecycle.py:234-344`。 | Phase 2/3：统一 CAS 和 fencing。 |
| R2 | Diagram Gate/Merge 和 Kernel Gate/Merge 仍并存；Kernel COMMITTED 不自动投影 Diagram/解锁下游。见 Phase 0 G03/G04、`AgentGraph/app/services/event_dispatch.py:340-388`。 | Phase 2：Kernel evidence 驱动 Diagram 幂等投影。 |
| R3 | `WorkScope.resources` 被保存和哈希保护，但数据库表/API 资源动作没有执行层授权实现；不得把其视为通行证。 | Phase 2/6：专门资源策略和负面测试。 |
| R4 | 根目录通配模式 `**/*` 因无法安全映射 Kernel 锁而拒绝；现有 Diagram 若使用该模式需先拆范围。旧已创建的无范围 Kernel Task 不能原地升级。 | Phase 2：迁移清单、明确批准的新 Attempt。 |
| R5 | 当前生产镜像/数据库版本本阶段未部署验证；本地真实集成证明功能路径，不等于线上可用。Gate 生产清单仍为 `local` Sandbox。 | Phase 6：镜像 digest、迁移 ledger、隔离与容量验收。 |
| R6 | 新 EventEnvelope 为契约定义，现有 outbox/inbox 尚未全面采用其 ID、版本和乱序规则。 | Phase 2/3：事件适配与回放测试。 |
| R7 | 含集成测试的较广回归在租约过期状态回退用例失败一次（LEASED，期望 READY），同一用例隔离重跑通过；全套没有绿灯。详见 TEST_REPORT。 | Kernel 所有权团队：排查 Gate Worker/Lease 回收竞态，固定独立回归后再作生产验收。 |
| R8 | 本机缺 `pgvector`，AgentGraph 全套测试在收集时中止；只验证了本阶段兼容适配器单元测试。 | AgentGraph 团队：在完整依赖环境执行全套及跨服务 E2E。 |
