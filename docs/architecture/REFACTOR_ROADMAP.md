# REFACTOR_ROADMAP — 渐进式重构路线

> 原则：Strangler Pattern；先审计、先契约、先修正确性/安全，再扩功能/容量；每一阶段独立可验收、可回滚。
> 当前状态：**Phase 0 审计完成，但架构就绪门槛未通过**（见 `CURRENT_STATE.md` §9）。
> 本文是路线图，不是实施授权。Phase 1 及以后需要单独授权。

---

## 0. 阶段总览

| 阶段 | 目标 | 前置 | 主要交付 | 验收 | 回滚 |
|---|---|---|---|---|---|
| Phase 0 | 架构冻结与仓库审计 | 无 | 本文档集 + ADR 索引 | 证据化盘点完成 | 不涉及运行变更（本轮仅有用户指令的 Kernel 替换） |
| Phase 1 | 契约与身份基础 + P0 修复 | Phase 0 | 统一 ID、状态命令、事件 envelope、错误码、认证、code_engine 测试 | 跨服务契约测试、状态机测试、P0 安全测试 | 保留旧路径/Feature Flag |
| Phase 2 | Durable Agent Runtime | Phase 1 | Agent Definition/Run/Step/Checkpoint/Model Gateway/Tool Runtime | 重启恢复、幂等、取消、预算、子 Agent | 新 Runtime 并行，旧 runner 保留 |
| Phase 3 | 统一任务生命周期与事件投影 | Phase 1/2 | Kernel Outbox→Runtime/AgentGraph 投影、取消传播、对账 | Kernel 完成/失败/取消可恢复；无第二条主链路 | 投影可停，读旧 API |
| Phase 4 | Kernel 多仓库与 Execution Cell | Phase 1/3 | Repository Registry、Cell、Placement、Scheduler Partition、公平调度 | 多仓库/Cell 故障恢复/无双重 authority | 单 Cell 回退 |
| Phase 5 | Code Intelligence & Concurrency | Phase 4 | Snapshot 协议、Read/Write/Contract Set、动态写集、冲突升级、受影响测试 | 同符号冲突/无关并行/动态写集授权/无静默覆盖 | 关闭新锁策略，回退文件级 |
| Phase 6 | 安全执行与交付 | Phase 1/5 | Sandbox Runtime、Gate Pipeline、Merge Recovery、Delivery Evidence | 不可信代码隔离、旧 worker 不能覆盖、失败不污染 main | 切回旧 gate/merge |
| Phase 7 | 产品体验与开发者平台 | Phase 2/3/6 | Project/Agent Workspace/Execution Graph/Code Review/Delivery/API/CLI | 用户能创建/理解/控制/审查/批准交付 | UI Feature Flag |
| Phase 8 | 容量、可靠性与生产认证 | Phase 6/7 | 100/500/1,000 压测、故障注入、SLO、运维手册 | 目标负载无非终态、无跨租户、可恢复、成本可核算 | 保留上一版本部署 |

---

## 1. Phase 0 — 架构冻结与仓库审计

### 1.1 状态

**审计交付完成。** 交付物：

- `CURRENT_STATE.md`
- `TARGET_ARCHITECTURE.md`
- `MODULE_ASSET_MAP.md`
- `DATA_OWNERSHIP.md`
- `STATE_MACHINE_INVENTORY.md`
- `INTEGRATION_MAP.md`
- `CAPACITY_BASELINE.md`
- `REFACTOR_ROADMAP.md`
- `RISK_REGISTER.md`
- `docs/adr/ADR_INDEX.md`

### 1.2 验收判定

| 条件 | 结果 |
|---|---|
| 核心模块有归属 | 通过 |
| 核心表有写入权威 | **未通过**（多写入方） |
| 核心状态转换有负责人 | **未通过**（无单一 FSM） |
| 主链路有调用图 | 通过 |
| 已验证能力有测试基线 | 部分通过 |
| 未完成能力明确标记 | 通过 |
| 未把文档当实现 | 通过 |
| 未开始大规模破坏性迁移 | 通过（仅用户指令的 Kernel 运行副本替换） |

### 1.3 结论

Phase 0 **审计完成**，但架构就绪门槛未通过。不得直接进入 Phase 2+ 大规模重构；Phase 1 应先完成契约/身份与 P0 修复。

---

## 2. Phase 1 — 契约与身份基础 + P0 修复

### 2.1 目标

