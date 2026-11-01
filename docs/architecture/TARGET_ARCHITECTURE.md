# TARGET_ARCHITECTURE — Veynod Agent Coding Runtime 目标架构

> 文档性质：**目标设计**，不是当前实现，也不表示任何阶段已通过验收。
> 依据：`C:\Users\14564\Downloads\VEYNOD_FULL_REFACTOR_PLAN.md` 的 D-01～D-14，以及 2026-10-31 Phase 0 审计结果。
> 当前实现状态见 `CURRENT_STATE.md`；差距与迁移顺序见 `REFACTOR_ROADMAP.md`。

---

## 1. 战略定位

Veynod 是面向 AI Agent 的**高并发编码运行时平台（Agent Coding Runtime Platform）**：

- 人类通过 AgentGraph 创建项目、目标和编码任务；
- Agent 理解目标、规划、拆分、委派、执行、重规划和完成判断；
- Kernel 对工程任务做 DAG 调度、准入、租约、fencing 和并发控制；
- Worker 在隔离环境执行模型工具调用、代码修改和测试；
- AST/符号/契约/写集机制控制并行修改风险；
- Gate 和 Merge Pipeline 验证并安全交付；
- 任务、Run、Attempt、Submission、Commit 可追踪、可恢复、可审计、可计量；
- 通过多仓库、Execution Cell、Worker Pool 横向扩展。

第一阶段**只做透 Coding 场景**，不以通用性牺牲代码执行、并发修改、验证和交付的工程深度。

---

## 2. 核心架构决策

| 编号 | 决策 | 结论 |
|---|---|---|
| D-01 | 产品入口 | AgentGraph 是唯一用户产品入口 |
| D-02 | Kernel 定位 | Veynod Kernel 是 headless 代码执行与并发协调引擎 |
| D-03 | 领域边界 | Product Control / Agent Runtime / Kernel 保持明确 bounded context |
| D-04 | 数据边界 | 不把所有数据库表和 schema 强行合并 |
| D-05 | 任务权威 | 产品任务、Agent Run、Kernel DAG Task 分别由所属领域负责 |
| D-06 | Agent Loop | 必须持久化、事件驱动、可恢复、可取消、可审计 |
| D-07 | 调度分层 | Agent Orchestration 与 Kernel Execution Scheduling 分离 |
| D-08 | 执行模式 | API、Scheduler、Agent Runtime Worker、Gate Worker、Merge Worker 可独立扩容 |
| D-09 | 并发控制 | 以 Repository 为代码并发协调边界，支持 Snapshot、Write Set、Symbol/Contract 冲突检查 |
| D-10 | 分布式扩展 | Execution Cell + Repository Sharding，避免单实例绑定单仓库 |
| D-11 | 安全 | Agent 生成的代码视为不可信输入，生产执行必须进入受控沙箱 |
| D-12 | 迁移策略 | 渐进式迁移（Strangler），不做一次性推倒重写 |
| D-13 | 微服务策略 | 先明确模块边界，再按吞吐/故障隔离/资源特征拆服务 |
| D-14 | 容量目标 | 分阶段验证 100 / 500 / 1,000 / 5,000 / 10,000 级 Agent；所有数字必须真实压测 |

**一条原则：一个产品入口、多个领域边界、一个清晰的任务身份体系、多个独立的状态权威、一套可恢复的 Agent Loop、一套可靠的代码执行与交付链路。**

---

## 3. 六个核心平面

```text
┌──────────────────────────────────────────────────────┐
│ 1. PRODUCT CONTROL PLANE                             │
│ Identity / Tenant / Workspace / Project / RBAC       │
│ Product Tasks / Approval / Audit / Billing / UI      │
├──────────────────────────────────────────────────────┤
│ 2. AGENT ORCHESTRATION PLANE                         │
│ Intent / Planning / Delegation / Agent Lifecycle     │
│ Context / Model Routing / Tool Policy / Budget       │
├──────────────────────────────────────────────────────┤
│ 3. KERNEL SCHEDULING PLANE                           │
│ DAG / Admission / Lease / Fencing / Fairness        │
│ Dependency / Repository Scheduling / Write Set      │
├──────────────────────────────────────────────────────┤
│ 4. EXECUTION PLANE                                   │
│ Agent Worker / Runner / Tool Executor / Sandbox     │
│ Git / Worktree / Resource Manager                    │
├──────────────────────────────────────────────────────┤
│ 5. VERIFICATION & DELIVERY PLANE                     │
│ AST / Static Analysis / Tests / Policy / Gate       │
│ Submission / Merge / Commit / Delivery Evidence      │
├──────────────────────────────────────────────────────┤
│ 6. INFRASTRUCTURE & OBSERVABILITY PLANE              │
│ PostgreSQL / Outbox / Object Storage / Event System │
│ Metrics / Logs / Traces / Deployment / Secrets       │
└──────────────────────────────────────────────────────┘
```

