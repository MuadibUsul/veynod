# PHASE1E_REPORT — AgentGraph 取消/失败投影（按需）

> 日期：2026-10-31
> 范围：Phase 1E 第一批：Kernel CANCELLED/FAILED 投影到 AgentGraph
> 状态：源码 + 单测通过；**按需投影（GET/sync 时触发）**，还不是后台事件消费者；AgentGraph 镜像未重建

---

## 1. 本次完成

### 1.1 投影动作映射

`AgentGraph/app/services/code_engine.py` 新增：

```python
lifecycle_action(kernel_status, task_status) -> "cancel" | "fail" | "commit" | None
```

规则：

- 只对 `TODO`/`READY`/`IN_PROGRESS` 的产品任务投影；
- Kernel `CANCELLED` → `cancel`
- Kernel `FAILED` → `fail`
- Kernel `COMMITTED` → `commit`
- 已在 `REVIEWING`/`HANDOFF`/`AWAITING_HUMAN`/`DONE` 的任务不降级/不覆盖；
- Kernel 非终态 → 不动作。

### 1.2 `_sync_lifecycle` 处理取消/失败

- Kernel `CANCELLED`：
  - 调用 `task_service.cancel_task(..., propagate_to_kernel=False)`；
  - 因为 Kernel 已经是 CANCELLED，避免再次调用 Kernel；
  - 产品任务 → `CANCELLED`，审计/lease/子任务按现有 cancel 逻辑处理。
- Kernel `FAILED`：
  - 调用新增的 `task_service.fail_task`；
  - 产品任务 → `FAILED`，释放 lease，取消子任务，写审计；
  - 不调用 Kernel。
- Kernel `COMMITTED`：仍走真实 gate evidence 路径；缺少证据不写 PASS。

### 1.3 `fail_task` 命令

`AgentGraph/app/services/tasks.py` 新增：

- 只允许 `task:transition` 权限；
- 行锁 + expected_version；
- 已是 DONE/FAILED/CANCELLED 幂等返回；
- 释放 lease、取消子任务、写 `state_audit_diffs`；
- 状态变更由 DB trigger 产生 `task.status_changed` 事件（不再造未知事件类型）。

### 1.4 返回状态

`get_run` 的 `status` 字段现在在状态变化后返回 `SYNCED`（不再只对 REVIEWING 生效），并保留 `agentgraph_status`。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| AgentGraph 非集成 | **76 passed, 28 skipped** |
| Kernel 非集成 | 95 passed（未改 Kernel） |
| contracts | 5 passed |

新增投影单测覆盖：

- Kernel CANCELLED + READY → cancel；
- Kernel FAILED + READY → fail；
- Kernel COMMITTED + READY → commit；
- Kernel LEASED + READY → 无动作；
- Kernel CANCELLED/FAILED + REVIEWING/DONE → 不覆盖。

> AgentGraph 的真实 DB/Kernel 集成测试仍受 pgvector 缺失阻塞；本轮没有跑。

---

## 3. 这是“按需投影”，不是“事件投影”

当前触发点：

- `GET /v1/code/runs/{task}`；
- `POST /v1/code/runs/{task}/sync`。

没有：

- 后台 projection worker；
- Kernel outbox → AgentGraph consumer；
- 取消事件自动推送。

因此如果没人 GET，产品任务仍可能停在 READY。要真正自动，需要 Phase 3 的 event projection。

---

## 4. 尚未完成

1. 后台/事件投影（Kernel Outbox -> AgentGraph projection）；
2. in-flight sandbox 主动终止；
3. 生产禁用未绑定 legacy token；
4. token revoke/rotate 的独立 admin audit 事件；
5. AgentGraph 镜像重建 + 真实端到端测试；
6. 统一 DagTaskCommandService 其余命令；
7. 没有 commit。

---

## 5. 下一步（Phase 1F / Phase 3）

1. 把投影做成后台循环或事件消费者；
2. gate worker 取消轮询 + 杀死本地 pytest 进程树；
3. 生产 `VN_REQUIRE_BOUND_TOKENS=true`；
4. AgentGraph 镜像重建 + 端到端 code run（含取消/失败/证据）；
5. 统一任务命令层。

---

## 6. 结论

AgentGraph 现在在主动读取 code run 时，会把 Kernel 的 CANCELLED/FAILED 投影成产品任务的 CANCELLED/FAILED，并且不会覆盖已进入 REVIEWING 的任务。**但这不是自动事件投影，且运行镜像未更新。**