先建立跨领域统一身份、协议、状态命令和最小安全基线，避免后续模块改造互相冲突。

### 2.2 前置

- Phase 0 文档评审通过；
- 明确 Project/Repository/CodingSession 的归属（至少 ADR 决策）；
- 确认不做数据库合并/微服务拆分。

### 2.3 工作项（最小范围）

#### A. 身份与契约

1. 定义并冻结 ID：`tenant_id`、`workspace_id`、`project_id`、`repository_id`、`coding_session_id`、`product_task_id`、`agent_run_id`、`workflow_id/version`、`kernel_task_id`、`attempt_id`、`submission_id`、`verification_run_id`、`commit_sha`、`delivery_id`；
2. 定义 Product Task / Agent Run / Workflow / DAG Task / Attempt 关系；
3. 定义 Agent Runtime ↔ Kernel API Contract；
4. 定义事件 envelope：`event_id`、`type`、`version`、`tenant/workspace/project`、`aggregate_type/id/version`、`occurred_at`、`correlation_id`、`causation_id`、`producer`、`payload_ref`、`payload`；
5. 定义错误码、Idempotency Key、Correlation/Causation、版本兼容、认证/服务身份；
6. 建立 Contract Tests（至少 AgentGraph↔Kernel code run）。

#### B. 状态命令层

7. AgentGraph `TaskCommandService`：所有 status 写入经命令；转换矩阵；expected_version/actor/reason/event_id/idempotency；
8. Kernel `DagTaskCommandService`：所有 status 写入经命令；增加 CANCELLED；处理 SUSPENDED；
9. 取消/重试/超时语义：产品层取消意图 → Runtime 停新动作 → Orchestrator 停子任务 → Kernel 取消 → Worker/Sandbox 终止 → 外部副作用对账 → 各域记录终态；
10. 上游 FAILED/SKIP 的下游处理；
11. heartbeat lease 过期校验；
12. merge 临界区复核 task status + lease token + submission status。

#### C. P0 安全与证据

13. 禁止合成 gate 证据：AgentGraph 只读 Kernel `gate_results`/`merge_intents`；
14. code_engine 纳入 git；补契约/失败/取消测试；
15. 生产默认：强 admin key、禁止 `local` gate、绑定 loopback/内网、DAG/dashboard 鉴权/关闭；
16. Kernel token 绑定 workspace；admin token revoke/rotate；
17. 迁移 ledger 修复（v4_006–008）。

#### D. 事件

18. AgentGraph SSE 使用 `seq` 而非 `row.id`；补 `has_more`/续传；
19. Kernel outbox consumer/DLQ/retention 或明确“仅轮询”；
20. 在 Phase 1 先建立“事件契约 + 测试”，不要求立刻做完整投影（Phase 3）。

### 2.4 交付物

- `contracts/identifiers`、`contracts/events`、`contracts/agent_runtime`、`contracts/kernel`、`contracts/errors`；
- `docs/protocols/API_CONTRACTS.md`、`EVENT_CONTRACTS.md`；
- 状态命令层代码 + 测试；
- code_engine 测试 + CI；
- P0 安全修复 + 负向测试；
- 迁移 ledger 修复；
- ADR-001～006、009、011、018 的 Accepted/Proposed 记录。

### 2.5 验收

- 不同服务可独立验证协议；
- 旧客户端兼容策略明确；
- 事件版本可演进；
- 重复请求有明确行为；
- 跨域调用有认证和追踪 ID；
- AgentGraph/Kernel 状态转换测试覆盖合法/非法/并发/取消；
- merge 在 task lease 过期时不能推进；
- AgentGraph gate 证据来自 Kernel，不再合成；
- 负向安全测试通过（admin 401/503、跨租户、sandbox 环境、DAG 鉴权）。

### 2.6 回滚

- 状态命令层用 Feature Flag 切换；旧写入路径保留一个迁移窗口；
- 契约版本化，旧 API 继续服务；
- 安全默认值变更提供环境开关（仅非生产）；
- 不做不可逆数据迁移。

---

## 3. Phase 2 — Durable Agent Runtime

### 3.1 目标

建立真正持久化、可恢复的 Agent Loop，替代进程内 `while` 循环。

### 3.2 工作项

Agent Definition、Run Manager、Loop Controller、Run 状态机、Step 持久化、Checkpoint、Context Manager、Model Gateway、Tool Runtime、Budget Manager、Retry/Cancellation/Recovery、Delegation、Run Event、Observability。

