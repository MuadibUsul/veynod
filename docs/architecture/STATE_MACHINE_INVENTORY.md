# STATE_MACHINE_INVENTORY — 状态机与转换清单

> 目的：列出所有核心状态、转换、写入方、守卫条件和缺口；为 Phase 1 的统一任务身份/状态命令层提供基线。
> 证据：源码与测试；日期 2026-10-31。

---

## 1. AgentGraph 产品任务状态机

### 1.1 状态枚举

`AgentGraph/app/models.py:50-63`：

```text
TODO
READY
IN_PROGRESS
REVIEWING
HANDOFF
AWAITING_HUMAN
ESCALATED_TO_HUMAN
WAITING_HUMAN
DONE
FAILED
CANCELLED
```

数据库 enum 初始 9 个值，`v3_23_collaboration.sql` 增加 `WAITING_HUMAN`、`CANCELLED`。默认 `READY`。

### 1.2 允许转换（代码实际）

| 来源 | 目标 | 触发/入口 | 守卫 | 证据 |
|---|---|---|---|---|
| 创建 | TODO / READY | create task | 依赖未全部 DONE → TODO；否则 READY | `tasks.py:417-421` |
| 创建 | AWAITING_HUMAN | escalation 任务 | 系统创建 | `collab.py:758-763` |
| TODO | READY | 依赖全部 DONE | `_release_dependents` | `tasks.py:929-959` |
| READY / HANDOFF / IN_PROGRESS | IN_PROGRESS | claim | `_CLAIMABLE`；不同 live lease；清旧 lease；version+1 | `tasks.py:678-709` |
| IN_PROGRESS / REVIEWING / HANDOFF | REVIEWING / HANDOFF / DONE / AWAITING_HUMAN / FAILED | handoff | status 白名单；expected_version；claim owner；active lease；可选 fencing/epoch；REVIEWING brief | `tasks.py:988-1105` |
| REVIEWING / AWAITING_HUMAN / ESCALATED_TO_HUMAN | DONE / HANDOFF / READY | review approve | expected_version；human/commander；quality gate；deploy evidence | `tasks.py:1221-1320` |
| REVIEWING / AWAITING_HUMAN / ESCALATED_TO_HUMAN | READY / IN_PROGRESS / HANDOFF / ESCALATED_TO_HUMAN | review reject | expected_version；rework/retry 规则 | `tasks.py:1321-1386` |
| 任意非终态 | ESCALATED_TO_HUMAN | escalate | expected_version；blocking question 需 holder；普通 escalate 不检查 holder | `tasks.py:1469-1507` |
| WAITING_HUMAN | IN_PROGRESS | resolve escalation | holder 仍活着；否则 orphan READY/ESCALATED | `collab.py:869-917` |
| 任意非终态 | FAILED | cancel endpoint | expected_version；**不写 CANCELLED** | `tasks.py:1687-1707` |
| 未完成子任务 | CANCELLED | parent cancel / diagram raw SQL | 递归取消 | `collab.py:655-688`、`diagrams.py:1236-1251` |
| IN_PROGRESS / REVIEWING | READY / ESCALATED_TO_HUMAN | lease reaper | 过期/孤儿阈值 | `tasks.py:1546-1568`、`leases.py:542-571` |
| READY | HANDOFF | placement 放弃 | 3 轮失败 | `scheduler.py:421-438` |
| 任意非终态 peer_review | DONE | submit peer review | **无 version/lease/fence/audit** | `reviews.py:148-190` |
| 任意非终态 | 大多数状态 | SUPER_ADMIN update | 仅拒绝 DONE/IN_PROGRESS；不清 lease | `tasks.py:1914-1952` |
| 任意 | 旧快照 | audit undo | 无 FSM 校验 | `audit.py:280-285` |

### 1.3 守卫覆盖

