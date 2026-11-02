# Phase 1 实施报告

基准提交：`88626e3`。主要契约见 [10_EXECUTION_CONTRACTS.md](../10_EXECUTION_CONTRACTS.md)。

| 变更 | 实际落点 | 可验证效果 |
|---|---|---|
| WorkScope v1 及三份 JSON Schema | `contracts/work_scope.py`、`work-scope.schema.json`、`work-item.schema.json`、`event-envelope.schema.json` | 一处规范化/匹配/哈希逻辑，路径排除、保护路径、符号收窄、保守 Kernel 锁前缀 |
| 身份与事件契约对齐真实键 | `contracts/identifiers.py`、`events.py`、`README.md` | tenant/Chunk/Placement 接受不透明 ID，UUID 对象保持 UUID；定义 Diagram/Attempt 事件名 |
| Diagram / Product Task → Kernel 兼容适配 | `AgentGraph/app/services/kernel_delivery.py` | 从 Diagram 原始路径生成 WorkScope 请求，旧符号任务保持旧路径；旧无范围或过期范围 Lease 不可用于 scoped 提交 |
| Product Task → Runtime → Kernel 传递 | `AgentGraph/app/services/task_lifecycle.py`、`agent_runtime/tools/kernel.py` | Runtime 使用 Product Task 的冻结范围，不接受模型参数扩张写集；默认 Kernel endpoint 退路仍携范围 |
| Kernel Task / Lease Snapshot | `veynod_kernel/sql/v4_017_work_scope.sql`、`app/models.py`、`services/task_factory.py`、`task_bootstrap.py`、`api/runtime_routes.py`、`engine/kernel_service_impl.py` | 创建时存不可变范围/hash；领取时存 Lease 快照/hash；同一创建键换范围返回 409 |
| 两层提交校验 | `veynod_kernel/app/mcp_server/syscalls.py`、`services/verification_gate.py` | 接收前检查租约/写集/范围；Git apply 后按真实 staged 文件、符号链接路径和 AST 改动符号再次检查 |
| Kernel 镜像提供共享契约 | `deploy/Dockerfile.kernel` | 生产镜像包含 `contracts` 包；Runtime 镜像此前已经包含 |

没有修改 Diagram Gate/Merge、Scheduler 活动所有权、事件 dispatcher 或生产部署配置。它们在目标规范中分别属于后续阶段。所有新范围字段只为新 scoped Kernel Task 使用；旧任务仍可沿原路径执行，现存 scoped Diagram Task 遇旧无范围 Lease 时会被明确拒绝并要求新 Attempt。
