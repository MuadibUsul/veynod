# Veynod 下一代目标架构设计与全量重构实施规范

**文档版本：** 1.0\
**基准日期：** 2026-11-02\
**目标仓库：** `MuadibUsul/veynod`\
**目标产品：** Veynod --- Graph-Driven Distributed Agent Workspace\
**用途：** 作为 Codex 重构的唯一目标架构基准、实施约束与验收规范

------------------------------------------------------------------------

## 0. 给 Codex 的执行声明

本文件是目标架构与重构验收规范，不是要求一次性推倒重写的授权。

开始修改前，必须先检查当前仓库
HEAD、分支、未提交改动、目录结构、数据库迁移、现有测试和实际部署版本。本文引用的现状来自
2026-11-02 对仓库的只读审查，审查时观察到的 `main` 分支提交为
`88626e3`。如果当前 HEAD
已变化，以代码和可复现结果为准，并在实施报告中列出差异。

### 必须遵守

1.  **先审计，后修改。**
    不得仅凭本文的现状描述直接重构；先验证每一项现状和问题。
2.  **不推倒重来。** 保留已验证有效的 AgentGraph、Agent Runtime、Veynod
    Kernel、Runner、Diagram、Contracts
    和现有安全机制；只在证明无法演进时才替换。
3.  **不制造第二套任务系统。** 产品任务、Diagram Chunk、Runtime
    Run、Kernel Task、Runner Placement
    是不同层级的对象，必须有明确映射和唯一所有权，不能各自成为可独立争抢的同一份工作。
4.  **不制造第二条代码交付链。** 所有代码变更必须经过统一的 Kernel
    Worktree、Lease、Submission、Gate、Merge 和 Evidence
    路径。旧路径只能作为受控迁移适配器，不能与新路径同时拥有同一任务。
5.  **不把中央 Worker 变成代码执行瓶颈。**
    中央服务负责规划、调度、状态协调和事件路由；模型推理与代码执行应能分布在不同
    Runner/设备上。
6.  **不以增加 Agent 数量作为并发设计。** 并发必须受 DAG
    依赖、写集冲突、资源、模型额度、Runner 容量和合并能力共同约束。
7.  **不以文档或 Mock 代替验收。** 必须提供真实多 Runner、真实
    Worktree、真实 Kernel Gate、真实合并和故障恢复的端到端证据。
8.  **不静默改变冻结设计。**
    修改冻结图必须通过变更提案、影响分析和批准流程。
9.  **不把未通过的测试写成通过。**
    所有报告必须区分静态检查、单元测试、集成测试、E2E、压力测试和生产部署验证。
10. **每个阶段独立提交、可回滚。** 不允许一个巨型 PR
    同时改动任务模型、数据库、执行器、调度器和 UI 而无法定位回归。

### Codex 第一阶段交付物

在任何功能代码修改前，先提交一份
`docs/refactor/00_CURRENT_STATE_VERIFICATION.md`，至少回答：

-   当前 HEAD 与本文基准提交之间有哪些变化？
-   Diagram Chunk、Product Task、Runtime Run、Kernel Task、Runner
    Placement 的真实数据关系是什么？
-   当前每一种执行入口分别会启动什么执行器？
-   哪些路径会创建 Worktree、申请 Lease、提交 Patch、执行 Gate 和
    Merge？
-   Diagram 的 `allowed_write_paths` 是否实际传递到 Kernel 的有效写集？
-   Kernel Merge 成功后，Diagram Chunk 是否被可靠更新并触发下游解锁？
-   旧 Diagram Gate/Merge 与 Kernel Gate/Merge 是否可能同时处理同一
    Chunk？
-   当前 Scheduler 是否可能把已由 Runtime 执行的任务再次分配给 Host
    Runner？
-   哪些测试覆盖上述路径？哪些没有？
-   生产部署运行的是否为当前 HEAD？证据是什么？

如果某个问题无法从代码、测试或运行记录证明，标记为 **UNKNOWN / NOT
VERIFIED**，不得猜测。

------------------------------------------------------------------------

# 第一部分：产品定义与设计原则

## 1. 产品定义

Veynod 是一个由人类定义目标、由架构 Agent 生成工程图、由分布式 Agent
并发实现、由确定性执行内核验证交付的 Agent Workspace。

它不是：

-   一个把多个聊天机器人放在同一页面的聊天产品；
-   一个只有中心 Worker 的单线程 AI 编程工具；
-   一个只会把需求拆成待办事项的任务管理器；
-   一个允许多个 Agent 直接修改共享工作目录的脚本集合；
-   一个让模型自行宣布完成、没有独立证据的代码生成器。

它应该形成一条持续运行的工程生产线：

**人类定义目标与产品边界 → 架构 Agent 设计可执行工程图 →
人类确认关键设计 → 调度器并发分派 → 分布式 Agent 执行 → Kernel
验证和合并 → 图状态推进 → 失败自动修复或重新规划 → 直到目标满足。**

人类的主要注意力应集中在：

1.  产品目标与需求；
2.  关键架构决策；
3.  影响范围较大的变更审批；
4.  预算与风险边界；
5.  最终产品验收。

日常领取任务、复制命令、搬运上下文、检查普通测试、手动解锁下游任务，不应成为人类的常规工作。

## 2. 核心设计原则

### 2.1 Graph is the Plan Source of Truth

工程图是项目实施计划的唯一权威来源。

-   每个可执行 Chunk 都必须存在于已确认的 Diagram 版本中。
-   任务依赖、接口契约、写入范围和验收标准以冻结图为准。
-   Scheduler 不得自行创造未在图中定义的开发工作。
-   Agent 不得以自然语言消息覆盖冻结图。
-   发现遗漏或错误时，必须创建 Change Proposal，分析影响后更新图版本。

### 2.2 Kernel is the Code Delivery Authority

Kernel 是代码变更的唯一交付权威。

-   Worktree 与 Lease 由 Kernel 管理或通过 Kernel 授权的适配器管理。
-   Patch/Submission 必须绑定任务、尝试、租约和写集。
-   Gate 必须产生可追溯证据。
-   Git 合并必须由统一 Merge Coordinator 完成。
-   只有 Kernel 的成功合并事件可以把代码交付标记为已合并。
-   Agent、Runner、Runtime 和 Diagram 不得自行把任务标记为代码已交付。

### 2.3 Runtime is the Agent Reasoning Authority

Agent Runtime 负责一个逻辑 Agent Run
的持续推理、工具调用、上下文、Checkpoint、预算、暂停、恢复和终止。

它不拥有 Git 合并权，不拥有 Kernel Gate
的最终裁决权，也不应把代码执行锁死在单个中心 Worker 进程里。

### 2.4 Scheduler is the Placement Authority

Scheduler 负责决定哪个 Agent/Runner 在什么时间执行哪个已就绪任务。

它不重新解释产品需求，不修改工程图，不绕过 Kernel 的租约和写集约束。

### 2.5 Distributed Execution, Centralized Coordination

控制面可以集中，执行面必须可分布。

中央服务允许承担：

-   项目和图管理；
-   任务就绪判定；
-   Runner 选择；
-   预算与策略；
-   事件路由；
-   状态聚合；
-   异常升级。

真正的模型会话和代码执行可以运行在：

-   用户本机；
-   VPS；
-   云主机；
-   多台不同架构的计算机；
-   未来的隔离 Execution Cell。

中央服务不得成为所有代码读写、测试执行和模型推理的单线程执行点。

### 2.6 Smallest Independently Verifiable Unit

最小任务单元不是最小代码行，也不是最小函数，而是：

**能够独立理解、拥有明确写入范围、具备可验证交付物、可以安全合并，并且不会把大量隐式上下文留给其他
Agent 的最小工程单元。**