| 操作 | expected_version | lease holder | fencing/epoch | 备注 |
|---|---|---|---|---|
| claim | 否 | 检查不同 live lease | 否 | FOR UPDATE + version+1 |
| renew | 否 | 是 | 是（epoch + fencing） | DB 时钟 |
| release | 否 | 非 force 是 | 否 | force 仅 admin |
| handoff | 是 | claim owner + active lease | 是 + 可选 epoch | API 2026-11 起 DB 时钟 holder |
| review | 是 | 否 | 否 | reviewer 不要求 lease |
| escalate | 是 | 仅 blocking question | 是 | 普通 escalate 可绕过 holder |
| cancel | 是 | 否 | 否 | 写 FAILED |
| reassign | 是 | 否 | 否 | admin/owner |
| admin update | 是 | 否 | 否 | 不清 lease |
| reaper | 否 | 是（扫描条件） | 否 | skip locked |
| diagram cancel | 否 | 否 | 否 | 原始 SQL，清 lease 字段但不增 epoch |
| peer review | 否 | 否 | 否 | 直接 DONE |
| audit undo | 批次冲突检查 | 否 | 否 | 状态恢复无 FSM |

### 1.4 数据库约束

- `chk_in_progress_requires_lease`：IN_PROGRESS 必须有 lease。
- `chk_reviewing_requires_briefing`：REVIEWING 必须有 brief。
- `trg_enforce_task_immutability`：DONE/FAILED 的 status/description/acceptance 不可变；**不校验合法转换**；CANCELLED 不被视为 terminal。
- `trg_sync_task_lease`：任务 lease 变化镜像到 `leases`。

### 1.5 缺口

1. **没有单一 FSM**：9+ 模块写 status；没有转换矩阵表/命令层。
2. **取消语义不一致**：用户 cancel 写 FAILED；子任务/图表写 CANCELLED；CANCELLED 不是 DB terminal。
3. **普通 escalate 不需要 holder/lease**：任何有 `task:transition` 的主体可升级他人任务。
4. **SUPER_ADMIN/undo 绕过 FSM**；admin update 不清 lease。
5. **peer review 直接 DONE**，无审计/依赖释放/质量检查。
6. **REVIEWING 的 review 不要求 lease**，reviewer 与 worker 可并发。
7. **没有 Agent Run 状态机**（不存在 Agent Runtime 领域）。

### 1.6 目标收口

Phase 1 建立 `TaskCommandService`：

```text
command = {
  task_key,
  expected_version,
  action,          # claim|release|handoff|review|escalate|cancel|reassign|...
  actor,
  reason,
  idempotency_key,
  correlation_id
}
```

所有写入经命令层；转换矩阵集中定义；每次变更写 audit + event；禁止服务直接 `task.status = ...`。

---

## 2. Kernel DAG 任务状态机

### 2.1 状态枚举

`veynod_kernel/app/models.py:54-63`：

```text
BLOCKED
READY
LEASED
VERIFYING
SUSPENDED
SUSPENDED_ESCALATED
COMMITTED
FAILED
SPLIT_ARCHIVED
```

**没有 CANCELLED。** `SUSPENDED` 无写入方（不可达）。

### 2.2 转换表

| 来源 | 目标 | 写入方 | 守卫 | 证据 |
|---|---|---|---|---|
| 创建 | BLOCKED / READY | task_factory | in_degree/依赖 | `task_factory.py:31` |
| 父 LEASED | SPLIT_ARCHIVED | mutator SPLIT | status LEASED（不校验 token/expiry）；子写集必须是父的子集 | `mutator.py:81-88, 232-245` |
| 报告者 LEASED | SUSPENDED_ESCALATED | mutator ESCALATE | status LEASED（不校验 token） | `mutator.py:386-405` |
| 受害者 LEASED/VERIFYING? | BLOCKED | mutator ESCALATE | 受害者集合排除 VERIFYING | `mutator.py:337-353` |
| 修订任务 | READY | mutator ESCALATE | — | `mutator.py:408-438` |
| 注入目标 LEASED | BLOCKED | mutator INJECT | status LEASED（不校验 token） | `mutator.py:496-517` |
| 新注入节点 | READY | mutator INJECT | 任意 `prerequisite_write_set` | `mutator.py:485-561` |
| READY | LEASED | scheduler claim | Rust 决策 + DB 条件 UPDATE（READY、无 lease、无锁重叠） | `scheduler.py:452-531` |
| LEASED | READY / FAILED | release/reaper | status + token（release）；过期（reaper） | `scheduler.py:668-690, 747-784` |
| VERIFYING | READY / FAILED | stuck reaper | `updated_at` 超时（默认 3600s） | `scheduler.py:692-721` |
| LEASED | VERIFYING | verification_gate | 单次 LEASED→VERIFYING，token + expiry | `verification_gate.py:234-259` |
| VERIFYING | LEASED / FAILED | gate reject | ORM 读改写；retry_count | `verification_gate.py:1302-1338` |
| VERIFYING | LEASED | merge revalidate | status VERIFYING + lease hash | `merge_coordinator.py:431-451` |
| VERIFYING | COMMITTED | on_task_committed | status VERIFYING；更新 workspace HEAD；递减下游 | `scheduler.py:619-655` |
| COMMITTED | LEASED / FAILED | reconcile | Git 不包含 result commit；lease hash 存在→LEASED（但不恢复 token/runner） | `merge_coordinator.py:617-641` |