六个平面是**逻辑边界**，第一阶段不必部署成六个服务；但代码、数据和接口边界必须清楚。

---

## 4. 三个 Bounded Context

### 4.1 Product Control — AgentGraph

拥有产品与业务事实：

- User、Organization/Tenant、Workspace、Membership、RBAC；
- Project、Coding Session、Product Task、Approval、Human Review；
- Product Audit、Billing/Budget、Webhook；
- Dashboard、Public API、SSE、Inbox。

AgentGraph 是**唯一用户身份入口和产品控制入口**。

### 4.2 Agent Runtime — 目标新增领域

拥有 Agent 执行生命周期：

- Agent Definition、Agent Run、Agent Step、Agent State；
- Checkpoint、Context Reference；
- Model Invocation、Tool Invocation；
- Delegation、Loop Decision、Retry、Cancellation；
- Runtime Budget、Recovery、Run Event。

Phase 0 结论：当前**不存在**这个领域。AgentGraph 只有任务放置和外部 runner；Kernel 只有 DAG 任务。Phase 2 必须新建，不能把 `collab_tasks` 或 `dag_tasks` 硬当 Agent Run。

### 4.3 Kernel — Veynod Kernel

拥有代码执行事实：

- Repository、Kernel Workspace、Code Artifact、Artifact Snapshot；
- AST / Symbol Graph；
- DAG Task、Task Dependency、Scheduler Authority；
- Admission Permit、Lease / Fencing、Write Set / Lock；
- Patch Submission、Gate Result、Merge Intent、Git Commit；
- Kernel Event、Execution Cell。

Kernel 不拥有产品登录、项目/成员/审批，不做产品 UI。

---

## 5. 统一任务对象层级

```text
Tenant
└── Workspace
    └── Project
        └── Coding Session
            └── Product Task
                └── Agent Run
                    └── Workflow
                        └── DAG Tasks
                            └── Execution Attempts
                                ├── Model Invocations
                                ├── Tool Invocations
                                ├── Patch Submissions
                                └── Verification Runs
```

| 对象 | 含义 | 所属领域 | 当前是否存在 |
|---|---|---|---|
| Product Task | 用户提出的业务目标 | AgentGraph | 有 `collab_tasks` |
| Coding Session | 一次持续编码协作会话 | 待定（Phase 0 定权） | **无** |
| Agent Run | 一次 Agent 执行生命周期 | Agent Runtime | **无** |
| Workflow | 一个执行计划及其版本 | Agent Runtime | **无** |
| DAG Task | 一个可调度的工程任务 | Kernel | 有 `dag_tasks` |
| Execution Attempt | 某个 DAG Task 的具体尝试 | Kernel | 部分（`retry_count`，无独立 attempt 表） |
| Model Invocation | 一次模型请求 | Agent Runtime | **无平台持久化** |
| Tool Invocation | 一次工具调用 | Agent Runtime / Tool Runtime | 仅 MCP 24h 幂等记录 |
| Patch Submission | 一次代码补丁提交 | Kernel | 有 `patch_submissions` |
| Verification Run | 一次代码验证执行 | Kernel | 记录在 submission/merge intent 的 JSONB |
| Delivery | 面向用户的最终交付 | AgentGraph 投影 | **无独立 Delivery 实体** |

---

## 6. 状态权威矩阵

| 状态/事实 | 最终权威 | 其他系统如何获得 |
|---|---|---|
| 用户任务业务状态 | AgentGraph | 产品 API / 事件 |
| 用户审批状态 | AgentGraph | 审批事件 |
| Agent Run 状态 | Agent Runtime | Runtime Event / Projection |
| Agent 当前 Step | Agent Runtime | Runtime Event / Checkpoint |
| Workflow 版本 | Agent Runtime | Workflow API / Event |
| DAG Task 状态 | Kernel | Kernel Event / API |
| Lease / Fencing | Kernel | Kernel API / Event |
| Patch Submission 状态 | Kernel | Kernel API / Event |
| Gate 结果 | Kernel | Kernel Event / Evidence Reference |
| Git Commit / Merge 结果 | Kernel | Kernel Event / API |
| 最终产品交付状态 | AgentGraph | 基于权威结果投影，等待必要审批 |