过大的 Chunk 会降低并行度；过小的 Chunk
会增加调度、上下文、合并和验证成本。架构 Agent
必须在独立性与协调成本之间做权衡。

------------------------------------------------------------------------

# 第二部分：目标架构

## 3. 目标系统总览

``` mermaid
flowchart TB
    H[Human / Product Owner]
    UI[AgentGraph Workspace]
    PA[Architecture Planner Agent]
    DG[Diagram: Versioned Engineering Graph]
    SC[Scheduler / Placement Coordinator]
    EV[Event Inbox + Outbox]
    subgraph EXEC[Distributed Execution Plane]
        R1[Runner Host A]
        R2[Runner Host B]
        R3[Runner Host C]
        RN[Runner Host N]
    end
    subgraph AGENTS[Agent Execution Adapters]
        A1[Runtime-native Agent]
        A2[Claude Code Adapter]
        A3[Codex Adapter]
        A4[Other Agent Adapter]
    end
    K[Veynod Kernel]
    G[Verification Gate]
    M[Merge Coordinator]
    DB[(Persistent Stores)]
    OBS[Evidence / Trace / Metrics]

    H --> UI
    UI --> PA
    PA --> DG
    DG --> SC
    SC <--> EV
    SC --> R1
    SC --> R2
    SC --> R3
    SC --> RN
    R1 --> A1
    R2 --> A2
    R3 --> A3
    RN --> A4
    A1 --> K
    A2 --> K
    A3 --> K
    A4 --> K
    K --> G
    G --> M
    M --> EV
    EV --> DG
    EV --> UI
    K --> DB
    DG --> DB
    UI --> OBS
    K --> OBS
    SC --> OBS
```

### 3.1 分层职责

  -----------------------------------------------------------------------------------------------
  层                      核心组件                唯一职责
  ----------------------- ----------------------- -----------------------------------------------
  人类交互层              AgentGraph UI           需求、产品决策、审批、项目观察

  规划层                  Architecture Planner    理解需求、仓库、约束，生成工程图

  计划层                  Diagram                 保存版本化工程图、依赖、契约、验收标准

  协调层                  Scheduler               就绪判断、资源匹配、分布式派发

  Agent 层                Agent Runtime +         模型推理、工具调用、上下文、恢复
                          Execution Adapters      

  执行内核层              Veynod Kernel           Lease、Worktree、Patch、Gate、Merge、Evidence

  事件层                  Outbox / Inbox / Event  可靠事件传播、幂等消费、恢复唤醒
                          Router                  

  观测层                  Trace / Audit / Metrics 解释系统正在做什么、为什么等待、如何失败
  -----------------------------------------------------------------------------------------------

## 4. 组件权责与禁止事项

### 4.1 AgentGraph --- 人类工作空间与产品控制面

**负责：**

-   用户、Workspace、Project、权限和策略；
-   产品需求、目标、优先级、预算；
-   Diagram 的查看、确认、冻结、变更审批；
-   任务总览、执行进度、异常与最终交付；
-   展示 Runtime、Runner、Kernel 的聚合状态；
-   人类审批和审计记录。

**不负责：**

-   直接执行代码修改；
-   直接分配 Kernel Lease；
-   自行判定 Patch 已通过 Gate；
-   自行执行 Git Merge；
-   维护第二套与 Diagram 不一致的 DAG。

AgentGraph 中的 Product Task
可以保留为用户可见对象，但它必须是一个有明确关联关系的工作对象/投影，不能与
Diagram Chunk、Kernel Task 分别形成三个互相争抢的任务调度系统。

### 4.2 Architecture Planner Agent --- 架构规划角色

这是目标架构中的关键能力，应作为明确的 Agent Role 实现，而不是假设现有
Diagram 工具已经自动具备专业架构能力。

**输入：**

-   人类需求与验收目标；
-   当前仓库结构、代码索引和已有架构；
-   已存在的接口、数据模型、技术约束；
-   质量、安全、性能、预算和部署要求；
-   用户已确认的产品决策。

**输出：**

-   目标架构说明；
-   模块与子系统关系；
-   版本化工程图；
-   最小可独立执行的 Chunk；
-   Chunk 之间的依赖 DAG；
-   Interface Contract；
-   Write Scope；
-   自动验收规则；
-   风险、未知项和需要人类决策的问题。

**规划过程必须分阶段：**

1.  需求结构化：目标、非目标、用户场景、验收条件。
2.  仓库理解：识别现有模块、依赖、入口、数据所有权和测试。
3.  架构设计：确定边界、模块、接口、数据流和部署拓扑。
4.  工作分解：拆成最小独立交付单元。
5.  依赖分析：构建 DAG，识别关键路径和阻塞。
6.  写集分析：识别文件、符号、数据库、API、配置的冲突。
7.  验收设计：为每个 Chunk 定义可执行的完成条件。
8.  计划审查：检查循环依赖、孤儿任务、重复职责、隐式共享状态和不可测试目标。
9.  风险评估：标记高风险、未知和必须由人类决定的事项。
10. 生成可确认的图版本。

规划 Agent 不得仅输出一张可视化图。它必须生成机器可验证的数据结构。

**规划质量门禁：**

-   DAG 无环；
-   每个可执行 Chunk 都有唯一 ID；
-   每个 Chunk 都有明确目标和交付物；
-   每个 Chunk 都有 Write Scope；
-   每个 Chunk 都有验收条件；
-   跨 Chunk 接口有明确契约；
-   不允许两个并发 Chunk 无声明地修改同一共享文件；
-   所有依赖都能解释；
-   所有关键需求都有对应 Chunk 或明确标记为非本次范围；
-   无法确认的仓库事实必须标记 UNKNOWN，不能虚构。

高风险架构可以触发独立 Architecture Critic
Agent；普通变更不应无条件增加一轮昂贵模型调用。

### 4.3 Diagram --- 唯一工程计划

Diagram 保存：

-   Project Goal；
-   Architecture Baseline；
-   Diagram Version；
-   Subsystem / Module / Chunk；
-   Dependency DAG；
-   Interface Contract；
-   Write Scope；
-   Acceptance Criteria；
-   Test Plan；
-   Risk；
-   Change Proposal；
-   Approval；
-   Execution Progress Projection。

Diagram 的版本必须可追溯。冻结后的版本不可原地覆盖。

### 4.4 Scheduler --- 分布式调度器

Scheduler 负责：

-   计算 Chunk 是否满足依赖；
-   根据优先级、关键路径和资源决定派发顺序；
-   匹配 Runner 的仓库访问、操作系统、架构、运行时、能力和额度；
-   限制单主机并发；
-   避免同一工作被重复领取；
-   对离线、超时、失败和额度耗尽进行重新派发；
-   对 Review / Supervise 等特殊步骤匹配相应能力；
-   记录每次 Placement 的原因和结果。

Scheduler 不得：

-   绕过 Diagram 的依赖；
-   绕过 Kernel Lease；
-   自己判断代码合并成功；
-   为同一个 Chunk 同时启动两个有效执行者；
-   在没有变更批准的情况下修改冻结图。

### 4.5 Agent Runtime --- 持久化 Agent 会话

Runtime 负责：

-   一个逻辑 Run 的状态；
-   模型调用、上下文和工具调用；
-   每一步的持久化 Invocation；
-   Checkpoint 和恢复；
-   外部等待与事件唤醒；
-   Token / Cost / Deadline；
-   失败分类、重试和停止；
-   把 Kernel 结果和证据交回 Agent 继续推理。

Runtime 必须支持多个 Run 并发，而不是让一个 Run 的长时间等待阻塞其他
Run。

外部等待必须采用持久化状态，例如 `WAITING_EXTERNAL`，不能用占用 Worker
的 sleep/poll 循环代替。

### 4.6 Runner --- 分布式执行主机

每台设备是一个独立 Runner Host。