### 3.3 交付物

- `agent_runtime/` 模块；
- Run/Step/Checkpoint 数据模型（独立 schema/database）；
- Agent Loop API；
- Model/Tool Contract；
- Recovery 测试；
- 端到端 Agent Run Demo。

### 3.4 验收

- 进程重启后恢复 Run；
- 重复事件不重复执行已完成动作；
- 工具调用结果可追溯；
- 取消可传播；
- 预算阻止超额；
- 子 Agent 可委派/回收；
- 模型调用与内部调度延迟可区分；
- 已记录历史可重放为相同业务状态（不要求 LLM 文本一致）。

### 3.5 回滚

新 Runtime 并行运行，按 Workspace/Feature Flag 切流；旧 runner 保留；不双写同一 Run。

---

## 4. Phase 3 — 统一任务生命周期与事件投影

### 4.1 目标

解决 AgentGraph/Kernel/Agent Runtime 三套任务状态不一致。

### 4.2 工作项

保留领域边界；接入 Agent Run；Workflow→DAG 映射；Kernel Outbox；Runtime Inbox；AgentGraph Projection；事件重试/死信/重放；取消传播；失败/重试闭环；状态版本检查；端到端故障恢复。

### 4.3 交付物

Task Lifecycle Contract、Event Dispatcher、Inbox/Outbox、Projection Worker、状态对账工具、故障注入测试。

### 4.4 验收

- Kernel 完成而 AgentGraph 未同步时可恢复；
- 事件重复不重复完成；
- 乱序不覆盖新状态；
- 取消/重试可追踪；
- 没有第二条独立代码任务主链路。

### 4.5 回滚

投影可停；AgentGraph 读旧 API；Kernel 保持权威；事件保留可重放。

---

## 5. Phase 4 — Kernel 多仓库与 Execution Cell

### 5.1 目标

从单 workspace/repo 升级为多仓库、可分片、可故障隔离的执行体系。

### 5.2 工作项

Repository Registry、Tenant/Workspace/Project 映射、Cell Registry、Repository Placement、Scheduler Partition、Scheduler Authority、Lease/Fencing、Worker/Gate/Merge Pool、Cell Health/Capacity、Repository Migration、Cell Failure Recovery、Fair Scheduling。

### 5.3 验收

- 一个 Cell 管理多个 Repository；
- 不同 Cell 独立扩容；
- Repository 迁移无双重 Authority；
- 旧 Worker 失去租约后不能提交；
- 单 Tenant 不无限挤占；
- 不同 Repository 并行 Merge。

### 5.4 回滚

Repository 迁移用租约交接/Fencing；可迁回原 Cell；不删除旧 placement 记录。

---

## 6. Phase 5 — Code Intelligence & Concurrency Upgrade

### 6.1 目标

安全并行修改同一 Repository，避免全局串行化/静默覆盖。

### 6.2 工作项

Snapshot Binding、AST/Symbol/Dependency Graph Versioning、Read/Write/Contract Set、File/Symbol/Contract/Dependency 冲突检测、Dynamic Write Set Extension、Version Validation、Affected Test Selection、Conflict Escalation、Merge Precondition、并发回放测试。

### 6.3 验收

- 同符号冲突识别；
- 无关符号并行；
- 公共接口变更触发契约验证；
- 动态写集扩展经 Kernel 授权；
- 过期快照提交不能无条件通过；
- 无静默覆盖；
- 最终 Commit 可追溯 task/attempt/snapshot。

### 6.4 回滚

文件级锁回退；关闭动态写集扩展；保留旧 gate 路径一个窗口。

---

## 7. Phase 6 — 安全执行与交付 Pipeline

### 7.1 目标

代码执行、验证、合并、交付形成生产级闭环。

### 7.2 工作项

Sandbox 生命周期、CPU/Memory/Disk 限制、网络隔离、凭证隔离、Gate Worker 恢复、Verification Evidence、Submission Idempotency、Merge Intent、CAS/Fencing、Git Ref 原子更新、Artifact 管理、安全审计、Delivery Record。

### 7.3 验收

- 不可信代码隔离运行；
- Worker 失效可恢复；
- 旧 Worker 无法覆盖新提交；
- Gate 结果可追溯；
- 失败提交不污染 main；
- Commit/Delivery 一致可审计；
- merge 复核 task lease/submission。

### 7.4 回滚

