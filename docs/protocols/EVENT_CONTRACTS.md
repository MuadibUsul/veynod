# EVENT_CONTRACTS — 事件契约（Phase 1A）

> 状态：Proposed；共享定义在 `contracts/events.py`。当前两个服务仍使用各自的事件表，尚未接入统一 envelope/consumer。
> 原则：事务 Outbox、至少一次、消费者幂等、版本可演进、大型内容用引用。

---

## 1. Envelope

```json
{
  "event_id": "uuid",
  "event_type": "KernelTaskLeased",
  "event_version": 1,
  "tenant_id": "uuid",
  "workspace_id": "uuid",
  "project_id": "uuid|null",
  "aggregate_type": "kernel_task",
  "aggregate_id": "uuid",
  "aggregate_version": 12,
  "occurred_at": "2026-10-31T22:00:00+00:00",
  "correlation_id": "corr_...",
  "causation_id": "uuid|null",
  "producer": "veynod-kernel",
  "payload_ref": "doc://...|null",
  "payload": {}
}
```

字段规则：

| 字段 | 规则 |
|---|---|
| `event_id` | 全局唯一 UUID；消费者按它去重 |
| `event_type` | 必须在 `EVENT_TYPES` 注册 |
| `event_version` | 从 1 开始；破坏性变更升版本 |
| `tenant/workspace` | 必填 UUID；跨租户事件拒绝 |
| `project_id` | 可选；Phase 1 尚无 Project 实体 |
| `aggregate_type` | product_task/agent_run/workflow/kernel_task/submission/merge/delivery |
| `aggregate_id` | 聚合根 ID |
| `aggregate_version` | 单调递增；乱序事件不得覆盖新状态 |
| `occurred_at` | ISO-8601 UTC |
| `correlation_id` | 贯穿请求→任务→提交→commit→delivery |
| `causation_id` | 直接触发该事件的事件 ID |
| `producer` | agentgraph/agent-runtime/veynod-kernel |
| `payload_ref` | 大内容引用；`payload` 保持小 |
| `payload` | 业务字段；不超过 64KB |

---

## 2. 事件类型

```text
ProductTaskCreated / ProductTaskCancelled
AgentRunCreated / AgentRunStarted / AgentRunPaused / AgentRunResumed
AgentRunCompleted / AgentRunFailed / AgentRunCancelled
PlanCreated / PlanRevised / TaskDelegated
KernelTaskCreated / KernelTaskLeased / KernelTaskBlocked
KernelTaskCompleted / KernelTaskFailed / KernelTaskCancelled
PatchSubmitted / GateStarted / GatePassed / GateFailed
MergeStarted / MergeCommitted / MergeConflict
DeliveryReady / DeliveryApproved
```

当前实现映射：

| 目标事件 | 当前来源 | 状态 |
|---|---|---|
| ProductTask* | AgentGraph `workspace_events` | PARTIAL（类型名不同） |
| AgentRun* | 无 | MISSING |
| Plan* | 无 | MISSING |
| KernelTask* | Kernel `kernel_events`（TASK_LEASED 等）；Phase 1B 新增 `TASK_CANCELLED` | PARTIAL |
| PatchSubmitted | Kernel `patch_submissions`（无事件） | PARTIAL |
| GatePassed/Failed | Kernel `gate_results`（无独立事件） | PARTIAL |
| Merge* | Kernel `merge_intents`（无事件） | PARTIAL |
| Delivery* | 无 | MISSING |

---

## 3. 投递语义

- **至少一次**；不承诺 exactly-once；
- 生产者在业务事务内写 Outbox；
- Dispatcher 重试/退避；消费者维护已处理 `event_id`；
- 消费者按 `aggregate_version` 拒绝旧事件；
- `LISTEN/NOTIFY` 只做唤醒，不作为唯一持久化；
- 大 Artifact 用 `payload_ref`，不复制进事件；
- 死信可查询/重放；Schema 可版本演进。

当前状态：

| 能力 | AgentGraph | Kernel |
|---|---|---|
| 事务 outbox | `workspace_events` trigger | `event_outbox` + `kernel_events` |
| 至少一次 | 是（poll） | 是（DB cursor） |
| 消费者幂等 | 部分（event_id/seq） | 无跨域消费者 |
| 乱序保护 | 部分（seq） | 有 event_seq，无 aggregate_version |
| DLQ | 无（7 天 retention） | 无 |
| correlation/causation | 无 | 无 |
| SSE 身份 | `row.id` 与 `seq` 不一致 | `event_seq` |

---

## 4. 生产者/消费者

```text
AgentGraph transaction
  -> workspace_events (trigger)
  -> SSE / poll / inbox / webhook

Kernel transaction
  -> kernel_events + event_outbox
  -> pg_notify (current: no LISTEN consumer)
  -> EventHub poll -> SSE

Target:
  Kernel Outbox -> Dispatcher -> Agent Runtime Inbox / AgentGraph Projection
```

Phase 3 目标：Kernel 的 `KernelTaskCompleted/Failed`、`PatchSubmitted`、`Gate*`、`Merge*` 投影到 AgentGraph；AgentGraph 的取消/审批投影到 Runtime/Kernel。**当前没有这条链路。**

### 4.1 Phase 1I interim

AgentGraph 已通过 `/v1/dag/events` + `code_projection_cursors` 做 2 秒轮询投影：

- 游标按 Kernel workspace 持久化；workspace 从 code task/游标自动发现；
- 只处理带 `task_id` 的事件；
- 任务扫描每 30 秒兜底；
- 这不是 push/consumer，Kernel Outbox 仍未直连 AgentGraph。

---

## 5. 示例

### 5.1 KernelTaskLeased

```json
{
  "event_id": "3f...",
  "event_type": "KernelTaskLeased",
  "event_version": 1,
  "tenant_id": "t-uuid",
  "workspace_id": "w-uuid",
  "project_id": null,
  "aggregate_type": "kernel_task",
  "aggregate_id": "task-uuid",
  "aggregate_version": 4,
  "occurred_at": "2026-10-31T22:00:00+00:00",
  "correlation_id": "corr_abc",
  "causation_id": null,
  "producer": "veynod-kernel",
  "payload_ref": null,
  "payload": {"runner_id": "runner-1", "lease_expires_at": "..."}
}
```

### 5.2 GatePassed

```json
{
  "event_type": "GatePassed",
  "aggregate_type": "submission",
  "aggregate_id": "submission-uuid",
  "payload": {
    "task_id": "task-uuid",
    "submission_id": "submission-uuid",
    "commit_sha": "40-hex",
    "stages": ["WRITE_SET_BOUNDARY", "AST_SCOPE_DIFF", "CONTRACT_HASH_GUARD", "STATIC_TYPECHECK", "UNIT_TEST_SUITE"]
  }
}
```

---

## 6. Schema 演进

- 新增可选字段：不升 `event_version`；
- 删除/重命名字段、改变语义：升 `event_version`；
- 消费者必须忽略未知字段；
- 事件类型只增不改；
- 所有版本保留至少一个迁移窗口。

---

## 7. Phase 1A 状态

已完成：

- `contracts/events.py` 定义 envelope、类型、producer、校验；
- 5 个契约测试通过。

未完成：

- 两个服务未使用该 envelope 生产/消费；
- 没有 correlation_id 贯穿；
- 没有 DLQ/重放；
- 没有 AgentGraph/Kernel 投影消费者；
- SSE `id`/`seq` 仍不一致。

这些属于 Phase 1B/Phase 3，不得把本文件当作已实现。