Runner 上报：

-   Host ID；
-   OS / Architecture；
-   Runner Version；
-   可用 Agent Runtime；
-   可用模型和额度；
-   最大并发槽位；
-   仓库访问能力；
-   在线状态；
-   最近心跳；
-   当前工作负载。

Runner 的职责是接收 Placement、启动指定 Execution
Adapter、提供运行环境、回传执行状态和诊断信息。

Runner 不拥有计划，不拥有最终合并权，不得绕过 Kernel
直接把修改写入主分支。

### 4.7 Execution Adapter --- 统一 Agent 执行协议

不同模型和编码工具必须通过统一 Adapter Protocol 接入：

-   Runtime-native API Agent；
-   Claude Code；
-   Codex；
-   其他本地或远程 Agent。

每个 Attempt 只能选择一个执行 Adapter。不得让 Runtime-native Agent 和
Host Runner 同时操作同一个 Chunk/Lease。

统一 Adapter 至少提供：

``` text
start(run_context, task_contract, lease_context)
stream(event)
pause(reason)
resume(checkpoint)
cancel(reason)
collect_result()
health()
```

不同 Adapter 的差异封装在适配层，不能向 Scheduler
暴露各家工具的内部控制逻辑。

### 4.8 Veynod Kernel --- 唯一代码交付内核

Kernel 负责：

-   Workspace 和 Repository Binding；
-   Kernel Task / DAG 执行记录；
-   Lease 与 Fencing Token；
-   独立 Git Worktree；
-   Write Scope 校验；
-   Patch / Submission；
-   Gate；
-   Merge Queue / Merge Coordinator；
-   Commit；
-   Evidence；
-   代码执行隔离环境。

Kernel 不负责理解人类需求，不负责生成工程图，不负责决定产品优先级。

------------------------------------------------------------------------

# 第三部分：统一对象模型与数据所有权

## 5. 对象关系

目标关系如下：

``` mermaid
flowchart TD
    P[Project]
    D[Diagram Version]
    C1[Diagram Chunk A]
    C2[Diagram Chunk B]
    C3[Diagram Chunk C]
    T1[Product Task A]
    T2[Product Task B]
    T3[Product Task C]
    K1[Kernel Task A]
    K2[Kernel Task B]
    K3[Kernel Task C]
    R1[Runtime Run / Attempt]
    L1[Kernel Lease + Worktree]
    S1[Submission]
    G1[Gate Evidence]
    M1[Git Commit]
    PL[Runner Placement]

    P --> D
    D --> C1
    D --> C2
    D --> C3
    C1 --> T1
    C2 --> T2
    C3 --> T3
    T1 --> K1
    T2 --> K2
    T3 --> K3
    T1 --> PL
    PL --> R1
    R1 --> L1
    L1 --> S1
    S1 --> G1
    G1 --> M1
```

### 5.1 对象定义

  -----------------------------------------------------------------------------
  对象                    含义                          权威所有者
  ----------------------- ----------------------------- -----------------------
  Project                 人类定义的产品/工程目标       AgentGraph

  Diagram                 版本化工程计划                Diagram Service

  Chunk                   图中最小独立交付单元          Diagram Service

  Product Task            面向用户的任务视图/关联对象   AgentGraph

  Placement               某次执行分配到某台 Runner     Scheduler
                          的记录                        

  Runtime Run             一次持久化 Agent 推理会话     Agent Runtime

  Attempt                 某个 Chunk 的一次具体执行尝试 Runtime / Execution
                                                        Coordinator

  Kernel Task             Kernel 内部执行与依赖记录     Kernel

  Lease                   有期限、有 Fencing 的执行授权 Kernel

  Worktree                Attempt 的隔离工作目录        Kernel

  Submission              Agent 提交的变更              Kernel

  Gate Evidence           自动验证结果                  Kernel Gate

  Commit                  被 Merge Coordinator 接受的   Kernel
                          Git 提交                      
  -----------------------------------------------------------------------------

### 5.2 必须建立的关联键

根据当前代码实际字段做映射，不要求盲目增加同名字段；但语义必须完整：

``` text
workspace_id
project_id
diagram_id
diagram_version
chunk_id
product_task_key
placement_id
host_id
runtime_run_id
attempt_id
kernel_workspace_id
kernel_task_id
lease_id
lease_epoch
submission_id
commit_sha
trace_id
correlation_id
causation_id
idempotency_key
```

所有执行事件必须能从 `chunk_id` 追溯到最终 Commit，也必须能从 Commit
反查对应 Diagram Version、Chunk、Attempt、Runner 和 Gate Evidence。

### 5.3 任务映射规则

1.  一个可执行 Diagram Chunk 对应一个逻辑 Product Task 关联对象。
2.  一个逻辑 Product Task 在同一时刻最多存在一个活动 Run。
3.  一个 Chunk 可以有多次历史 Attempt，但同一时刻最多一个有效 Attempt。
4.  一个 Attempt 只能拥有一个有效 Lease。
5.  一个 Kernel Task 必须明确绑定 Diagram Chunk / Product
    Task，不能被其他 Run 随意领取。
6.  一个 Submission 必须绑定创建它的 Lease Epoch 和 Write Scope Hash。
7.  一个 Commit 必须关联一个或多个经过验证的 Submission，并保留对应
    Evidence。
8.  Product Task 的 UI 状态是聚合视图，不得与 Kernel 的 Lease/Gate/Merge
    状态形成相互覆盖的双写状态机。

### 5.4 数据库边界

不要为了"统一"而把所有数据库物理合并。

保留 bounded contexts：

-   AgentGraph / Product 数据；
-   Agent Runtime 数据；
-   Kernel 执行数据。

可以使用同一个 PostgreSQL 集群，但不同领域的数据表和迁移仍应有清晰边界。

统一的是标识、事件协议和状态所有权，不是强行把所有表塞进同一个 Schema。

------------------------------------------------------------------------

# 第四部分：Diagram 数据结构与任务拆分规范

## 6. Chunk Contract

每个可执行 Chunk 必须具备以下语义字段：

``` yaml
chunk_id: stable-unique-id
diagram_id: diagram-id
diagram_version: 12
title: Implement workspace membership API
objective: >
  Implement the membership API according to the frozen interface contract.
layer: implementation
status: pending

inputs:
  - ref: interface-contract/workspace-membership-v1
  - ref: upstream-chunk-output

outputs:
  - type: api
    ref: workspace-membership-api
  - type: test_evidence
    ref: workspace-membership-tests

dependencies:
  - chunk_id: define-membership-contract
    required_state: merged

interface_contract:
  contract_id: workspace-membership-v1
  version: 1
  hash: immutable-contract-hash
  required_behavior:
    - behavior-1
    - behavior-2

write_scope:
  paths:
    - AgentGraph/app/api/membership/**
    - AgentGraph/tests/api/test_membership.py
  symbols: []
  database:
    migrations:
      - membership tables only
  protected:
    - AgentGraph/app/core/auth.py

acceptance:
  - id: membership-api-tests
    type: command
    command: pytest AgentGraph/tests/api/test_membership.py -q
    required_exit_code: 0
  - id: contract-validation
    type: contract
    contract_id: workspace-membership-v1

risk:
  level: medium
  reasons:
    - touches authorization boundary

required_capabilities:
  - python
  - repository-write
  - test-execution

resource:
  estimated_cost_class: medium
  timeout_seconds: 1800

retry_policy:
  max_attempts: 3
  repair_attempts: 2

human_approval:
  required: false
  triggers:
    - scope-expansion
    - contract-change
    - security-boundary-change
```

以上仅为目标 Schema 示例。Codex 必须先对照当前 Diagram
数据结构，制定兼容迁移方案，不能直接复制示例覆盖现有字段。

## 7. Write Scope 统一规范

这是本次重构的 P0 核心。

