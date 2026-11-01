# ADR_INDEX — 架构决策记录索引

> 状态：**Proposed**（Phase 0 产物；尚未由架构评审正式接受）
> 说明：本索引记录“需要决定什么、推荐结论、影响、验收”，不是已实施事实。
> 相关文档：`CURRENT_STATE.md`、`TARGET_ARCHITECTURE.md`、`DATA_OWNERSHIP.md`、`STATE_MACHINE_INVENTORY.md`、`INTEGRATION_MAP.md`、`RISK_REGISTER.md`、`REFACTOR_ROADMAP.md`。

---

## 0. ADR 模板

每个 ADR 必须包含：

- 背景 / 需要解决的问题；
- 候选方案；
- 决策（推荐/已接受）；
- 取舍；
- 安全影响；
- 迁移影响；
- 验收方法；
- 状态（Proposed / Accepted / Superseded）；
- 关联风险/阶段。

---

## 1. 索引表

| ADR | 标题 | 推荐结论 | 状态 | 阶段 | 关联风险 |
|---|---|---|---|---|---|
| ADR-001 | Bounded Context Ownership | AgentGraph=Product Control；Agent Runtime=新领域；Kernel=Code Execution；不合并 schema | Proposed | Phase 1 | R-006/R-007/R-019 |
| ADR-002 | Product Task vs Agent Run vs DAG Task | 三者独立权威；用统一 ID 链关联，不互写 | Proposed | Phase 1 | R-017/R-019 |
| ADR-003 | Durable Agent Loop | 持久化状态机 + Checkpoint + 幂等工具；禁止进程内 while | Proposed | Phase 2 | R-020 |
| ADR-004 | Agent Orchestration vs Kernel Scheduling | Orchestration 决定“做什么”；Kernel 决定“如何安全调度执行” | Proposed | Phase 1/2 | R-006/R-023 |
| ADR-005 | Event Delivery | 事务 Outbox + 至少一次 + 消费者幂等 + 版本/关联 ID；不承诺 exactly-once | Proposed | Phase 1/3 | R-016/R-029 |
| ADR-006 | Database Isolation | 保留 AgentGraph/Kernel 独立 DB；Agent Runtime 独立 schema；不做跨库 FK | Proposed | Phase 1 | R-024/R-025 |
| ADR-007 | Repository Registry | 引入 Repository 一等实体；workspace 不再等同 repo | Proposed | Phase 4 | R-023 |
| ADR-008 | Execution Cell | Repository 级分片/Cell；Cell Manager 不做单点执行 | Proposed | Phase 4 | R-023 |
| ADR-009 | Scheduler Authority | DB 为事实；Rust 为可重建决策；authority lease + fencing；Rust 不是唯一路径 | Proposed | Phase 1/4 | R-009/R-010 |
| ADR-010 | Snapshot & Write Set | 绑定 base/snapshot/AST/read/write/contract；动态写集需 Kernel 授权 | Proposed | Phase 5 | R-013/R-014/R-015 |
| ADR-011 | Verification & Merge | Gate 证据可追溯；merge 临界区复核 task lease/submission；CAS | Proposed | Phase 1/6 | R-001/R-002/R-012 |
| ADR-012 | Sandbox Isolation | 生产禁用 local；Docker/K8s 非 root/网络隔离/资源限制；评估 gVisor/Kata | Proposed | Phase 6 | R-004/R-035 |
| ADR-013 | Model Gateway | 统一 Provider/路由/限流/重试/计量/模型版本；禁止硬编码 | Proposed | Phase 2 | R-020 |
| ADR-014 | Tool Runtime | 统一 tool 注册/schema/权限/风险/幂等/网络/审计 | Proposed | Phase 2 | R-020 |
| ADR-015 | Observability | OpenTelemetry；request→task→run→kernel_task→submission→commit→delivery 追踪；敏感内容脱敏 | Proposed | Phase 1/7 | R-016/R-032 |
| ADR-016 | Capacity Model | 区分 Connected/Active/Execution/Mutation；Gate 按资源定容；100/500/1,000/5,000/10,000 分阶段 | Proposed | Phase 8 | R-021/R-022 |
| ADR-017 | Deployment Topology | 模块化服务起步；API/Scheduler/Worker/Gate/Merge 可独立扩容；不先拆微服务 | Proposed | Phase 4/8 | R-039 |
| ADR-018 | Migration Strategy | Strangler + Feature Flag + 对账 + 回滚；不 Big Bang | Proposed | All | R-024/R-025 |