**严禁多个系统同时拥有同一状态的写入权。** 当前审计发现 AgentGraph 任务状态有 9+ 写入方、Kernel 任务状态有 6+ 写入方，未达到该原则。

---

## 7. 数据库与迁移策略

### 7.1 逻辑结构

```text
PostgreSQL Cluster
├── agentgraph schema / database
│   ├── identity
│   ├── workspace
│   ├── product_tasks
│   ├── approvals
│   ├── audit
│   └── billing
│
├── agent_runtime schema / database（Phase 2 新建）
│   ├── agent_runs
│   ├── agent_steps
│   ├── checkpoints
│   ├── invocations
│   └── runtime_events
│
└── kernel schema / database
    ├── repositories（目标；当前 workspace.repo_path）
    ├── dag_tasks
    ├── dependencies
    ├── leases
    ├── admission
    ├── submissions
    ├── gates
    ├── merges
    └── kernel_events
```

物理上可共用 PostgreSQL 集群，但逻辑 schema、迁移、权限和数据所有权必须隔离。第一阶段继续保留 AgentGraph/Kernel 独立数据库；**不做跨库 FK，不合并 48+15 张表**。

### 7.2 迁移规则

- 每个 bounded context 独立迁移；
- 迁移有版本和回滚策略；
- 先加兼容字段，再迁读写路径；
- 禁止无计划删旧字段；
- 迁移后做数据一致性检查；
- 高风险迁移分批/在线；
- 生产迁移必须备份和恢复演练。

当前缺陷：`v4_006`/`v4_007`/`v4_008` 不写 `schema_migrations`，版本表不能作为完整升级依据；Phase 1/4 必须修复并补一个 ledger 校验。

---

## 8. Durable Agent Runtime（目标）

### 8.1 禁止的生产循环

```python
while not task.completed:
    response = await model.generate(context)
    result = await execute_tools(response)
    context.append(result)
```

该循环无法独立解决进程重启、重复工具副作用、长时间执行、状态恢复、取消传播和跨节点迁移。

### 8.2 目标模块

```text
agent_runtime/
├── definitions/
├── run_manager/
├── loop_controller/
├── planning/
├── workflow/
├── context/
├── model_gateway/
├── tool_runtime/
├── delegation/
├── checkpoint/
├── budget/
├── recovery/
├── policy/
├── persistence/
├── events/
└── observability/
```

### 8.3 Agent Run 状态机

```text
CREATED -> QUEUED -> INITIALIZING -> PLANNING -> READY -> RUNNING
RUNNING  ├── WAITING_MODEL
         ├── WAITING_TOOL
         ├── WAITING_CHILDREN
         ├── BLOCKED
         ├── CHECKPOINTING
         └── REPLANNING
RUNNING  -> PAUSING -> PAUSED -> RESUMING -> RUNNING
RUNNING  -> VERIFYING -> COMPLETED
RUNNING  -> FAILED | CANCELLED | TIMED_OUT | BUDGET_EXCEEDED
```

所有状态变化必须通过受控命令，带 version、reason、actor、event_id、idempotency_key、必要证据。

### 8.4 Checkpoint 边界

计划创建完成、Workflow 版本确认、模型决策持久化、工具调用前、工具结果持久化后、子 Agent 委派/返回、代码提交受理后、Gate 结果返回后、最终交付前。

大型上下文存对象存储；数据库只存引用、版本和必要元数据。

### 8.5 模型/工具执行协议

```text
Model Response
  -> Structured Action Proposal
  -> Schema Validation
  -> Policy/Permission Check
  -> Budget/Deadline Check
  -> Idempotency Check
  -> Tool Execution
  -> Persist Result
  -> Observation
  -> Next Loop Decision
```

必须支持结构化输出校验、工具参数校验、allowlist、租户权限、超时、幂等键、副作用对账、结果持久化、错误分类、有上限重试、人工确认、预算检查。**不能把“模型请求成功”当“Agent Step 成功”。**