当前审查发现，Diagram 的 `allowed_write_paths` 与 Kernel
租约写集字段之间可能存在语义不一致。必须先通过代码追踪和测试确认，再统一。

### 7.1 统一的规范对象

定义 `WorkScope`，作为 Diagram → Product Task → Scheduler → Runtime →
Kernel → Runner 的唯一规范写入范围。

``` yaml
scope_version: 1
scope_hash: sha256
paths:
  include:
    - src/module_a/**
  exclude:
    - src/module_a/secrets/**
symbols:
  include: []
  exclude: []
resources:
  database_tables:
    - module_a_records
  api_contracts:
    - module-a-v1
protected_paths:
  - src/core/auth.py
```

### 7.2 规则

-   Diagram 是 Write Scope 的来源。
-   Kernel 在 Lease 签发时保存不可变 Scope Snapshot 和 Hash。
-   Runner/Agent 只能在该 Lease 对应 Worktree 中操作。
-   Submission 必须重新校验 Diff 是否落在授权范围。
-   路径匹配必须处理 `..`、符号链接、大小写、绝对路径和路径穿越。
-   排除范围优先于包含范围。
-   未声明的文件变更默认拒绝。
-   Scope 变更必须产生新 Attempt 或新的批准租约，不能原地扩大。
-   符号级限制是路径级限制的补充，不得以空的 Symbol Set
    意外解释为无限权限。
-   对数据库迁移、依赖锁文件、全局配置等高影响文件设置专门策略。

### 7.3 兼容映射

Codex 必须形成一个明确的兼容适配器：

``` text
Diagram.allowed_write_paths
            |
            v
       WorkScope
            |
            +----> Kernel path scope
            |
            +----> Kernel symbol scope
            |
            +----> Runner sandbox policy
            |
            +----> Submission diff validation
```

禁止各模块分别解析一遍原始字段，造成语义漂移。

------------------------------------------------------------------------

# 第五部分：并发执行与调度

## 8. 并发模型

### 8.1 并发的基本单位

并发单位是 Chunk / Attempt，不是模型调用数量，也不是 Worker 数量。

可并发条件：

-   所有上游依赖已满足；
-   Write Scope 无冲突，或冲突已通过明确依赖串行化；
-   Interface Contract 已冻结；
-   Runner 有合适的能力、额度和空闲槽位；
-   Kernel 能签发唯一有效 Lease；
-   当前 Chunk 没有其他活动 Attempt。

### 8.2 动态并发上限

有效并发数：

``` text
effective_concurrency =
min(
  ready_chunks,
  compatible_runner_slots,
  model_quota_capacity,
  kernel_execution_capacity,
  database_capacity,
  gate_capacity,
  merge_capacity,
  configured_workspace_limit
)
```

不得固定假设"500 个 Agent 一定比 50 个 Agent 快"。

### 8.3 调度优先级

建议综合考虑：

1.  关键路径优先级；
2.  下游解锁价值；
3.  用户设置的业务优先级；
4.  依赖等待时间；
5.  Runner 兼容度；
6.  模型额度和预计成本；
7.  当前负载；
8.  失败历史与重试成本。

调度算法必须可解释：UI 能说明为什么某个 Chunk
被派给某台设备，为什么另一个 Chunk 仍在等待。

### 8.4 冲突控制

-   不同文件、无共享资源的 Chunk 可以并发。
-   同一文件的写入默认串行，除非存在经过验证的分区规则。
-   同一数据库 Schema 的冲突迁移必须显式串行化。
-   Interface Contract 变更必须先于依赖它的实现。
-   共享配置、锁文件、全局注册表和公共入口需要显式协调。
-   Git Merge Queue 负责最终集成顺序。
-   不允许 Agent 直接向共享主工作目录写入。

### 8.5 避免过度拆分

Planner 必须评估：

-   任务是否能独立测试；
-   上下文是否足够自包含；
-   是否需要频繁修改同一文件；
-   是否会产生大量接口协调；
-   测试启动成本是否超过实现成本；
-   是否有足够的独立工作让多个 Agent 同时推进。

如果拆分后大部分任务都必须等待一个公共文件或公共接口，说明拆分方式不合理。

### 8.6 关键路径与负载均衡

Scheduler 应计算 DAG 的关键路径和可并发宽度。

优先执行：

-   能解锁多个下游节点的 Chunk；
-   关键路径上的高阻塞节点；
-   预计耗时较长且依赖已满足的 Chunk；
-   与当前 Runner 能力高度匹配的任务。

避免所有 Agent 同时争抢同一个上游接口任务。

------------------------------------------------------------------------

# 第六部分：统一执行生命周期

## 9. 目标执行闭环

``` mermaid
flowchart TD
    A[Human Requirement] --> B[Architecture Planner]
    B --> C[Repository Discovery]
    C --> D[Generate Engineering Graph]
    D --> E[Validate Graph]
    E --> F{Human Confirmation}
    F -->|Needs changes| B
    F -->|Approved| G[Freeze Diagram Version]
    G --> H[Find Ready Chunks]
    H --> I[Scheduler Placement]
    I --> J[Runner starts one Execution Adapter]
    J --> K[Runtime creates/continues Run]
    K --> L[Kernel grants Lease + Worktree]
    L --> M[Agent reads context and implements]
    M --> N[Submit Patch]
    N --> O[Kernel Verification Gate]
    O -->|Rejected with evidence| P[Runtime resumes repair]
    P --> M
    O -->|Passed| Q[Merge Queue]
    Q --> R[Merge Coordinator]
    R -->|Conflict / failed| P
    R -->|Committed| S[Kernel emits committed event]
    S --> T[Diagram updates Chunk]
    T --> U[Unlock downstream]
    U --> H
    T --> V{All graph acceptance met?}
    V -->|No| H
    V -->|Yes| W[Project-level integration validation]
    W --> X{Product acceptance}
    X -->|Failed| B
    X -->|Passed| Y[Human final approval / Delivery]
```

## 10. 状态所有权：禁止一个全局状态字段包办所有语义

不要把 Diagram、Runtime、Kernel、Runner 的状态硬塞进一个枚举。

各自保留本领域状态，并由 Project View 聚合。

  -----------------------------------------------------------------------
  状态域                  负责者                  示例
  ----------------------- ----------------------- -----------------------
  Diagram Plan            Diagram                 draft / confirmed /
                                                  frozen / completed /
                                                  superseded

  Chunk Plan              Diagram                 pending / blocked /
                                                  ready / merged / failed
                                                  / retired

  Placement               Scheduler               suggested / offered /
                                                  claimed / delivered /
                                                  failed / withdrawn

  Runtime Run             Runtime                 queued / running /
                                                  waiting_external /
                                                  paused / failed /
                                                  completed / cancelled

  Lease                   Kernel                  active / expired /
                                                  revoked / released

  Submission              Kernel                  received / validating /
                                                  accepted / rejected

  Gate                    Kernel                  pending / running /
                                                  passed / failed

  Merge                   Kernel                  queued / merging /
                                                  committed / conflict /
                                                  rejected
  -----------------------------------------------------------------------

Project Task 状态只是聚合状态，不得反向覆盖各领域权威状态。

### 10.1 状态投影规则

-   `REVIEWING` 不能仅由模型自行写入，必须有真实 Gate/Delivery
    Evidence。
-   `DONE` 必须有 Commit、完整 Evidence 和项目级验收记录。
-   迟到事件不能把终态回退。
-   同一个事件重复到达不能重复推进状态。
-   不同来源的事件冲突时，按权威所有者和版本号判断，不按到达时间简单覆盖。
-   无证据不能推进到成功状态。
-   有证据的 Gate 失败与缺少 Evidence 必须区分。

------------------------------------------------------------------------

# 第七部分：事件、可靠性与恢复