---

## 2. ADR 摘要

### ADR-001 — Bounded Context Ownership

**背景：** 当前 AgentGraph 与 Kernel 没有合并，但存在两条代码路径、合成证据、跨域状态不一致。

**候选：**
A. 合并为单一 monorepo/schema；
B. 保留 AgentGraph/Kernel 边界，新增 Agent Runtime；
C. AgentGraph 吞并 Kernel。

**推荐：B。** AgentGraph=Product Control；Agent Runtime=Agent Loop；Kernel=Code Execution。数据库逻辑隔离，跨域通过 API/事件。

**取舍：** 需要维护契约/投影/对账；但避免 monorepo 耦合和 Kernel 产品化。

**安全/迁移：** 不让 AgentGraph 用户 token 直达 Kernel admin；按 Workspace/Repository 渐进切流。

**验收：** 三个领域各有明确表/写入权威/API；无跨库直接写。

---

### ADR-002 — Product Task vs Agent Run vs DAG Task

**背景：** 当前 AgentGraph `collab_tasks` 与 Kernel `dag_tasks` 双状态；AgentGraph 运行中仍 READY；无 Agent Run。

**推荐：** 三个对象独立权威：

- Product Task：AgentGraph，业务目标/审批/交付；
- Agent Run：Agent Runtime，执行生命周期/Step/Checkpoint；
- DAG Task：Kernel，工程任务/lease/attempt/gate/merge。

用统一 ID 链关联，不互写；AgentGraph 只投影 Kernel/Runtime 的权威结果。

**验收：** 任一状态只有一个写入方；跨域投影可重放；取消传播有测试。

---

### ADR-003 — Durable Agent Loop

**背景：** 当前模型循环在外部 runner 进程，平台不持久化 Run/Step/Checkpoint；崩溃不可恢复。

**推荐：** 持久化状态机 + Checkpoint + 幂等 Tool Runtime；每次模型决策/工具调用前后写 checkpoint；大内容存对象存储；禁止无限重试。

**验收：** 进程重启恢复；重复事件不重复副作用；工具结果可追溯；取消传播；预算阻断；历史可重放为相同业务状态。

---

### ADR-004 — Agent Orchestration vs Kernel Scheduling

**背景：** AgentGraph placement scheduler 与 Kernel DAG scheduler 都叫 scheduler，职责不同。

**推荐：** Orchestration 负责目标/计划/拆分/委派/重规划/完成判断；Kernel 负责 DAG 可执行性/准入/lease/fencing/资源公平。Orchestrator 通过 Workflow Adapter 调 Kernel API，不读 Kernel DB。

**验收：** 两类调度有独立 API/指标/测试；Orchestrator 不直接改 `dag_tasks`。

---

### ADR-005 — Event Delivery

**背景：** AgentGraph 触发器 outbox 与 Kernel event_outbox 两套；SSE id/seq 不一致；无 correlation/DLQ。

**推荐：** 事务 Outbox；至少一次；消费者幂等；统一 envelope（event_id/type/version/tenant/workspace/project/aggregate/version/occurred_at/correlation/causation/producer/payload_ref）；大型 artifact 用引用；`LISTEN/NOTIFY` 仅唤醒，不是唯一持久化。

**验收：** 事件重复/乱序测试；消费者 inbox；DLQ/重放；SSE 用 seq；跨域追踪。

---

### ADR-006 — Database Isolation

**背景：** 当前 AgentGraph 48 表 + Kernel 15 表独立 DB；Kernel ORM 只列 7 表；迁移 ledger 不完整。

**推荐：** 保留独立数据库；Agent Runtime 独立 schema/database；逻辑 schema/迁移/权限隔离；不合并 48+15 表；不跨库 FK；跨域 ID 通过协议/索引字段关联。

**验收：** 每个 schema 独立迁移/回滚；无跨库写；数据一致性检查。

---

### ADR-007 — Repository Registry