### 2.3 缺失/不可达/风险

| 问题 | 影响 | 证据 |
|---|---|---|
| 无 CANCELLED / cancel API | AgentGraph 取消不停止 Kernel；gate/merge 继续 | `models.py:54-63`；无 cancel route |
| SUSPENDED 不可达 | 文档/枚举误导；bench 分类可能误判 | `models.py:59`；无写入 |
| 上游 FAILED 不递减下游 | 下游永久 BLOCKED；无 BLOCKED reaper | `scheduler.py:619-655` 只在 COMMITTED 递减 |
| heartbeat 可复活过期 lease | 过期未 reaper 的 runner 可续租 | `scheduler.py:723-745` 无 expiry 检查 |
| SPLIT/ESCALATE/INJECT 只检查 status | 并发 re-lease 可被旧 runner 操作 | `mutator.py:57-69, 386-405, 496-517` |
| `_reject` 读改写无锁 | 与 reaper/revalidate 竞态；重复 retry 计数 | `verification_gate.py:1308-1322` |
| merge 不复核 task lease/status | 过期/重排任务仍可 merge | `merge_coordinator.py:229-266`、`verification_gate.py:414-444` |
| 多个 submission 同一 lease | 第二个 submission 可能在 task 已 COMMITTED 后 finalize COMMITTED 但未跑 gate | `gate_worker.py:235-247`；`v4_002:51-52` 只唯一 `(task, lease, patch_sha)` |

### 2.4 状态机图

```text
                    ┌──────────── SPLIT ────────────┐
                    v                               │
BLOCKED ──(deps)──> READY ──claim──> LEASED ──verify──> VERIFYING ──commit──> COMMITTED
   ^                  │                 │                  │
   │                  │                 │ release/reap     │ reject
   │                  └<────────────────┘                  v
   │                                                     READY / FAILED
   │
   └── ESCALATE/INJECT victim <── LEASED (no token check)

SUSPENDED_ESCALATED: ESCALATE reporter (terminal-ish; on_task_committed can unblock)
SUSPENDED: unreachable
SPLIT_ARCHIVED: parent after SPLIT
```

---

## 3. Kernel Patch Submission 状态机

`patch_submissions.status`（`sql/v4_002_patch_submissions.sql:43-44`）：

```text
QUEUED -> RUNNING -> COMMITTED | FAILED
             ^          │
             └─ requeue ┘   (transient worker error, exponential available_at)
```

| 状态 | 写入方 | 说明 |
|---|---|---|
| QUEUED | SubmissionService.accept | 202 受理；`available_at` |
| RUNNING | GateWorker.claim | work lease；attempts+1 |
| COMMITTED | GateWorker._finalize | 仅 MERGED merge intent |
| FAILED | GateWorker._finalize | gate reject、lease lost、task failed、retries exhausted、interrupted VERIFYING |
| QUEUED（重试） | GateWorker._requeue | worker exception；backoff |

`FINAL_STATUSES = {COMMITTED, FAILED}`（`submission_service.py:24`）。

**没有 CANCELLED/CONFLICT 终态。** CONFLICT 只在 `merge_intents` 枚举里存在，从未写入。

### 3.1 关键失败语义

- 任务已 COMMITTED：重复 submission finalize COMMITTED，不重跑 gate（`gate_worker.py:235-247`）。
- 任务 FAILED：submission FAILED，task_failed。
- 任务 VERIFYING（前 worker 在 VERIFYING 后死亡）：submission FAILED `interrupted_while_verifying`，**不自动重放 gate**（`gate_worker.py:257-268`）。
- task lease 过期/不匹配：FAILED lease_expired/lease_lost；若 task 仍 LEASED，release 回 READY。
- 202 可表示已完成的重复提交（`final:true`）；调用方必须读 `final/status`，不能把 202 当“未完成”。

---

## 4. Merge Intent 状态机