## 11. 事件协议

所有跨服务事件采用统一 Envelope：

``` json
{
  "event_id": "evt_...",
  "event_type": "kernel.submission.committed",
  "schema_version": 1,
  "occurred_at": "2026-11-02T00:00:00Z",
  "producer": "veynod-kernel",
  "workspace_id": "ws_...",
  "project_id": "prj_...",
  "diagram_id": "dgm_...",
  "diagram_version": 12,
  "chunk_id": "chk_...",
  "product_task_key": "task_...",
  "runtime_run_id": "run_...",
  "attempt_id": "att_...",
  "kernel_task_id": "kt_...",
  "lease_id": "lease_...",
  "submission_id": "sub_...",
  "placement_id": "plc_...",
  "trace_id": "trace_...",
  "correlation_id": "corr_...",
  "causation_id": "evt_parent",
  "idempotency_key": "stable-key",
  "payload": {}
}
```

以上字段允许按事件类型为空，但关联语义必须明确。

### 11.1 核心事件

至少统一以下事件语义：

``` text
diagram.confirmed
diagram.frozen
diagram.change.proposed
diagram.change.approved
diagram.change.rejected

chunk.ready
chunk.blocked
chunk.placement.requested
chunk.placement.offered
chunk.execution.claimed
chunk.execution.started
chunk.execution.heartbeat
chunk.execution.failed

runtime.run.started
runtime.run.waiting_external
runtime.run.woken
runtime.run.failed
runtime.run.cancelled

kernel.lease.granted
kernel.lease.expired
kernel.submission.received
kernel.gate.started
kernel.gate.passed
kernel.gate.failed
kernel.merge.queued
kernel.merge.committed
kernel.merge.conflicted

chunk.merged
chunk.unblocked
chunk.completed

project.validation.started
project.validation.failed
project.validation.passed
project.escalation.required
project.completed
```

实际事件命名可以适配现有协议，但必须提供一张一一映射表，不能保留多套近义事件各自驱动同一状态。

### 11.2 Outbox / Inbox

跨服务事件不得依赖"先提交数据库，再尽力发 HTTP 请求"的非原子流程。

要求：

-   状态变更与 Outbox 事件在同一数据库事务内提交；
-   消费方通过 Inbox 记录处理过的 Event ID；
-   消费操作具备幂等性；
-   事件失败有重试与 Dead Letter；
-   支持安全 Replay；
-   Replay 不得重复创建 Commit 或重复解锁；
-   每个事件保留 Schema Version；
-   提供 Outbox 积压、消费延迟和死信监控。

### 11.3 事件驱动唤醒

Runtime 在等待 Kernel Gate 时：

1.  保存当前 Run Checkpoint；
2.  持久化外部等待条件；
3.  释放 Worker；
4.  Kernel Gate 完成后发出事件；
5.  Runtime Inbox 幂等消费；
6.  通过 Run ID 唤醒对应 Run；
7.  恢复上下文，继续推理。

禁止通过长时间占用 Worker 的轮询实现等待。

### 11.4 对账机制

事件系统之外，必须存在周期性 Reconciliation：

-   Diagram 显示 ready，但 Kernel 无任务；
-   Kernel 已 committed，但 Diagram 未更新；
-   Placement 已 claimed，但 Runner 已离线；
-   Runtime 显示 running，但 Lease 已过期；
-   Gate 已通过，但 Merge Queue 没有记录；
-   Submission 已拒绝，但 Product Task 未显示失败证据。

对账器只能按权威数据修复投影或产生明确异常，不能猜测并自动伪造成功状态。

------------------------------------------------------------------------

# 第八部分：Agent 自主循环与异常升级

## 12. 自动 Loop 规则

目标是让系统在正常路径上无需人类逐个介入。

### 12.1 正常循环

``` text
READY
  -> PLACED
  -> CLAIMED
  -> RUNNING
  -> SUBMITTED
  -> VERIFYING
  -> MERGE_QUEUED
  -> COMMITTED
  -> CHUNK_MERGED
  -> DOWNSTREAM_UNBLOCKED
  -> NEXT_CHUNK
```

### 12.2 失败循环

不同失败使用不同处理策略：

  失败类型                    默认动作
  --------------------------- -------------------------------------
  模型网络错误                有上限的退避重试
  Runner 短暂离线             过期 Lease，重新 Placement
  参数错误                    Agent 自修复一次或按策略重试
  Gate 测试失败               把具体证据交给原 Agent 修复
  Write Scope 越权            拒绝 Submission，不允许自动扩大权限
  Git Merge Conflict          生成冲突上下文，重新协调或重新规划
  Interface Contract 不一致   暂停相关下游，提交 Change Proposal
  预算耗尽                    暂停并请求预算决策
  多次修复仍失败              熔断并升级人工
  安全策略违规                立即拒绝，按风险等级升级
  需求歧义改变产品行为        请求人类澄清

### 12.3 重试预算

每个 Chunk 必须有：

-   最大执行 Attempt；
-   最大自动修复次数；
-   最大模型调用/Token/金额；
-   最大墙钟时间；
-   最大无进展时间；
-   失败熔断阈值。

重试必须产生可追溯 Attempt，不得覆盖之前的失败证据。

### 12.4 何时打扰人类

仅在以下情况默认升级：

-   产品需求存在无法消除的歧义；
-   需要改变冻结接口或架构边界；
-   涉及数据删除、权限、安全和不可逆操作；
-   预算或资源超限；
-   多轮修复失败；
-   外部依赖不可用且没有安全替代方案；
-   最终产品验收需要人类判断。

普通编译失败、测试失败、可自动修复的 Merge Conflict，不应立即打扰人类。

------------------------------------------------------------------------

# 第九部分：代码验证、合并与交付

## 13. Kernel 唯一交付链

``` text
Kernel Task
  -> Lease + Fencing Token
  -> Isolated Worktree
  -> Agent Changes
  -> Submission
  -> Scope Validation
  -> Verification Gate
  -> Merge Queue
  -> Merge Coordinator
  -> Commit
  -> Evidence Bundle
  -> Kernel Committed Event
```

### 13.1 Lease

Lease 必须绑定：

-   Workspace；
-   Repository；
-   Kernel Task；
-   Chunk；
-   Attempt；
-   Host；
-   Lease Epoch；
-   Work Scope Hash；
-   Expiry；
-   Idempotency Key。

Lease 过期或撤销后，旧 Agent 的迟到提交必须被 Fencing Token 拒绝。

### 13.2 Worktree

-   每个有效 Attempt 使用独立 Worktree。
-   不同 Runner 不得共享可写工作目录。
-   Worktree 创建、回收和清理由 Kernel 管理。
-   Runner 离线后可以安全恢复或重建。
-   不允许 Agent 直接写入主分支工作目录。

### 13.3 Verification Gate

保留当前已实现且经过验证的 Gate 能力，先审计现有阶段，再建立统一 Gate
Contract。

Gate 至少覆盖：

1.  Submission 身份、Lease 和幂等性；
2.  Write Scope 与 Diff 校验；
3.  文件和仓库安全策略；
4.  格式、Lint、静态类型；
5.  单元测试；
6.  接口契约和集成测试；
7.  必要的安全检查；
8.  证据完整性。

不是每个 Chunk 都必须执行全部昂贵测试。Gate Plan
可以按风险和影响范围分层，但最终合并必须满足对应策略。

### 13.4 Merge

-   只有 Merge Coordinator 能更新受保护的目标分支。
-   Merge Queue 负责串行化最终写入。
-   合并前必须验证目标基线是否变化。
-   冲突不能静默覆盖。
-   每次合并必须保存 Commit SHA、父提交、Submission、Gate Evidence 和
    Diagram Version。
-   合并失败必须产生可恢复状态。
-   同一个 Submission 重放不能生成第二个 Commit。

