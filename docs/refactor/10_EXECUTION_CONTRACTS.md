# Phase 1：统一任务与执行契约

实施基准：`88626e3cda38b9915e277d9a9065efa980b1826f`（GitHub main 于 2026-11-02 的提交）。Phase 0 审计基准是较早的 `d37b781`；新增的 `kernel_delivery.py`、Kernel Worktree Lease 和 Runner 默认 Kernel 路径属于本阶段实际基准。本文件定义跨域契约，明确已接线与仍待后续阶段完成的部分。

## 1. 身份与映射

| 对象 | 当前物理键 | v1 契约字段 | 创建者 / 关联规则 |
|---|---|---|---|
| Diagram | `diagrams.id` BIGINT、`diagram_key` | `diagram_id`, `diagram_version` | Diagram 冻结后版本不可原地扩大写域；`AgentGraph/app/services/diagrams.py:544-657` |
| Chunk | `(diagram_id, chunk_key)` | `diagram_id`, `chunk_key` | `diagram_chunks.task_key` 指向 Product Task 自然键，见 `diagrams.py:649-656` |
| Product Task | `collab_tasks.id` UUID、`task_key` | `product_task_id`, `product_task_key` | `task_key` 在 workspace 内唯一，见 `AgentGraph/app/models.py:562-571,713` |
| Attempt | 旧路径没有独立物理行 | `attempt_id` UUID | 新契约按一次执行尝试生成；旧数据为 `null/unknown`，不得用 `run_id` 或 `placement_id` 冒充 |
| Runner Placement | `runner_placements.placement_id` 字符串 `plc_*` | `placement_id` 不强制 UUID | 一次放置决策的幂等键；见 `AgentGraph/sql/migrations/v3_61_runner_placements.sql:13-40` |
| Runtime Run | `agent_runs.run_id` UUID | `agent_run_id` | Product Task 的 `active_run_id` 与 Runtime `product_task_id` 关联；见 `AgentGraph/app/services/task_lifecycle.py:326-344` |
| Kernel Task | `dag_tasks.task_id` UUID | `kernel_task_id` | Host 路径 `kernel_delivery` 或 Runtime `create_task` 创建，Kernel `runtime_request_key` 去重；见 `AgentGraph/app/services/kernel_delivery.py:447-481`、`veynod_kernel/app/api/runtime_routes.py:161-209` |

`contracts/work-item.schema.json` 是目标记录形状。现阶段并未声称上述所有字段已持久化为同一行：`attempt_id` 及跨域单一活动所有权仍需 Phase 2/3 迁移。`contracts/identifiers.py` 已按仓库真实类型区分 UUID 对象与 `ten_*`、`DG-*`、`plc_*` 等不透明字符串。任何关联只在同一 tenant/workspace 内有效；下游 ID 缺失与指向其他 ID 必须区别处理。

## 2. WorkScope v1

规范定义在 `contracts/work-scope.schema.json`，唯一语义解析器在 `contracts/work_scope.py`。来源是冻结 Diagram `allowed_write_paths`；AgentGraph 兼容适配器将其包成带 `scope_version: 1` 的请求对象，Runtime 只传输，Kernel 规范化并计算 `scope_hash`。该哈希是规范 JSON（UTF-8、键排序、紧凑分隔符、集合排序去重）上的 SHA-256，覆盖路径、符号、资源和保护路径，不覆盖哈希字段本身。

规则：仓库相对路径，`/` 分隔；`*`、`?` 只匹配一段，`**` 独占一段可跨层级；大小写精确匹配。绝对路径、盘符路径、`..`、`.`、空段、控制字符和未支持的字符类拒绝。排除与保护路径优先于包含；未声明文件拒绝。符号范围可进一步收窄路径范围，空符号列表不扩大路径权限。根目录通配符不能映射成安全 Kernel 锁，因此拒绝创建；`src/**` 映射到保守锁 `src`，实际提交仍按原模式与排除项精确检查。高影响资源字段随哈希保存，但数据库表/API 资源权限尚未由代码执行器实施，不可据此授权数据库操作。

路径从 `diagram_chunks.allowed_write_paths` → `collab_tasks.input_context.allowed_write_paths` → `kernel_delivery.work_scope_for` 或 `task_lifecycle.start_run` → Runtime Kernel Tool → Kernel `/v1/runtime/tasks`。Kernel Task 在创建时保存不可变 `work_scope/scope_hash`（`v4_017_work_scope.sql`、`task_bootstrap.py`），Kernel Lease 在领取时保存 `lease_scope/lease_scope_hash` 快照（`kernel_service_impl.py`）。`/v2/syscalls/submit_patch` 先验证 Kernel Lease 和写集，再用 Lease WorkScope 验证 Diff 文件与触及符号（`syscalls.py:_validate_submission`）；Gate 在 Git apply 后再次按真实 staged 文件、symlink 路径和 AST 改动符号验证（`verification_gate.py:_run_attempt/_check_ast_scope`）。路径授权的新文件可在祖先锁保护下提交；显式符号限制会禁止未列出的 AST 改动和文件级改动。范围变化必须新 Task/Attempt；相同 `request_key` 携不同范围返回 409，旧无范围 Lease 不可用于 scoped Product Task 提交。

