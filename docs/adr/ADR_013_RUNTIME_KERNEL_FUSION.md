# ADR-013..018：Runtime 与 Kernel 融合（本次重构）

状态：**Accepted**（2026-11-01，Phase 1 冻结）。取代/细化 ADR-INDEX 中同主题的 Proposed 条目。

---

## ADR-013 KernelToolRuntime 作为 Runtime 调用 Kernel 的唯一入口

**背景**：`agent_runtime.gateway.HTTPToolRuntime` 是通用 HTTP 工具，部署默认指向 `scripts/runtime_tool_server.py`（只有 `echo`）。
Runtime 与 Kernel 没有任何真实连接。

**候选**：
A. 让 Runtime 直接调用 Kernel admin API（简单，但 admin key 进入 Runtime 配置，且 admin 面无 lease/幂等语义）。
B. 新建一层 SDK 直接读 Kernel 数据库（破坏边界，不可接受）。
C. 在 Runtime 实现 `KernelToolRuntime`，以**服务身份 + 工作区上下文**调用 Kernel 的 syscall/runner 平面。

**决策**：C。

- 覆盖 12 个 capability：create_task、pull_task_bundle、submit_patch、get_submission、query_symbol_signature、
  get_task_evidence、get_task_status、cancel_task、request_task_split、escalate_contract_defect、
  inject_dependency_task。
- 复用 Kernel 既有 syscall 语义（lease_token、写集校验、202 异步提交、幂等键）。
- 凭据：Runtime 使用工作区绑定的 `vnk_` runner token（syscall 平面）与 `X-Veynod-Service-Key`（服务面），
  **不使用 admin key，不暴露给模型/浏览器**。
- 每次调用写入 `agent_runtime.runtime_tool_audit`（含 latency、error_class、trace_id）。

**取舍**：Runtime 需要理解 Kernel 的错误码与 202 语义；换来唯一执行链与完整审计。

**验收**：`agent_runtime/tests/test_kernel_tool.py` 覆盖 capability 注册表、幂等重放、错误分类；
真实 E2E：Runtime → pull_task_bundle → submit_patch(202) → gate → commit → Runtime resume。

---

## ADR-014 外部等待：`WAITING_EXTERNAL` + Durable Event 唤醒（禁止轮询作为唯一机制）

**背景**：Kernel 门禁与合并是异步的（`POST /v2/syscalls/submit_patch` → 202）。
`LoopController.step` 目前同步执行工具，Worker 会一直占用 run lease。

**决策**：
- `ToolResult` 增加 `deferred` 载荷；`LoopController` 收到后持久化 `external_invocations`
  （submission_id、deadline），Run → `WAITING_EXTERNAL`，**释放租约**，返回。
- 唤醒源：Kernel `kernel_events` → `event_inbox` → `runtime_wakeups`；
  Runtime 的 consumer 按游标读取并幂等写入（`UNIQUE(kind, ref_id, kernel_event_seq)`）。
- 兜底扫描：`ReconciliationLoop` 定期检查超期 deferred（只作为安全网，不作为主路径）。
- 恢复：Worker 领取 Run 时先消费 wakeups，把 `external_invocations` 置 FINAL，Run 回到 RUNNING。

**取值**：多一次 event 表跳转，换取"长门禁不占 Worker"和崩溃可恢复。

**验收**：门禁耗时 > 租约 TTL 时 Run 仍正确完成；Worker 在等待期间被 kill 后能恢复；
重复 wakeup 不产生重复提交（`submission_id` 幂等）。

---

## ADR-015 产品任务状态只有一个写入者

**背景**：`code_engine._sync_lifecycle`、`agent_runtime.get_task_run`、
`scheduler.py` 三处都能改 `collab_tasks.status`。

**决策**：新增 `AgentGraph/app/services/task_lifecycle.py` 作为唯一写入者；
其余模块只能读取 Runtime/Kernel 状态并调用它。写入必须：
- 单调（终态不可回退；REVIEWING/DONE 不被迟到事件改写）；
- 幂等（`(task_key, action, dedupe_key)` 去重，dedupe_key 来自 `source:seq`）；
- 审计（`record_audit` + `workspace_events`）。

**验收**：代码检索不存在其他 `CollabTask.status =` 的跨域写入；
并发投影同一事件两次只产生一次副作用（测试覆盖）。

---

## ADR-016 syscall 平面显式 Workspace 路由

**背景**：Kernel syscall 依赖 `VN_DEFAULT_WORKSPACE_ID`（单 workspace），
与"多项目/多仓库"目标冲突；且 AgentGraph 用全局 `KERNEL_WORKSPACE_ID`。

**决策**：
- syscall 接受可选 `X-Workspace-Id`；必须是 token 所属 workspace，否则 403；缺省回落现有行为。
- `pull_task_bundle`/`submit_patch`/`verify_active_lease` 等使用请求级 workspace，而非进程级默认值。
- AgentGraph 用 `kernel_bindings`（workspace+project+repo → kernel workspace/token）路由；
  同一 Kernel 可承载多 workspace，分片时 binding 指向不同实例，对产品透明。
- 移除"全局 KERNEL_WORKSPACE_ID 是唯一仓库配置"的用法（保留兼容读取）。

**验收**：同一 Kernel 上两个 workspace 并行执行互不串扰；跨 workspace token 被 403。

---

## ADR-017 Kernel Runner CLI 降级为开发工具

**背景**：`veynod_kernel/cli` 会 claim DAG 任务并提交 patch，与 Runtime 成为同一任务的第二个执行者。

**决策**：产品默认执行者为 Runtime。Kernel Runner 保留用于开发/兼容；产品侧排他：
- 有活动 Agent Run 的产品任务不进入 host placement（`scheduler._fits` 排除）；
- `kernel_bindings` 默认不向 runner 发 task token（仅 Runtime 持有）。
- 文档明确标注 CLI 为 dev tool。

**验收**：`scheduler` 测试覆盖"有活动 Run 的任务不被放置"；E2E 中同一任务只被 Runtime 认领。

---

## ADR-018 废弃 `veynod_platform` 与三个独立技术入口

**背景**：`veynod_platform` 是带自有 SQLite store 的第三产品面；
`/dashboard/code|agent-runs|projects` 是三个并列技术控制台。

**决策**：
- `veynod_platform` 标记 deprecated，移出默认部署与导航；代码保留一个版本供参考。
- 三个页面降级为 `/dashboard/internal/*`（管理员权限），不做产品导航；
  Task Detail 承担 Code/Loop/Gate/Delivery 展示。
- 不通过"隐藏菜单"完成融合：后端路由权限与信息架构同步调整。

**验收**：普通用户导航只有 Project/Task/Loop/Agent/Documents；
访问旧路径被重定向或 403；Task Detail 能展示完整执行链证据。