### 13.5 Reviewer Agent

不要无条件为每个任务额外启动一个常驻昂贵 Reviewer。

采用分层审查：

-   低风险、规则明确的 Chunk：Kernel 自动 Gate；
-   中风险：独立模型审查 Diff 和 Evidence；
-   高风险：不同 Host/Agent 的独立 Review；
-   安全、数据模型、公共接口、核心架构变更：按策略要求人类批准。

Reviewer 不能替代确定性测试，也不能自行执行最终合并。

------------------------------------------------------------------------

# 第十部分：安全与隔离

## 14. Execution Cell

多租户生产环境不得把不可信仓库代码和测试直接放在共享 Kernel 容器中执行。

目标：

-   每个执行 Attempt 使用隔离容器或隔离 Execution Cell；
-   默认不允许访问内部服务网络；
-   默认不挂载宿主机敏感目录；
-   不向 Agent 暴露数据库管理员密钥、服务密钥或其他 Workspace 凭证；
-   使用短时、最小权限凭证；
-   限制 CPU、内存、磁盘、进程数和运行时间；
-   限制出站网络；
-   对制品和日志做脱敏；
-   执行结束清理临时凭证与可写环境；
-   Gate 结果必须可审计。

### 14.1 安全验收

至少验证：

-   一个 Workspace 无法读取另一个 Workspace 的 Worktree；
-   Agent 无法读取宿主机敏感文件；
-   恶意测试无法访问内部数据库和服务；
-   过期 Lease 无法提交；
-   Scope 外文件修改必定被拒绝；
-   Runner Token 撤销后不能继续领取新任务；
-   重复事件不能绕过权限；
-   Replay 不会重新执行不可逆操作。

------------------------------------------------------------------------

# 第十一部分：用户体验

## 15. 人类工作空间

产品默认界面不应该暴露大量内部技术对象。

核心导航建议继续收敛：

1.  **Diagram**：工程目标、架构图、Chunk、依赖、总体进度。
2.  **Loop**：正在执行、等待、失败、自动修复和异常。
3.  **Documents**：需求、契约、决策和交付文档。
4.  **Settings**：Workspace、Runner、模型、预算和策略。

### 15.1 Diagram 页面

用户应该一眼看到：

-   当前工程目标；
-   当前冻结的架构版本；
-   已完成、进行中、阻塞的模块；
-   并发执行情况；
-   关键路径；
-   预计剩余工作；
-   当前风险；
-   是否发生架构变更。

### 15.2 Loop 页面

展示：

-   哪些 Agent 正在工作；
-   它们在哪台设备上；
-   当前任务；
-   当前阶段；
-   等待原因；
-   最近一次进展；
-   是否正在自动修复；
-   是否需要人类干预。

默认不展示冗长的模型思考内容。提供可追溯的工具调用、执行日志和证据，但不让用户承担日常技术监控。

### 15.3 人类注意力指标

把"节省人类注意力"变成可测量的产品指标：

-   每个项目的人类干预次数；
-   每次干预所需决策时间；
-   自动修复成功率；
-   无人值守完成率；
-   因信息不完整造成的人工追问；
-   每个成功合并 Chunk 的人工操作数；
-   从需求确认到最终交付的人类主动操作次数。

目标不是让系统永远不问人，而是只在人的判断真正能改变结果时提问。

------------------------------------------------------------------------

# 第十二部分：重构实施路线

## 16. 总体策略

采用 **Strangler / Adapter 渐进迁移**：

-   先明确统一契约；
-   再把现有路径接入统一契约；
-   再把执行权逐步迁移到 Kernel；
-   最后关闭重复旧路径。

禁止先删除旧实现，再期望新实现自然接上。

## 17. Phase 0 --- 现状验证与架构盘点

**目标：** 形成可信的当前系统地图。

任务：

-   [ ] 确认 HEAD、分支和部署版本；
-   [ ] 盘点服务、Worker、CLI、MCP、API；
-   [ ] 盘点所有任务创建入口；
-   [ ] 盘点所有任务领取入口；
-   [ ] 盘点所有 Worktree / Lease 创建路径；
-   [ ] 盘点所有 Patch / Submission 路径；
-   [ ] 盘点所有 Gate / Merge 路径；
-   [ ] 盘点所有 Diagram 状态更新和下游解锁路径；
-   [ ] 绘制真实数据关系；
-   [ ] 绘制真实事件流；
-   [ ] 盘点所有状态机及其写入者；
-   [ ] 记录当前测试基线；
-   [ ] 记录生产部署版本和配置。

交付：

-   `00_CURRENT_STATE_VERIFICATION.md`
-   `01_CURRENT_ARCHITECTURE.mmd`
-   `02_STATE_OWNERSHIP_MATRIX.md`
-   `03_EXECUTION_PATH_INVENTORY.md`
-   `04_GAP_AND_RISK_REGISTER.md`

**退出条件：** 每一个关键结论都有代码位置、测试或运行证据。

## 18. Phase 1 --- 统一任务和执行契约

**目标：** 先解决模块之间的语义分裂。

任务：

-   [ ] 定义 Chunk / Product Task / Runtime Run / Kernel Task / Attempt
    / Placement 的映射；
-   [ ] 定义 `WorkScope`；
-   [ ] 定义统一 ID 关联规则；
-   [ ] 定义 Lease 与 Fencing 规则；
-   [ ] 定义事件 Envelope；
-   [ ] 定义 Outbox / Inbox 幂等规则；
-   [ ] 定义状态权威所有者；
-   [ ] 定义旧字段到新契约的兼容映射；
-   [ ] 增加契约级测试。

交付：

-   `contracts/work-item.schema.json`
-   `contracts/work-scope.schema.json`
-   `contracts/event-envelope.schema.json`
-   `docs/refactor/10_EXECUTION_CONTRACTS.md`
-   兼容适配器与测试。

**退出条件：** Diagram 的 Write Scope 可以无损到达 Kernel Lease，并在
Submission Diff 校验中得到验证。

## 19. Phase 2 --- Diagram 到 Kernel 的闭环

**目标：** 让图中的 Chunk 真正成为 Kernel 唯一代码交付链上的工作单元。

任务：

-   [ ] Diagram Freeze 生成不可变版本快照；
-   [ ] Chunk Ready 事件进入统一 Scheduler；
-   [ ] 每个 Chunk 只绑定一个有效 Kernel Task；
-   [ ] Kernel Task 绑定 Diagram Version / Chunk / Product Task；
-   [ ] WorkScope 传入 Kernel；
-   [ ] Kernel Lease 创建独立 Worktree；
-   [ ] Patch Submission 回写 Chunk 关联；
-   [ ] Gate 结果投影回 Diagram；
-   [ ] Merge Commit 事件更新 Chunk；
-   [ ] 上游 Chunk 合并后自动解锁下游；
-   [ ] 失败证据返回原 Run；
-   [ ] 旧 Diagram Gate/Merge 与 Kernel 路径建立明确所有权边界；
-   [ ] 对账器修复事件丢失造成的投影不一致。

**关键要求：** 对 Kernel-owned Chunk，旧 Diagram Merge 不得再独立执行
Git 合并。Diagram 负责图状态和解锁，Kernel 负责代码交付。

**退出条件：** 一个真实 Chunk 从图派发到 Git
Commit，再到下游自动解锁，整个过程只有一条权威交付链。

## 20. Phase 3 --- 统一 Runtime 与多设备 Runner

**目标：** 不同设备和不同模型工具使用同一执行协议。

任务：

