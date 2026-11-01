# 横向扩容、准入控制、持久化后台任务与事件 Outbox

> 日期：2026-10-31  
> 迁移：[`sql/v4_005_admission_outbox_jobs.sql`](../sql/v4_005_admission_outbox_jobs.sql)、
> [`sql/v4_006_admission_permits.sql`](../sql/v4_006_admission_permits.sql)、
> [`sql/v4_007_unlogged_admission_permits.sql`](../sql/v4_007_unlogged_admission_permits.sql)、
> [`sql/v4_008_active_lock_indexes.sql`](../sql/v4_008_active_lock_indexes.sql)

---

## 1. 按仓库准入控制

[`app/engine/scheduler.py`](../app/engine/scheduler.py) 在发放任务前统计：

- **流水线深度**：`LEASED`（租约未过期）、`VERIFYING`、`patch_submissions`
  `QUEUED/RUNNING`、`merge_intents` `PENDING/RUNNING` 的任务去重计数；
- **准入许可**：`admission_permits` 中未过期的预留（claim 尚未提交时也能看到）；
- **合并队列**：`merge_intents.status IN ('PENDING','RUNNING')`。

达到以下任一限制就暂停发放：

```text
VN_ADMISSION_GATE_QUEUE_LIMIT=256
VN_ADMISSION_MERGE_QUEUE_LIMIT=128
```

claim 前先尝试获取一个很短的 per-workspace advisory lock（`pg_try_advisory_xact_lock`，
失败立即返回 429，不排队占用数据库连接），插入 `admission_permits` 并提交。
这样 1000 个并发 pull 不会同时看到空队列而把全部任务租走；许可在 claim 成功后
绑定 `task_id`，任务进入 `COMMITTED/FAILED/READY` 时由触发器删除，过期/孤儿许可
在下次准入时清理。重量级的 Rust/SQL claim 不在锁内，因此不会把完整 claim 延迟
串行化。许可数与流水线深度取较大值作为压力，许可过期也不会导致超发。

行为：

- `VN_ADMISSION_MODE=reject`（默认）：`POST /v1/syscalls/pull_task_bundle` 返回 **429**，`Retry-After` header，body：

```json
{"detail":{"error":"admission_control","reason":"...","retry_after_seconds":5,
           "queue_depth":{"gate_queue":256,"leased":200,"permits":256,
                          "submissions":180,"merge_queue":10,"verifying":180}}}
```

- `VN_ADMISSION_MODE=defer`：返回 200 `leased=false, admission_denied=true,
  retry_after_seconds=5, queue_depth={...}`，供旧客户端平滑迁移。
- Node Runner 与 `scripts/bench_swarm.py` 均解析 `Retry-After`/`admission_denied`，
  按建议时间退避；准入拒绝不计入 HTTP 错误或 retry 预算。
- 准入检查在 claim 前执行；不会把已经接受/正在门禁的提交丢掉，只是停止产生新的 VERIFYING/队列积压。


---

## 2. 横向 API 与 Rust 调度核心唯一权威

迁移新增 `scheduler_authorities`（`workspace_id` 主键）。

- 每个 API 进程启动后持有自己的 `authority_id`。
- 每次 claim 尝试获取/续租 workspace authority lease（`VN_SCHEDULER_AUTHORITY_LEASE_SECONDS=30`）。
- **只有 authority holder 使用进程内 Rust `SchedulerCore` 决策**。
- 其他 API 实例自动走 `pg_advisory_xact_lock + GIN + 条件 UPDATE` 的 SQL claim 路径。
- 两条路径最终都由 PostgreSQL 单行条件 UPDATE 作为 durable fence：
  `status='READY'`、`lease_token_hash IS NULL`、无冲突活跃写集。
- `VN_SINGLE_KERNEL_CLAIM=false` 强制所有实例使用 SQL/advisory 路径，适合多进程/多主机。
- Authority lease 过期后另一个实例可接管；旧实例的下一次 claim 不再使用 Rust core，避免双权威。
- API 侧 `scheduler_core` 仍通过 `kernel_events` 同步外部 gate/merge 提交，保证已加载 core 不会永久陈旧。