切回旧 gate/merge；保留 sandbox 镜像版本；Git 引用由 CAS 保护，不重置历史。

---

## 8. Phase 7 — 产品体验与开发者平台

### 8.1 目标

把执行引擎变成完整产品。

### 8.2 工作项

Project Workspace、Agent Workspace、Agent Run Visualization、Execution Graph、Code Review、Delivery View、Runtime Observability、API/SDK、CLI、Tenant Quota、Usage Metering、Audit、企业权限、开发者文档。

### 8.3 验收

- 用户能创建项目和编码任务；
- 能理解 Agent 状态/等待原因；
- 能查看/控制 Agent Run；
- 能看 DAG/阻塞原因；
- 能审查 diff/Gate 证据；
- 能批准/拒绝交付；
- API/CLI 稳定；
- 用量/成本可核算。

### 8.4 回滚

UI Feature Flag；旧 Code 页面保留；API 版本化。

---

## 9. Phase 8 — 容量、可靠性与生产认证

### 9.1 目标

以真实生产配置完成容量、故障恢复和安全验收。

### 9.2 工作项

100/500/1,000 完整链路；1,000 Connected；1,000 级任务/事件压力；多 Repository 并发；单 Repository 高冲突；Gate/Merge/DB 饱和；Worker 批量故障；Cell 故障；Provider 限流；Soak；成本核算；SLO/运维手册。

### 9.3 验收

- 目标负载无非终态；
- 失败分类明确；
- 状态可恢复；
- 无跨租户泄露；
- 无未授权提交；
- 容量/成本有真实测量；
- 部署/升级/回滚演练。

### 9.4 回滚

上一版本部署；数据库备份/PITR；Feature Flag；不删除已提交 Git。

---

## 10. 迁移顺序（推荐）

```text
Current AgentGraph + Kernel
  -> Phase 0 audit (done)
  -> Phase 1 contracts/identity + P0 fixes
  -> New Agent Runtime in parallel (Phase 2)
  -> Runtime handles selected runs
  -> Event projection to AgentGraph (Phase 3)
  -> Kernel multi-repo/Cell (Phase 4)
  -> New execution/verification pipeline (Phase 5/6)
  -> Gradual traffic migration
  -> Old code path disabled
  -> Legacy code removed after evidence
```

---

## 11. 硬约束

1. 不做 Big Bang Cutover；
2. 不允许新旧 Scheduler 同时对同一任务有调度权；
3. 不允许新旧 Runner 同时提交同一任务；
4. 双写只允许明确迁移窗口，必须有对账和终止日期；优先 Outbox 投影；
5. 迁移前备份；迁移后检查记录数/状态分布/关键引用；
6. 切流必须有 Feature Flag；
7. 回滚不得丢失已提交 Git Commit；
8. 外部副作用必须对账，不能靠回滚 DB 假装撤销；
9. 旧接口下线必须有版本、公告、调用方检查；
10. 没有 Benchmark/压测不得声称容量/性能达标。

---

## 12. Phase 1 最小任务清单（可直接排期）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 1 | 冻结 ID/身份链 ADR | ADR-002 + `contracts/identifiers` | 所有对象有生成方/所有者/引用规则 |
| 2 | 统一事件 envelope | ADR-005 + contract tests | event_id/version/correlation/causation |
| 3 | AgentGraph TaskCommandService | 转换矩阵 + 命令层 | 非法转换拒绝；并发/取消测试 |
| 4 | Kernel DagTaskCommandService | 状态命令 + CANCELLED | 所有写入经命令；取消传播 |
| 5 | merge lease 复核 | 修复 + 负向测试 | 过期 lease 不能 merge |
| 6 | 去合成 gate 证据 | code_engine 读取真实 gate_results | 证据可追溯 |
| 7 | code_engine 纳入 git + 测试 | CI + 契约/失败/取消 | 主链路回归 |
| 8 | 安全默认值 | admin key/sandbox/DAG 鉴权 | 负向测试通过 |
| 9 | 迁移 ledger | v4_006–008 记录 | 版本表完整 |
| 10 | 事件 SSE seq 一致性 | SSE 用 seq + has_more | 重连不丢事件 |

完成以上 10 项前，不启动 Phase 2 的大规模 Agent Runtime 实现。

---

## 13. 一句话

**Phase 0 只完成了“看清现状”；Phase 1 要先把身份、状态、事件、取消、证据和安全基线做对，之后才谈 Agent Runtime、多仓库、容量和生产认证。**