-   [ ] 定义 Execution Adapter Protocol；
-   [ ] 将 Runtime-native Agent 接入统一 Adapter；
-   [ ] 将 Claude Code Runner 接入统一 Adapter；
-   [ ] 将 Codex Runner 接入统一 Adapter；
-   [ ] 每个 Attempt 只能选择一个 Adapter；
-   [ ] Scheduler 只向一个执行者派发；
-   [ ] Host 离线后 Lease Fencing 和重派发；
-   [ ] Runtime 外部等待与 Runner 任务状态一致；
-   [ ] Checkpoint / Resume；
-   [ ] Cancel / Pause / Withdraw；
-   [ ] 多 Host 并发；
-   [ ] 不同架构设备的兼容性测试。

**退出条件：** 两台以上真实设备可以同时领取不同
Chunk，且不会重复执行、共享写目录或争夺同一 Lease。

## 21. Phase 4 --- 架构规划 Agent 产品化

**目标：** 让工程图不只是数据结构，而是专业规划能力的机器输出。

任务：

-   [ ] 定义 Architecture Planner Role；
-   [ ] 建立仓库发现与代码索引输入；
-   [ ] 建立需求结构化输出；
-   [ ] 建立架构图生成 Schema；
-   [ ] 建立 Chunk 自动拆分规则；
-   [ ] 建立 Interface Contract 生成；
-   [ ] 建立 Write Scope 生成；
-   [ ] 建立 DAG 和冲突分析；
-   [ ] 建立验收条件生成；
-   [ ] 建立 Graph Validator；
-   [ ] 建立高风险 Architecture Critic；
-   [ ] 建立 Change Proposal；
-   [ ] 建立人类确认和 Freeze；
-   [ ] 建立规划质量基准测试集。

**退出条件：** 给 Planner
一个真实仓库和明确需求，可以生成通过机器校验、可派发、可验证的工程图，而不是只生成自然语言计划。

## 22. Phase 5 --- 自动修复与无人值守 Loop

**目标：** 让正常工程推进无需人类逐个操作。

任务：

-   [ ] Gate 失败自动回传 Evidence；
-   [ ] 原 Agent 恢复上下文；
-   [ ] 自动修复有预算和次数限制；
-   [ ] Merge Conflict 自动恢复；
-   [ ] 上游合并自动解锁下游；
-   [ ] 图内所有节点完成后执行项目级集成验证；
-   [ ] 发现系统性问题生成 Change Proposal；
-   [ ] 超出自动处理能力时生成可执行的人工决策请求；
-   [ ] 记录每次自动循环的因果链。

**退出条件：**
至少一个真实中型工程在没有人工逐任务干预的情况下，从冻结图推进到最终集成验证；人工只处理预先定义的升级事件。

## 23. Phase 6 --- 安全与生产资格

**目标：** 让系统具备可信的多设备、多 Workspace 运行条件。

任务：

-   [ ] Execution Cell 隔离；
-   [ ] 默认关闭不安全的 Local Gate 模式；
-   [ ] 短时凭证；
-   [ ] 网络隔离；
-   [ ] 资源限制；
-   [ ] 恶意仓库测试；
-   [ ] 多租户越权测试；
-   [ ] Kernel API / DB / Runtime 故障恢复；
-   [ ] 100 / 250 / 500 / 1000 Runner 分级容量测试；
-   [ ] 真实生产部署和回滚演练；
-   [ ] 生产观测与告警。

**退出条件：**
安全阻断项清零；容量目标有可复现数据；部署版本与验收版本一致。

------------------------------------------------------------------------

# 第十三部分：测试与验收

## 24. 必须建立的 E2E 验收场景

### E2E-01：单 Chunk 完整交付

``` text
Create Project
-> Create Diagram
-> Confirm
-> Freeze
-> Dispatch
-> Runner Claim
-> Kernel Lease
-> Worktree
-> Agent Change
-> Submission
-> Gate Passed
-> Merge
-> Chunk Merged
-> Product Task Updated
```

### E2E-02：多 Chunk 并发

-   至少 3 个无冲突 Chunk；
-   至少 2 台真实 Runner；
-   同时运行；
-   每个 Chunk 有独立 Worktree；
-   所有结果成功合并；
-   没有重复 Attempt；
-   图状态全部正确。

### E2E-03：依赖自动解锁

-   Chunk B 依赖 Chunk A；
-   A 未合并时 B 不得执行；
-   A 合并后产生事件；
-   B 自动进入 Ready；
-   Scheduler 自动 Placement；
-   不需要人工点击派发。

### E2E-04：写集冲突保护

-   两个 Chunk 申请同一文件；
-   未声明依赖时不得同时获得冲突写权限；
-   强制伪造越权 Patch；
-   Kernel 必须拒绝；
-   审计记录必须保留。

### E2E-05：Gate 失败自动修复

-   第一次 Submission 测试失败；
-   Evidence 回传 Runtime；
-   Agent 恢复并修复；
-   第二次 Submission 通过；
-   最终只有一个有效 Commit；
-   历史失败证据仍存在。

### E2E-06：Runner 中途离线

-   Runner Claim 后突然断网；
-   Lease 过期或被撤销；
-   旧 Runner 迟到提交被 Fencing 拒绝；
-   新 Runner 接管；
-   不重复提交、不污染主分支。

### E2E-07：重复事件

-   重复投递 `kernel.merge.committed`；
-   Diagram 只推进一次；
-   下游只解锁一次；
-   不生成重复 Placement；
-   不重复创建 Run。

### E2E-08：图变更

-   图冻结后提出接口修改；
-   系统识别受影响 Chunk；
-   阻止受影响下游继续；
-   人类批准新版本；
-   保留旧版本和历史 Evidence；
-   仅重新执行受影响节点。

### E2E-09：真实模型多轮运行

-   至少两个不同模型/Adapter；
-   多轮真实推理；
-   参数自修复；
-   工具调用；
-   外部等待；
-   失败恢复；
-   最终提交与证据。

### E2E-10：项目级最终验收

-   所有 Chunk 已完成；
-   执行完整项目级测试；
-   检查需求覆盖；
-   检查接口一致性；
-   生成交付摘要；
-   由人类做最终产品验收。

## 25. 性能指标

不得只报告 Worker 数量或任务创建速度。

必须测量：

  指标                      含义
  ------------------------- -----------------------------------------
  Ready Queue Wait          Chunk 从就绪到被领取的等待时间
  Placement Latency         调度和派发耗时
  Agent Startup Latency     Agent 启动时间
  Execution Duration        实际执行时间
  Gate Duration             验证耗时
  Merge Latency             合并等待和执行耗时
  End-to-End Completion     从派发到 Commit 的总耗时
  Throughput                每分钟成功合并 Chunk 数
  Retry Rate                自动重试比例
  Repair Success Rate       自动修复成功比例
  Merge Conflict Rate       合并冲突比例
  Runner Utilization        Runner 有效利用率
  Agent Idle Time           Agent 空闲时间
  Cost per Merged Chunk     每个成功合并 Chunk 的模型与基础设施成本
  Human Intervention Rate   每个项目需要人工介入的次数

所有延迟都要同时报告 P50 / P95 /
P99，并区分客户端、服务端和真实端到端耗时。

### 25.1 容量测试原则

-   先测 10，再测 50、100、250、500、1000；
-   区分任务数量、在线 Runner 数、活动 Agent 数和并发 Gate 数；
-   压测客户端不能成为未识别的瓶颈；
-   记录数据库连接、事件积压、模型限流、CPU、内存和磁盘；
-   不能把 1000 个已创建任务称为 1000 并发执行；
-   每一级必须完成任务并核对 Commit 与 Evidence；
-   不允许只用 Mock 模型结果宣称真实模型吞吐。

------------------------------------------------------------------------

# 第十四部分：Definition of Done

## 26. 整体重构完成标准

只有同时满足以下条件，才可以宣称 Veynod 已完成本轮目标架构重构。

### 架构