---

## 9. Agent Orchestration 与 Kernel Scheduling 分离

### 9.1 Orchestration

目标理解、计划生成/修订、任务拆分、Agent 选择、子 Agent 委派、结果汇总、失败判断、重规划、人工确认、完成判断。

### 9.2 Kernel Scheduling

DAG 可执行性、依赖检查、Admission、Lease、Fencing、Worker 选择、Repository 分区、写集冲突、资源配额、优先级、公平调度、Backpressure、执行重试/超时回收。

### 9.3 交互协议

```text
Orchestrator
  -> Workflow Adapter
  -> Kernel API (create DAG tasks/dependencies)
  -> Kernel Scheduler
  -> Kernel Events (leased/blocked/finished)
  -> Agent Runtime
  -> Orchestrator decision
```

Orchestrator 不直接操作 Kernel 数据库；Kernel 不决定用户目标和模型推理。

---

## 10. 代码并发与一致性模型

### 10.1 Snapshot Binding

每个代码任务开始时绑定：

```text
repository_id
base_commit_sha
artifact_snapshot_id
ast_version
dependency_graph_version
read_set
write_set
contract_set
```

### 10.2 四层冲突控制

| 层级 | 控制对象 | 用途 |
|---|---|---|
| L1 | File | 文件级粗粒度保护 |
| L2 | Symbol | 函数/类/方法级写入 |
| L3 | Contract | API/类型/接口/Schema 契约 |
| L4 | Dependency | 跨文件依赖和语义影响 |

不同层级不必全部排他锁；只读/无关符号/不同 Repository 应并行。

### 10.3 Read/Write Set

任务创建时预测读写集；执行时声明初始集合；Kernel 验证冲突；执行中发现新依赖时申请扩展；Kernel 检查；必要时暂停/释放/重排队；提交前重新验证快照和写集。Agent 不得绕过 Kernel 扩大写入范围。

当前 Kernel 有符号/文件锁和 SPLIT 守恒，但**没有通用动态写集扩展 API**；INJECT 可创建任意新锁，需在 Phase 5 收紧。

### 10.4 提交前检查

1. Base Commit 仍有效；
2. Artifact Snapshot 匹配；
3. Read Set 无不可接受版本变化；
4. Write Set 无冲突；
5. Symbol Lock 仍有效；
6. Contract 无不兼容变化；
7. Dependency Impact 是否扩大测试；
8. Gate 通过；
9. Merge 有有效 Fencing；
10. Git Ref 更新成功。

当前 Kernel 在 Gate 层检查 1/2/4/5/6，在 Merge 层检查 CAS/依赖变化；**没有统一 Snapshot 协议，也没有在 merge 临界区复核 task lease**。

### 10.5 Repository 线性化

- 同一 Repository 的正式引用更新需要明确线性化顺序；
- 不同 Repository 可独立并行 merge；
- 不存在跨所有 Repository 的全局 merge 锁；
- 大型 Repository 可进一步分区，但最终 Git 引用必须有 CAS/Fencing；
- “文件不同”不等于“没有语义冲突”。

当前以 workspace 为并发边界，多个 workspace 可指向同一 repo_path，存在跨 workspace 绕过锁的风险。

---

## 11. 多租户、多仓库与 Execution Cell

### 11.1 资源层级

```text
Global Control Plane
  └── Tenant
        └── Workspace
              └── Project
                    └── Repository
                          └── Execution Cell
                                ├── Scheduler Partition
                                ├── Worker Pool
                                ├── Gate Pool
                                └── Merge Workers
```

| 对象 | 职责 |
|---|---|
| Tenant | 隔离、计量、企业配额 |
| Workspace | 团队成员、权限、协作边界 |
| Project | 软件工程项目和业务目标 |
| Repository | 代码执行、Snapshot、锁、Git 协调边界 |
| Execution Cell | 执行资源和故障隔离边界 |
| Scheduler Partition | Repository/分片的调度协调 |
| Worker Pool | 可共享但受授权的计算资源池 |

### 11.2 Cell 原则

Cell ID、健康、容量、Repository Placement、API/Scheduler Endpoint、Worker/Gate/Merge Pool、版本兼容、心跳/租约、故障隔离/迁移。Cell Manager 不能成为所有任务执行的单点瓶颈。

### 11.3 公平调度