兼容映射：

| 旧字段 / 情形 | v1 映射 | 失败规则 |
|---|---|---|
| Diagram `allowed_write_paths` | `paths.include`，`paths.exclude=[]` | 空、无安全锁前缀或非法路径由 Kernel 422 拒绝 |
| Product Task 显式 `input_context.work_scope` | 原样传输给 Kernel 作唯一解析 | 非对象不当作有效 WorkScope；与现存 Lease 不同则拒绝复用 |
| 旧 `write_set_symbols` / `symbol`，无 Diagram | 保持原 Kernel 写集路径 | 不自动伪造冻结 Diagram 来源 |
| 旧已租出的无范围 Kernel Task | 不回填虚假范围 | scoped Product Task 拒绝复用，需新 Task/Attempt |

## 3. Lease、Fencing 与状态权威

现有 Product Task Lease 使用 `lease_epoch`，Runtime Run 使用自己的 fencing，Kernel 使用租约 token hash、runner 身份和到期时间；这些已存在但未合并成统一 owner 记录。目标规则是同一 `(workspace_id, product_task_id, attempt_id)` 至多一个活动执行所有者，状态在 `unassigned → placed → leased → verifying → committed/failed/cancelled` 中单向或按明确重试迁移；所有重试必须携 attempt 与 fence。Scheduler 只能放置，Runtime 只能拥有推理，Kernel 独占代码 Lease、Gate、Git Merge；Diagram 只拥有计划和提交事实投影。现阶段 Product Task 的 Host claim 与 Runtime start_run 互斥问题仍在，见 Phase 0 的 G02；本阶段没有宣称它已解决。

对于 scoped Kernel Task，Task WorkScope 创建后不可更新（数据库触发器拒绝）；Lease 领取时复制快照/哈希；提交必须携当前 token，Kernel 对 token、runner、到期、范围 hash 和实际 Diff 逐层验证。重复任务创建以 `(workspace_id, request_key)` 去重，重复键若范围不同直接冲突。每次范围批准应使用新的 request key 和 attempt，不能修改原任务。

## 4. Event Envelope 与 Outbox / Inbox

`contracts/event-envelope.schema.json`、`contracts/events.py` 定义 `event_id/event_type/schema_version/event_version/tenant_id/workspace_id/aggregate_type/aggregate_id/aggregate_version/producer/occurred_at/correlation_id/causation_id/idempotency_key/payload_ref/payload`，并提供 Diagram、Chunk、Run、Attempt、Kernel Task、Lease、Submission、Placement 的可空关联字段。新事件 ID 采用不透明的 `evt_*`；tenant、Chunk、Placement 的实际键也是不透明字符串，workspace、Run、Kernel Task 当前用 UUID。事件投递规则：生产者将领域状态变更与 outbox 同事务提交；消费者以 `(source, source_instance, source_seq)` 或事件 ID 幂等接收，以 aggregate version 拒绝倒序投影，处理成功后再 ACK；失败可重试、死信可重放。

| 当前实际事件 | 目标语义 | 适配约束 |
|---|---|---|
| Diagram `DIAGRAM_CONFIRMED` / `DIAGRAM_FROZEN` | `diagram.confirmed` / `diagram.frozen` | 同一 Diagram 版本一次投影 |
| Diagram `CHUNK_BLOCKED` / `CHUNK_UNBLOCKED` / `CHUNK_MERGED` | `chunk.blocked` / `chunk.unblocked` / `chunk.merged` | 必须带 diagram + chunk 身份 |
| Kernel `GATE_PASSED` | `kernel.gate.passed` | 只证明 Kernel Gate；不可单独推导 Diagram merged |
| Kernel `TASK_READY` | Kernel Task 就绪事实 | 缺少 Chunk 关联，不能直接当成 `chunk.ready` |

其余旧事件名不能凭相似字面自动映射为目标状态，需要 Phase 2/3 逐一补充关联、权威版本和证据规则。现有 Kernel event outbox 与 AgentGraph inbox 已有部分机制（`veynod_kernel/app/services/event_bus.py:61-75`；`AgentGraph/app/services/event_dispatch.py:57-165,340-388`），**尚未改成统一 Envelope，也尚未投影 Diagram**。这些接线属于 Phase 2/3，不以本契约文档冒充实现。

## 5. 阶段边界

本阶段完成的是 WorkScope 语义与 Diagram/Runtime/Host → Kernel Task → Lease → Submission/Gate 的写域链，以及 JSON 契约。Phase 2 才统一 Chunk 状态投影、Gate/Merge 所有权；Phase 3 才建立跨 Host/Runtime 的单一活动 Attempt。生产隔离、1000 Runner 容量和完整端到端无人值守仍未验证。