-   [ ] Diagram 是唯一工程计划权威；
-   [ ] Kernel 是唯一代码交付权威；
-   [ ] Runtime 是唯一逻辑 Agent Run 权威；
-   [ ] Scheduler 是唯一 Placement 权威；
-   [ ] Product Task 不再形成独立竞争执行链；
-   [ ] 所有跨域对象都有稳定关联键；
-   [ ] 状态所有权明确；
-   [ ] 没有重复的 Gate / Merge 权威路径。

### 图驱动执行

-   [ ] Planner 能生成机器可验证工程图；
-   [ ] Chunk 有独立契约、写集和验收标准；
-   [ ] 图冻结后自动派发就绪节点；
-   [ ] 依赖满足后自动解锁；
-   [ ] 图变更有版本和影响分析；
-   [ ] 所有 Chunk 都能追溯到 Commit。

### 分布式并发

-   [ ] 两台以上真实 Runner 同时执行；
-   [ ] 不同 Agent 使用独立 Worktree；
-   [ ] 不会重复领取同一 Attempt；
-   [ ] 写集冲突可阻断；
-   [ ] Runner 离线可安全接管；
-   [ ] Runtime 与 Host Runner 使用统一 Adapter Protocol；
-   [ ] 中央控制面不承担所有代码执行。

### 自动 Loop

-   [ ] Gate 失败可以自动修复；
-   [ ] Merge Conflict 可以进入恢复流程；
-   [ ] 上游成功后下游自动推进；
-   [ ] 外部等待不占用 Worker；
-   [ ] 重复事件不会重复交付；
-   [ ] 超限或不可恢复错误能够准确升级人工。

### 安全与可靠性

-   [ ] Gate 执行环境隔离；
-   [ ] Lease Fencing 生效；
-   [ ] Write Scope 强制校验；
-   [ ] 多租户隔离通过；
-   [ ] 核心服务重启恢复通过；
-   [ ] Outbox / Inbox 幂等和 Replay 通过；
-   [ ] 生产部署与验收版本一致。

### 人类体验

-   [ ] 用户只需提出需求和确认关键设计；
-   [ ] 不需要人工逐个派发普通任务；
-   [ ] 不需要人工逐个执行 Gate；
-   [ ] 不需要人工手动解锁普通下游任务；
-   [ ] 用户可以理解系统当前状态和等待原因；
-   [ ] 只有真正需要判断的事项才打扰人类。

------------------------------------------------------------------------

# 第十五部分：Codex 实际工作规则

## 27. Codex 的角色

Codex 在本项目中不是自由发挥的开发
Agent，而是受本架构规范约束的重构执行者。

必须：

1.  先读取本文和 `00_CURRENT_STATE_VERIFICATION.md`。
2.  每个 Phase 开始前先列出涉及模块、数据迁移、兼容风险和验收测试。
3.  先增加或更新测试，再修改实现。
4.  先实现统一契约，再迁移调用方。
5.  每个 Phase 单独提交。
6.  不得同时启动多个 Agent 修改同一组核心状态机文件。
7.  对数据库迁移提供向前兼容与回滚策略。
8.  每次改动后运行最小相关测试，再运行全量测试。
9.  记录所有无法完成的验收项，不得隐藏。
10. 遇到架构决策冲突时，优先遵守本文的权责边界；涉及产品取舍时升级人类。

### 27.1 每个 Phase 的交付格式

每个 Phase 必须交付：

``` text
docs/refactor/phase-N/
  PLAN.md
  IMPLEMENTATION_REPORT.md
  TEST_REPORT.md
  MIGRATION_REPORT.md
  RISK_REGISTER.md
```

其中：

-   PLAN：实施范围、文件、步骤和风险；
-   IMPLEMENTATION_REPORT：实际改了什么，哪些没有改；
-   TEST_REPORT：命令、环境、结果和失败；
-   MIGRATION_REPORT：Schema、数据迁移、兼容和回滚；
-   RISK_REGISTER：未解决问题、证据和后续负责人。

### 27.2 严禁事项

-   禁止未经盘点删除旧模块；
-   禁止只改 README 让文档看起来统一；
-   禁止使用 Mock E2E 代替真实 Runner E2E；
-   禁止让两个 Scheduler 同时领取同一个任务；
-   禁止让 Diagram 和 Kernel 同时拥有 Git Merge 权；
-   禁止通过放宽数据库约束掩盖状态机错误；
-   禁止把所有服务合并成一个巨型 Runtime Worker；
-   禁止把所有代码执行放进中心服务；
-   禁止为了提高并发而取消写集隔离；
-   禁止为了通过测试而降低验收标准；
-   禁止把未验证功能标记为生产可用。

## 28. 推荐实施顺序

``` text
Phase 0 现状审计
   |
Phase 1 统一契约与状态所有权
   |
Phase 2 Diagram -> Kernel 完整闭环
   |
Phase 3 Runtime / Runner 统一执行协议
   |
Phase 4 Architecture Planner 产品化
   |
Phase 5 无人值守自动 Loop
   |
Phase 6 安全、容量、生产资格
```

不得为了尽快展示多 Agent 并发效果而跳过 Phase 1 和 Phase 2。

------------------------------------------------------------------------

# 附录 A：本轮审查重点核对项

以下是需要 Codex 在 Phase 0
重新验证的具体线索，不是未经复核即可视为事实的永久结论。

  ---------------------------------------------------------------------------------------------------------------------------
  审查线索                重点位置                                                        需要证明的事情
  ----------------------- --------------------------------------------------------------- -----------------------------------
  Diagram 写入范围        `AgentGraph/app/services/diagrams.py`                           `allowed_write_paths` 如何进入
                                                                                          Kernel Lease

  Kernel 租约             `AgentGraph/app/services/kernel_delivery.py`                    Write Scope
                                                                                          的实际读取、规范化和校验

  Kernel 工具             `agent_runtime/tools/kernel.py`                                 Runtime 使用的工具与能力边界

  Runner 执行             `AgentGraph/cli/bin/runner.mjs`                                 Worktree、Lease、Submission
                                                                                          的真实路径

  Scheduler               `AgentGraph/app/services/scheduler.py`                          Work / Review / Merge Placement
                                                                                          的所有权

  Diagram 状态            `AgentGraph/app/services/diagrams.py`                           Gate、Merge、Chunk 状态与下游解锁

  事件投影                `AgentGraph/app/services/event_dispatch.py`、`code_engine.py`   Kernel 事件如何进入产品与 Diagram
                                                                                          状态

  MCP 工具                `AgentGraph/app/mcp_server/server.py`                           Diagram、Loop、任务工具的真实入口

  统一验收                `docs/UNIFIED_ACCEPTANCE_REPORT.md`                             报告中的测试与当前 HEAD 是否一致

  生产运维                `docs/PRODUCTION_OPERATIONS.md`                                 生产镜像是否落后于当前代码
  ---------------------------------------------------------------------------------------------------------------------------

相关仓库：

-   https://github.com/MuadibUsul/veynod
-   https://github.com/MuadibUsul/veynod/tree/main/AgentGraph
-   https://github.com/MuadibUsul/veynod/tree/main/agent_runtime
-   https://github.com/MuadibUsul/veynod/tree/main/veynod_kernel
-   https://github.com/MuadibUsul/veynod/tree/main/contracts

------------------------------------------------------------------------

# 附录 B：最终产品判断标准

不要以"有多少 Agent""有多少服务""有多少代码"衡量 Veynod 是否成功。

最终只看这条链是否可靠：

**人类提出一个真实的软件需求，Veynod
能否独立理解现有工程，生成合理的最小任务图，在多台设备上并发推进，严格遵守接口和写入边界，自动验证和合并，失败时自行恢复，直到完成图定义的产品目标，并且只在真正需要人类判断时打断人类。**

如果这条链可以被真实项目反复验证，Veynod 才真正成为它想成为的产品。