Admission 不能只看全局队列长度；需考虑 Tenant/Workspace/Repository 配额、Worker CPU/Memory、Gate/Merge 并发、模型速率、Token/Cost、优先级、等待时间、队列老化、长短任务公平。建议评估 Weighted Fair Queuing + Token Bucket + 优先级队列组合。

当前 Kernel 只有 per-workspace gate/merge 队列深度准入，没有租户配额/公平调度。

---

## 12. 安全沙箱（目标基线）

生产执行至少需要：

- 非 Root 用户；
- 独立文件系统/工作目录；
- CPU/Memory/PID/Disk 限制；
- 执行超时；
- 默认网络隔离；
- 明确出站 allowlist；
- 不挂载宿主机 Docker Socket；
- 不暴露敏感目录；
- 临时凭证、最小权限；
- 密钥不写日志；
- 进程/子进程限制；
- Artifact 扫描；
- 沙箱生命周期管理；
- 执行审计；
- 租户间隔离测试。

对更高风险多租户，评估 gVisor/Kata Containers，按兼容性、性能、威胁模型选型。

**当前 `local` 模式不满足以上任何一条隔离要求，只能用于单机开发。**

---

## 13. 验证与交付（目标）

### 13.1 Verification Pipeline

按成本和风险从低到高：

1. Patch 格式/路径校验；
2. 基础安全检查；
3. AST/Syntax；
4. 类型检查；
5. Lint/Static Analysis；
6. 受影响测试；
7. 完整测试；
8. Contract Verification；
9. 依赖影响验证；
10. Policy/Security Gate；
11. Merge Precondition；
12. 最终 Git Commit。

具体阶段按语言/仓库规模/任务风险配置，不强制所有项目跑同一昂贵集合。

### 13.2 Gate Worker 要求

持久化队列、`FOR UPDATE SKIP LOCKED`、Lease、Heartbeat、Fencing、超时、重试、幂等、资源限制、结果证据、失败分类、任务恢复、运行指标。

### 13.3 Merge Coordinator 要求

每个 Repository 最终引用受控；CAS/版本检查；旧 Worker 不能覆盖新提交；Merge Intent 可恢复；失败有明确状态；不同 Repository 并行；同一 Repository 有确定顺序；Commit/Base/Tree/Gate Evidence 可追溯；未过必需 Gate 不得进入保护分支。

**目标增加：merge 临界区必须复核 task status + lease token + submission status，不能只依赖 workspace coordinator lease。**

---

## 14. 模型与工具目标

### 14.1 Model Gateway

统一 Provider Adapter、模型能力注册、选择、任务路由、超时、限流、重试、失败切换、Token 计量、费用核算、请求关联、Prompt/Response 安全策略、模型版本记录。禁止各模块硬编码 Provider。

### 14.2 模型路由输入

任务类型、复杂度、代码规模、上下文长度、推理要求、延迟预算、成本预算、Provider 可用性、数据隐私等级、用户模型约束。路由决策可审计，记录实际模型/版本。

### 14.3 Tool Runtime

统一注册协议：

```text
tool_id, tool_version, input_schema, output_schema,
required_permissions, risk_level, execution_mode,
timeout, idempotency_policy, network_policy,
resource_profile, audit_policy
```

工具执行必须权限/预算检查；高风险工具可要求人工审批。

### 14.4 Context Engine

分层管理 System Policy、Project/Repository/Task Context、Agent Memory、Recent Execution History、Tool Results、Code Snapshot、Relevant Symbol Context、User Decisions、Checkpoint References。支持 Token Budget、优先级、摘要、引用和版本。代码事实绑定具体 Snapshot。

---

## 15. API / CLI / Dashboard 目标

### 15.1 产品 API（AgentGraph）

Authentication、Organization、Workspace、Project、Product Task、Coding Session、Agent Run、Approval、Audit、Usage、Delivery。

### 15.2 Runtime API（Agent Runtime）

Create/Get/Pause/Resume/Cancel Run、Get Steps/Checkpoints/Workflow、Delegate、Get Usage。

### 15.3 Kernel API

Repository Registration、Artifact Registration、DAG Task Creation、Dependency Management、Claim/Heartbeat/Release、Patch Submission/Status、Gate Evidence、Merge Status、Repository Snapshot、Scheduler Health。

### 15.4 API 规范

