# PHASE 3 REPORT — 统一任务生命周期与事件投影

日期：2026-11-01
范围：Kernel / Agent Runtime / AgentGraph 的持久事件投递、重试、死信与重放。

---

## 1. 交付

### 1.1 AgentGraph 持久事件 Inbox

- 迁移 `v3_70_event_inbox.sql`：
  - `event_inbox(source, source_instance, source_seq, event_type, workspace_id, payload, status, attempts, next_attempt_at, last_error, processed_at, dead_lettered_at)`；
  - 唯一键 `(source, source_instance, source_seq)` 保证重复投递幂等；
  - `runtime_projection_cursors` 保存 Runtime 全局事件游标（Kernel 仍用原有 `code_projection_cursors`）。
- `app/services/event_dispatch.py`：
  - `ingest_kernel_once`：按 Kernel workspace 增量拉取事件，先入 Inbox，再推进游标；
  - `ingest_runtime_once`：按 Runtime base_url 增量拉取 `/v1/events`，入 Inbox；
  - `dispatch_once`：`PENDING` 事件按 `next_attempt_at` 领取，成功 `PROCESSED`；失败指数退避，5 次后 `DEAD`；
  - 事件处理：Kernel task → `sync_kernel_task_event`；Runtime run → `sync_runtime_run_event`；
  - 后台循环 `event_dispatch_loop` 替代原来的直接事件投影循环（扫描兜底仍保留）。
- 运营接口（admin token）：
  - `GET /v1/admin/event-inbox/stats`
  - `GET /v1/admin/event-inbox?status=&source=`
  - `POST /v1/admin/event-inbox/{id}/replay`
  - `POST /v1/admin/event-inbox/dispatch`
- Agent Runtime 新增：
  - `GET /v1/events/head`
  - `GET /v1/events?after_seq=&limit=`
  - `RunStore.list_events_global()` / `latest_event_seq()`
  - AgentGraph 客户端 `events()` / `latest_event_seq()`。

### 1.2 Kernel 事件 envelope 与 Outbox 死信

- 迁移 `v4_011_event_delivery.sql`：
  - `kernel_events` 增加 `event_id`、`event_version`、`producer`、`correlation_id`、`causation_id`；
  - `event_outbox` 增加 `available_at`、`dead_lettered_at`、`last_error`；
  - `EventBus.publish(..., correlation_id, causation_id, producer)`；
  - `/v1/dag/events` 与 SSE 帧返回 envelope 字段；
  - Outbox 超大 NOTIFY payload 不再阻塞队列（记录 `last_error`，SSE 仍从游标读取）。
- 运营接口：
  - `GET /v1/admin/event-outbox/stats`
  - `GET /v1/admin/event-outbox/dead-letter`

### 1.3 失败/重试闭环

- Kernel/Runtime 终态事件仍通过现有 `_sync_lifecycle` / `get_task_run` 投影；
- 投影失败不再只写日志：Inbox 保留事件、重试、死信、可重放；
- Runtime usage 在 Run 终态时幂等写入 `runtime_usage_metered` 与 `usage_ledger`。

---

## 2. 验证

| 验证 | 结果 |
|---|---|
| AgentGraph 集成：projects/limits | passed |
| AgentGraph 集成：Inbox 管理接口 | passed |
| AgentGraph 集成：失败重试→DEAD→replay→PROCESSED | passed |
| AgentGraph 集成：Runtime usage 幂等计量 | passed |
| AgentGraph 非集成 | 80 passed, 28 skipped |
| Kernel 非集成 | passed |
| Kernel 集成（22 tests） | passed |
| 线上真实 Run（DeepSeek） | Run 完成，8 个 Runtime 事件入 Inbox，7 个自动 PROCESSED；1 个旧代码错误进入 DEAD，replay 后成功 |
| 线上计量 | `runtime_usage_metered` 1 行；`usage_ledger category=agent_runtime` 417 tokens |

---

## 3. 限制

1. AgentGraph 仍是**拉取**（1s 间隔），没有 Kernel `pg_notify`/Runtime push 消费者；Kernel 的 `pg_notify` 没有跨服务 LISTEN 客户端。
2. 语义是 at-least-once；投影函数幂等，但没有严格 exactly-once 的事务边界。
3. Inbox 没有自动 retention，需要按 `PROCESSED` 时间清理。
4. `contracts/events.py` 的版本化 envelope 尚未被三个服务的请求路径直接引用；Kernel 新字段是兼容增量。
5. Workflow→DAG 映射仍由现有 diagrams/agent_runtime 的 `workflow_id` 字段承载，没有统一映射服务。
6. 没有跨 API 副本的单写者选主；多个 AgentGraph 副本会同时拉取，唯一键保证不重复入 Inbox，但会重复尝试。

---

## 4. 结论

Phase 3 的核心目标已经落地：事件可持久入 Inbox、失败可重试、死信可重放，Kernel/Runtime/AgentGraph 的终态投影不再依赖“下一次碰巧扫描”。它仍是轮询、至少一次投递，不是完整的分布式事件总线。
