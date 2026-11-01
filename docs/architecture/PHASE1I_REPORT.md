# PHASE1I_REPORT — 事件游标投影与线上接线

> 日期：2026-10-31
> 范围：Phase 1I：Kernel 事件游标 → AgentGraph 增量投影
> 状态：源码 + 单测通过；AgentGraph 镜像已重建并运行；未提交 git

---

## 1. 本次完成

### 1.1 Kernel 暴露最新事件序号

`GET /v1/dag`（admin key）新增：

```json
{"latest_event_seq": 16, ...}
```

AgentGraph 用它初始化游标，避免首次消费全部历史事件。

### 1.2 AgentGraph 事件游标表

迁移 `v3_69_code_projection_cursor.sql`：

```sql
CREATE TABLE code_projection_cursors (
  kernel_workspace_id UUID PRIMARY KEY,
  last_event_seq BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- 每个 Kernel workspace 一个游标；
- 首次没有游标时，从 Kernel `latest_event_seq` 开始；
- 批量处理后用 `GREATEST` 前移，不回退。

### 1.3 事件投影循环

`AgentGraph/app/services/code_engine.py`：

- `KernelEngineClient.events()` → `/v1/dag/events`；
- `event_task_ids()`：从事件 payload 提取唯一 task_id；
- `project_code_events_once()`：
  - 读游标；
  - 拉取 `after_seq` 之后的事件；
  - 对每个 task_id 查 AgentGraph code task；
  - 复用 `lifecycle_action` / `_sync_lifecycle` 投影 CANCELLED/FAILED/COMMITTED；
  - 推进游标；
- `code_projection_event_loop()`：默认每 2 秒消费一次；
- 与任务扫描共用 `_PROJECTION_LOCK`，避免两个循环同时写同一任务。

### 1.4 双轨保障

- 事件循环：2s，增量；
- 任务扫描循环：30s，兜底（事件丢失/游标异常时仍能同步）。

### 1.5 线上配置修复

发现 AgentGraph 容器的 `KERNEL_WORKSPACE_ID` 为空，导致投影不会启动。已在 `AgentGraph/.env` 写入当前 Kernel workspace：

```text
KERNEL_WORKSPACE_ID=0ae92b98-f687-461f-ae03-c5941dc31db8
```

重建并重启后：

- `code_projection_cursors` 出现 `0ae92b98... | 16`；
- 日志显示 `/v1/dag` 与 `/v1/dag/events?after_seq=16` 正常轮询；
- Kernel 重启期间出现一次 `httpx.ConnectError`，循环捕获后恢复，未中断。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| AgentGraph 非集成 | **77 passed, 28 skipped** |
| Kernel 非集成 | **98 passed** |
| contracts | **5 passed** |
| 线上 | health 200、Code 页面 200、cursor 已初始化、事件轮询 200 |

新增单测：`event_task_ids` 去重/跳过无 task_id 的事件。

> 没有自动化“真实 Kernel 事件 → AgentGraph 投影”的集成测试；本轮用线上事件轮询做了 smoke。

---

## 3. 已知限制

1. 仍是 HTTP 轮询（2s），不是 Kernel Outbox push/webhook。
2. 只支持一个 `KERNEL_WORKSPACE_ID`；多 Kernel workspace 需要多游标/多实例。
3. 事件里没有 AgentGraph task key，仍需按 `input_context.kernel.task_id` 查询。
4. 任务扫描兜底仍全表扫描（每 30s，最多 100）。
5. Kernel 短暂不可用会打印一次异常栈（循环继续），日志较吵。
6. 未提交 git。

---

## 4. 下一步（Phase 1J / Phase 2）

1. Kernel Outbox → AgentGraph webhook/consumer（真正 push）；
2. 多 Kernel workspace 游标与 Repository Registry；
3. 统一 DagTaskCommandService；
4. 1,000 Runner 容量复测；
5. Phase 2 Durable Agent Runtime。

---

## 5. 结论

AgentGraph 现在按 Kernel 事件游标增量投影，不再只靠全表扫描；线上已初始化 cursor 并正常轮询。**但这是 2 秒 HTTP 轮询、单 Kernel workspace，不是完整事件总线。**