**背景：** 当前 `workspaces.repo_path` 混同 workspace 与 repo；多 workspace 可指向同一 repo，锁按 workspace 隔离，存在并发风险。

**推荐：** 引入 Repository 实体（repository_id、repo_path、main_branch、tenant/workspace/project、placement）；Repository 是并发/Snapshot/锁/Git 协调边界；Workspace 是团队/权限边界。

**验收：** 一个 Cell 多 Repository；同一 repo 不同 workspace 不会绕过锁；Repository 迁移有 fencing。

---

### ADR-008 — Execution Cell

**背景：** 单 Kernel 实例主要服务一个 repo；无法水平扩展/故障隔离。

**推荐：** Repository 级分片；Cell 具备 ID/健康/容量/Placement/API/Worker/Gate/Merge/版本/心跳；Cell Manager 只做注册/容量/Placement，不做所有任务执行；Repository 迁移需租约交接/Fencing。

**验收：** 多 Cell 独立扩容；Cell 故障恢复；无双重 Authority；单 Tenant 不无限挤占。

---

### ADR-009 — Scheduler Authority

**背景：** Rust SchedulerCore 是可选决策路径；`scheduler_authorities.fencing_token` 不 fence 写；SQL fallback 一等。

**推荐：** PostgreSQL 是持久化事实；Rust 是可重建高性能状态；authority lease 只用于选举/加载；任务写由条件 UPDATE/fencing 保护；Rust 不是唯一事实；重启从 DB 恢复；只有基准证明瓶颈才重构 Rust。

**验收：** Rust/SQL 决策一致；stale core 不覆盖；authority 丢失回退 SQL；重启恢复；无内存唯一事实。

---

### ADR-010 — Snapshot & Write Set

**背景：** 当前有文件/符号锁和 SPLIT 守恒，但无持久化 Snapshot、无通用动态写集扩展、INJECT 可绕过。

**推荐：** 任务绑定 repository/base_commit/artifact_snapshot/AST/dependency/read/write/contract；四层冲突（File/Symbol/Contract/Dependency）；动态写集必须 Kernel 授权；提交前重新验证；测试修改源码要重新验证或拒绝。

**验收：** 同符号冲突/无关并行/契约变更/过期快照/动态扩展/无静默覆盖测试。

---

### ADR-011 — Verification & Merge

**背景：** 四阶段 gate、work lease、CAS、reconcile 已有；但 AgentGraph 合成证据、merge 不复核 task lease、tree merge 未验证、多 submission 假成功。

**推荐：** Gate 证据来自 Kernel 持久化；Merge 临界区复核 task status/lease token/submission status；CAS + per-repo 串行；旧 worker 不能覆盖；冲突/失败有明确状态；AgentGraph 只投影证据。

**验收：** 过期 lease 不能 merge；旧 worker 被 fence；失败不污染 main；证据可追溯；tree merge 端到端测试。

---

### ADR-012 — Sandbox Isolation

**背景：** 默认 `local` 在主机执行 Agent 代码；Docker 模式有基本 flag 但无镜像/测试/非 root/seccomp/网络策略。

**推荐：** 生产禁用 `local`；Docker/K8s 非 root、只读 root、tmpfs、CPU/Memory/PID/disk、网络隔离/allowlist、无宿主 docker socket、临时凭证、审计；高风险多租户评估 gVisor/Kata。

**验收：** 不可信代码不能读宿主敏感目录/访问网络/逃逸；资源限制生效；凭证不进日志；负向测试。

---

### ADR-013 — Model Gateway

**背景：** 当前没有平台模型调用；外部 runner 自行调用模型，无法统一计量/路由/审计。

**推荐：** 统一 Provider Adapter、模型注册/选择/路由、超时/限流/重试/失败切换、Token/费用、请求关联、Prompt/Response 安全、模型版本；路由决策可审计。

**验收：** 所有模型调用经 Gateway；实际模型/版本记录；成本可核算；Provider 故障可切换。

---

### ADR-014 — Tool Runtime

**背景：** 当前 MCP 有 113 工具和 24h 幂等，但没有统一 tool 注册/风险/权限/网络/资源协议。