版本策略、OpenAPI/Schema、请求 ID、Idempotency Key、统一错误码、超时、分页、Rate Limit、Retry-After、认证授权、Trace Context、向后兼容、Deprecation Policy。内部 Admin API 不暴露公网。

### 15.5 CLI

Login、Workspace/Project 选择、Repository 注册、Agent Run 启动/查询/事件/Pause/Resume/Cancel、本地 Runner、Sandbox Runner、日志诊断、配置校验、版本兼容检查。

### 15.6 Dashboard 五个核心区域

Projects、Agent Workspace、Execution Graph、Code Review、Runtime Observability。用户不必理解 Lease/Fencing/Admission；必须知道 Agent 在做什么、为什么等待、何时需要人；最终交付必须有验证证据。

---

## 16. 目标部署形态

第一阶段模块化服务，可共用部署单元，但保持代码/数据/接口边界：

- Control API；
- Agent Runtime API / Worker；
- Kernel API；
- Scheduler；
- Execution Worker Pool；
- Gate Worker Pool；
- Merge Worker；
- PostgreSQL；
- Object Storage；
- Outbox Dispatcher；
- Observability Stack。

后续按资源特征拆：API、Scheduler、Agent Worker、Gate Worker、Merge Worker 独立水平扩容。

---

## 17. 容量与 SLO 目标（规划，不是当前 SLA）

| 指标 | 阶段 A | 阶段 B | 阶段 C |
|---|---:|---:|---:|
| Connected Agents | 1,000 | 5,000 | 10,000 |
| Active Agent Runs | 500 | 2,000 | 5,000 |
| Concurrent Executions | 100 | 500 | 1,000 |
| Execution Cells | 2 | 5 | 10+ |
| Repository 数量 | 100 | 500 | 2,000+ |

Gate Worker 数量按 CPU/内存/测试时间/仓库规模/I/O 实测决定，不按 Agent 数机械配置。

Phase 0 SLO 原则：先测基线，再定义 Availability、请求延迟、调度延迟、事件投递滞后、Agent 恢复时间、任务完成率、Gate 等待、Merge 延迟、数据持久性、租户隔离。每个 SLO 明确窗口、分母、排除项、负载模型、错误预算。外部模型 Provider 延迟与内部调度延迟分开统计。

---

## 18. 目标与当前资产映射

| 目标能力 | 当前可复用资产 | 差距 |
|---|---|---|
| Product Control | AgentGraph identity/tenant/RBAC/task/audit/SSE | 无 Project/Repository/CodingSession；任务状态多写入方 |
| Agent Runtime | 无 | 全新领域；不能复用 in-process 循环 |
| Orchestration | AgentGraph scheduler/runner placement | 无计划/委派/重规划/模型网关 |
| Kernel Scheduling | Rust SchedulerCore + SQL fallback + admission/lease | 无 Repository Registry/Cell/公平调度；fencing 不完整 |
| Execution | Kernel Runner + worktree + gate sandbox | 无生产沙箱；无 Agent Runtime Worker |
| Verification | 四阶段 gate + AST/契约/测试 | 无 policy/security gate；受影响测试未实现；静态检查测试未启用 |
| Merge | Merge Coordinator + Git CAS + reconcile | merge 不复核 task lease；tree merge 未端到端验证 |
| Multi-repo/Cell | 多 workspace 表结构 | syscalls 不带 workspace；无 placement/shard |
| Events | 两套事件日志/outbox | 无统一 envelope/consumer/DLQ/correlation |
| Observability | 运行时 metrics/SSE 指标 | 无统一 trace；无跨域 correlation |

---

## 19. 目标架构的硬约束

1. 不删除已验证能力而没有等价替代和回归测试；
2. 不只凭文档推断功能已实现；
3. 不在资产盘点前大规模搬迁目录；
4. 不同时存在两套代码任务主执行路径；
5. 不跨 bounded context 直接写对方数据库表；
6. 不把模型输出视为可信执行指令；
7. 不把 Redis/MQ/内存状态作为唯一权威事实；
8. 不把连接数/任务创建数/受理数包装成完整交付能力；
9. 无压测报告不宣称容量达标；
10. 每个迁移阶段必须有回滚方案。

---

## 20. 一句话

**AgentGraph 提供产品与组织能力；Agent Runtime 提供持续工作的 Agent Loop；Veynod Kernel 提供高并发、可验证的工程执行能力。三者是一个产品，但保持清晰的数据和责任边界。**