`merge_intents.status`（`sql/v4_004_merge_coordinator.sql:45-46`）：

```text
PENDING -> RUNNING -> MERGED
                    -> REJECTED
                    -> REVALIDATE -> (gate rerun) -> PENDING/RUNNING
CONFLICT: 枚举存在，从未写入
```

| 状态 | 写入方 | 守卫 |
|---|---|---|
| PENDING | GateWorker._upsert_merge_intent | 已验证 patch；submission_id 唯一 |
| RUNNING | MergeCoordinator.claim | per-workspace coordinator token |
| MERGED | _finish_intent | status RUNNING + token |
| REJECTED | _finish_intent / gate reject | token/null（reconcile 可 null） |
| REVALIDATE | _revalidate / reconcile | status RUNNING + token；最多 3 次 |

**风险：** `_finish_intent` 允许 `token IS NULL`（`merge_coordinator.py:510-514`），reconcile 路径可无 token 完成；task lease/submission status 不复核。

---

## 5. Gate Worker / Merge Coordinator 生命周期

### 5.1 Worker

```text
REGISTERED -> IDLE -> CLAIMED -> INITIALIZING -> RUNNING -> CHECKPOINTING? -> COMPLETING -> IDLE
异常: HEARTBEAT_LOST / LEASE_EXPIRED / FENCED / RECOVERING / TERMINATED
```

代码没有显式状态表；用 `work_lease_id`、`work_lease_expires_at`、`status` 表示。Worker 不能仅凭本地状态认定拥有任务；finalize 必须匹配 `status='RUNNING' AND work_lease_id`。

### 5.2 Merge Coordinator

```text
ACQUIRE(workspace lease) -> RENEW -> CLAIM(intent) -> PROCESS -> FINISH -> release/renew loop
```

每个 workspace 一个 coordinator lease；token fencing 只保护 merge_intents 更新，不保护 dag_task 更新。

---

## 6. AgentGraph Diagram/Chunk 状态（旧路径）

- Diagram/Chunk 有 submit/CI/review/queue/merge 状态（`diagrams.py`），但代码执行由 runner 完成；平台只存文件/元数据。
- `merge` 是元数据操作，不做 Git 合并。
- 状态与 `collab_tasks` 可相互影响（chunk 创建任务、父任务取消子任务）。
- **结论：** 不能把 diagram 状态当代码交付状态；目标应把它定位为协作编排，不参与 Kernel code delivery。

---

## 7. 状态权威矩阵（当前）

| 状态 | 权威 | 写入方数量 | 是否有单一命令层 |
|---|---|---:|---|
| AgentGraph Product Task | AgentGraph | 9+ | 否 |
| AgentGraph Lease | AgentGraph | 4+（leases/tasks/collab/diagrams + trigger） | 否 |
| AgentGraph Event/Inbox | AgentGraph | trigger + service + DSAR | 部分 |
| Kernel DAG Task | Kernel | 6+（factory/scheduler/mutator/gate/coordinator/reconcile） | 否 |
| Kernel Patch Submission | Kernel | accept + gate worker | 大体是 |
| Kernel Merge Intent | Kernel | gate worker + coordinator | 大体是（token 可空） |
| Kernel Lease/Fencing | Kernel | scheduler/gate/coordinator | 分散 |
| Git HEAD / Commit | Kernel | workspace update/on_task_committed/reconcile | 多路径，但 CAS |

---

## 8. Phase 1 状态机收口要求

1. 为 AgentGraph Product Task 定义**唯一转换矩阵**和 `TaskCommandService`；
2. 为 Kernel DAG Task 定义**唯一状态命令/守卫**，所有写路径经它；
3. 增加 CANCELLED（Kernel）并定义取消传播；
4. 移除/实现 SUSPENDED，禁止不可达状态；
5. 上游 FAILED/SKIP 必须显式处理下游（unblock/fail/cancel），不能永久 BLOCKED；
6. heartbeat 必须验证 lease 未过期；
7. merge 临界区复核 task status + lease token + submission status；
8. submission/merge intent 增加 CANCELLED/CONFLICT（如需）并统一终态；
9. 每次转换记录 version、actor、reason、event_id、idempotency_key、correlation_id；
10. 状态机测试：合法/非法转换、并发、重试、取消、恢复。

未完成前，不得进入 Phase 2 的 Agent Runtime 大规模实现，也不得宣称“统一任务生命周期”。