**推荐：** 统一 tool_id/version/schema/permissions/risk/execution_mode/timeout/idempotency/network/resource/audit；执行前权限+预算；高风险人工审批；副作用对账。

**验收：** 未授权工具失败；参数/schema 校验；幂等/补偿；审计。

---

### ADR-015 — Observability

**背景：** 当前有 metrics/SSE 统计，但没有 request→commit 的统一 trace/correlation。

**推荐：** OpenTelemetry；统一 trace 链：request_id→product_task→agent_run→workflow→kernel_task→attempt→tool→submission→verification→commit→delivery；模型输入/输出/工具参数默认不写 trace，独立隐私/脱敏/权限。

**验收：** 任一交付可追溯到请求/任务/attempt/commit；延迟分内部/Provider；敏感内容不泄露。

---

### ADR-016 — Capacity Model

**背景：** 历史/当前容量口径混乱；100 已完成但 P99 失败；500/1,000 未通过。

**推荐：** 区分 Connected/Active Run/Execution/Mutation/Gate/Merge；Gate 按 CPU/测试时间定容；分阶段 100/500/1,000/5,000/10,000；每轮记录源码 commit/迁移/环境/沙箱；至少 3 次重复；不把连接/受理当交付。

**验收：** 目标负载无非终态；P99/错误率/池超时达标；容量/成本报告可复现。

---

### ADR-017 — Deployment Topology

**背景：** 当前 AgentGraph 单 FastAPI 进程；Kernel API/Worker 分离；Gate/Merge 在 worker；Platform 死代码。

**推荐：** 模块化服务起步；Control API、Agent Runtime API/Worker、Kernel API、Scheduler、Execution Worker、Gate Worker、Merge Worker、PostgreSQL、Object Storage、Outbox、Observability；按资源特征拆，不先拆微服务；Kernel Admin 内网。

**验收：** API/Worker/Gate/Merge 可独立扩容；故障隔离；部署/回滚手册。

---

### ADR-018 — Migration Strategy

**背景：** 当前 code_engine 未跟踪、旧/新路径并存、迁移无回滚、文档漂移。

**推荐：** Strangler + Feature Flag + Adapter + 版本化 Contract；按 Workspace/Repository/Tenant 切流；对账；不做 Big Bang；双写仅明确窗口；回滚不丢 Git commit；旧接口下线有版本/公告。

**验收：** 新旧路径可切换；对账一致；回滚演练；无重复执行/提交。

---

## 3. ADR 接受门槛

- ADR-001/002/004/006 必须在 Phase 1 开始前 Accepted；
- ADR-005/009/011/012/018 必须在 Phase 1 结束前 Accepted；
- ADR-003/013/014 在 Phase 2 前 Accepted；
- ADR-007/008/016/017 在 Phase 4/8 前 Accepted；
- 每个 ADR 的验收必须有测试/证据，不接受“文档已写”。

---

## 4. 状态维护

| 状态 | 含义 |
|---|---|
| Proposed | 已提出，未评审 |
| Accepted | 架构评审通过，进入实施 |
| Superseded | 被新 ADR 替代 |
| Rejected | 明确不采用 |

当前全部为 **Proposed**。Phase 0 只负责提出，不代替架构评审。


---

## 2. 本轮已接受（2026-11-01）

统一重构的最终决策见 [ADR_013_RUNTIME_KERNEL_FUSION.md](ADR_013_RUNTIME_KERNEL_FUSION.md)：

| ADR | 决策 | 状态 |
|---|---|---|
| ADR-013 | KernelToolRuntime 是 Runtime 调用 Kernel 的唯一入口（服务身份，不用 admin key） | Accepted |
| ADR-014 | 外部等待：`WAITING_EXTERNAL` + 持久事件唤醒，轮询只作兜底 | Accepted |
| ADR-015 | 产品任务执行态只有一个写入者：`app/services/task_lifecycle.py` | Accepted |
| ADR-016 | syscall 平面显式 Workspace 路由（`X-Workspace-Id`，向后兼容） | Accepted |
| ADR-017 | Kernel Runner CLI 降级为开发工具，Runtime 是唯一执行者 | Accepted |
| ADR-018 | 废弃 `veynod_platform` 与三个独立技术入口 | Accepted |