测试：两个真实 uvicorn 进程共享同一数据库，并发 20 次 pull；10 个任务全部只被租约一次，`scheduler_authorities` 只有一行。

---

## 3. 持久化 Edge Rebuild 队列

原 `asyncio.Queue` 替换为表 `edge_rebuild_jobs`：

- `PENDING / RUNNING / DONE / FAILED`；
- `attempts`、`available_at`、`locked_by`、`lease_expires_at`、`last_error`；
- Gate 合并事务中直接 INSERT（同一事务，避免“合并成功但任务丢失”）；
- 任意 API/worker 进程都可以：
  - `FOR UPDATE SKIP LOCKED` 领取 PENDING 或租约过期的 RUNNING；
  - 失败指数退避，最多 `VN_EDGE_REBUILD_MAX_ATTEMPTS`；
  - 进程停止时把自己的 RUNNING 任务放回 PENDING，重启立即恢复；
  - lease TTL `VN_EDGE_REBUILD_LEASE_SECONDS=120` 防止崩溃任务永久卡住。

`symbol_edges` 仍是最终一致；主路径不读它。

---

## 4. 事务性 Outbox 与 SSE 游标续传

### Outbox

- `kernel_events` 仍是唯一 durable SSE 日志。
- `EventBus.publish` 在同一业务事务中写：
  1. `kernel_events` 行（获得 gapless event_seq）；
  2. `event_outbox` 行（引用 event_seq）。
- 事务回滚时两者一起回滚。
- `OutboxDispatcher` 在提交后执行 `pg_notify` 并标记 `published_at`；可重启、可重试，重复通知最多造成一次多余唤醒，不会丢事件。
- 多 API/worker 进程使用 `FOR UPDATE SKIP LOCKED` 分片发送。

### SSE 续传

- `GET /v1/events/stream?since=<event_seq>` 与 `Last-Event-ID` 均支持。
- EventHub ring 不够时从 PostgreSQL `catch_up`；修复了 ring 为空但 DB 已有事件时的 catch-up 条件。
- 断线重连继续从最后 event_seq 发送，事件不丢。

测试：直接写入 5 条事件，SSE `since=first-1` 收齐全部；另开 **1000 条长连接**，全部 200，无连接错误。

---

## 5. 配置

```bash
VN_ADMISSION_GATE_QUEUE_LIMIT=256
VN_ADMISSION_MERGE_QUEUE_LIMIT=128
VN_ADMISSION_MODE=reject          # reject | defer
VN_ADMISSION_RETRY_AFTER_SECONDS=5
VN_ADMISSION_CLAIM_SLOTS=32        # concurrent reservations; all busy -> 429
VN_SCHEDULER_AUTHORITY_LEASE_SECONDS=30
VN_EDGE_REBUILD_LEASE_SECONDS=120
VN_EDGE_REBUILD_MAX_ATTEMPTS=5
VN_OUTBOX_POLL_SECONDS=0.1
```

---

## 6. 测试与实测

### 关键测试

```bash
TEST_DATABASE_URL=postgresql+asyncpg://.../<fresh-test-db> \
  pytest -q tests/test_integration.py tests/test_admission.py \
           tests/test_merge_coordinator.py tests/test_submission_restart_e2e.py \
           tests/test_work_leases.py tests/test_edge_outbox.py \
           tests/test_horizontal_api.py
```

本轮：**25 passed**（包含两个真实 API 进程竞争、1000 条 SSE、事件重放、edge job 重启恢复、outbox 事务）；`pytest -q -m "not integration"` **79 passed**。

- 多 API：10 个任务并发 20 次 pull，只产生 10 个唯一租约；authority 表 1 行。
- Edge job：DONE；模拟 RUNNING 过期后新服务重领并 DONE。
- Outbox：`kernel_events` 与 `event_outbox` 同事务写入；dispatcher 标记 published。
- SSE：5 条事件从 `since` 完整重放；1000 条长连接全部建立。

