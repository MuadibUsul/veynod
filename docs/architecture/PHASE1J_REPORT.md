# PHASE1J_REPORT — 多 Kernel Workspace 事件游标

> 日期：2026-10-31
> 范围：Phase 1J：自动发现 code task 的所有 Kernel workspace，并分别维护事件游标
> 状态：源码 + 单测通过；AgentGraph 镜像已重建；线上两个 workspace 均在轮询

---

## 1. 本次完成

### 1.1 自动发现 Kernel workspace

`AgentGraph/app/services/code_engine.py`：

- `_kernel_workspace_ids()` 从两处发现：
  - `collab_tasks.input_context.kernel.workspace_id`（所有 code task）；
  - `code_projection_cursors` 已有游标；
  - 加上配置的 `KERNEL_WORKSPACE_ID`。
- `merge_kernel_workspace_ids()` 去重/排序，单测覆盖。

### 1.2 每 workspace 独立游标

- 原 `project_code_events_once` 拆成：
  - `project_code_events_once`：发现所有 workspace，逐个处理；
  - `_project_one_kernel_workspace`：一个 workspace 的 cursor/events/task 投影。
- 聚合统计：`workspaces`、`events`、`tasks`、`synced`、`cancel/fail/commit`、`skipped`、`errors`、`cursors`。
- 一个 workspace 失败不影响其他 workspace。

### 1.3 线上验证

AgentGraph 重建后：

```text
code_projection_cursors
0ae92b98-f687-461f-ae03-c5941dc31db8 | 16
94b60104-cdc5-4f1c-b389-2d389a5410ce | 14
```

日志同时出现：

```text
GET /v1/dag/events?workspace_id=0ae...&after_seq=16 200
GET /v1/dag/events?workspace_id=94b...&after_seq=14 200
```

说明旧的 `platform-runner` 代码任务所在的 Kernel workspace 也被纳入投影。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| AgentGraph 非集成 | **78 passed, 28 skipped** |
| Kernel 非集成 | **98 passed** |
| contracts | **5 passed** |
| 线上 | 两个 cursor 初始化；两个 workspace 事件轮询 200 |

新增单测：`merge_kernel_workspace_ids` 去重/空值。

> 没有真实“事件 → 任务状态”变化的自动化集成测试；线上只验证了轮询和 cursor。

---

## 3. 限制

1. 仍是 HTTP 轮询（2s），不是 push/consumer。
2. 多 workspace 只解决投影；`create_run` 仍只用一个 `KERNEL_WORKSPACE_ID`。
3. 事件里没有 AgentGraph task key，仍需 JSONB lookup。
4. 任务扫描兜底仍是全表扫描。
5. 没有事件重放/DLQ；Kernel `kernel_events` 无 retention。
6. 未提交 git。

---

## 4. 下一步（Phase 1K / Phase 2）

1. Kernel Outbox → AgentGraph push/consumer；
2. 统一 DagTaskCommandService 其余命令；
3. 多 workspace 创建/路由（Repository Registry）；
4. 1,000 Runner 容量复测；
5. Phase 2 Durable Agent Runtime。

---

## 5. 结论

事件投影不再只认一个 Kernel workspace：AgentGraph 会发现所有 code task 的 workspace，并为每个 workspace 维护独立 cursor；线上两个 workspace 已在同时轮询。**但仍是 2 秒 HTTP 轮询，不是事件总线 push；创建入口仍单 workspace。**