### 1000 Runner / 1000 SSE（带准入控制）

见本轮 `.run/bench/v5-1000`、`.run/bench/v9-1000` 结果。准入控制的目标是**限制积压**而不是提升 gate 吞吐：达到 256 流水线/128 合并队列后，新 pull 返回 429 + Retry-After，Runner 退避，不再继续制造无界 VERIFYING。

- 未加许可的 v5-1000：417/1000 COMMITTED，583 非终态；2595 次 429 被旧 harness 误记为 4xx。
- 加许可后的 400/400 对照（8 gate worker、300s 窗口）：**400/400 COMMITTED**，
  0 非终态、0 DB pool 超时、0 HTTP 4xx/5xx；准入拒绝 2595 次全部按 Retry-After 退避。
  仍退出 1：642 次 claim / 53 次 submit 的 TCP 连接失败（`All connection attempts failed`），
  超过 transport/retry 预算。这是单 API + Windows 本地连接层的余量问题，不是任务状态不一致。
- 加许可后的 v9-1000（8 gate worker、1000 Runner/1000 SSE、1200s 窗口）：
  **1000/1000 COMMITTED，0 FAILED，0 非终态，0 DB pool 超时，0 HTTP 4xx/5xx，
  1000 条 SSE 全部建立、3,000,000 帧、0 SSE 错误；准入拒绝 12,772 次全部按 Retry-After 退避**。
  但验收仍为 **false / exit 1**：4,606 次 claim transport/timeout（22.7%）与
  168 次 submit transport（14.4%）超过 0.5% transport/retry 预算。所有任务最终都提交并合并，
  说明准入和持久化路径正确；失败项是单进程 API 在 1000 并发长连接下的**连接层余量**，
  不是 COMMITTED/DB 不一致。

---

## 7. 尚未解决的风险

1. **许可预留与崩溃窗口**：许可在 claim 前提交，初始 1000 并发不会超发；若进程在
   “插入许可”与“绑定 task_id”之间崩溃，该孤儿许可最长保留 2 分钟（TTL 为 lease+300s），
   期间少放行；流水线深度仍作为下限，不会因此超发。许可 TTL 过期后若任务仍在门禁，
   实际流水线深度会继续阻止超发。
2. **Rust core authority 失效窗口**：authority lease 过期到接管之间，旧实例停止使用 core；窗口内所有实例走 SQL/advisory 路径，DB fence 仍正确。
3. **Edge job 仍最终一致**：`symbol_edges` 滞后不影响 6 个 syscall 热路径，但 escalation 在滞后窗口内可能不完整。
4. **Outbox 是 at-least-once 通知**：通知可能重复，SSE 客户端用 event_seq 去重；事件本身只写一次。
5. **1000 SSE 资源**：1000 条连接消耗 socket/内存；本轮测试通过，但生产需 OS backlog/文件描述符配额。
6. **1000 完成率已达标、验收仍失败于连接层**：v9-1000 达到 1000/1000 COMMITTED、
   0 非终态、0 pool timeout、0 HTTP 错误，但单 API 进程在 1000 Runner + 1000 SSE 下
   出现 22.7% claim 与 14.4% submit 的 ConnectTimeout/transport error，超过 0.5% 预算，
   因此 `capacity_accepted=false`。下一步是 API 横向扩容/负载均衡（本阶段已提供唯一
   Rust 权威 + DB fence）或多进程客户端分片；当前 gate 吞吐（约 1.0 commit/s 端到端）
   也不是生产容量结论。
7. **许可槽位并发**：`VN_ADMISSION_CLAIM_SLOTS=32` 允许最多 32 个并发许可插入，
   因此 permit 计数可短暂比上限高 31；流水线深度仍会回收，阈值应留余量。
8. **旧客户端**：`reject` 模式 429 需要客户端支持；可用 `defer` 模式过渡。
