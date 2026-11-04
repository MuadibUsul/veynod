# PHASE2D_REPORT — Runtime Worker 与租约

> 日期：2026-10-31
> 范围：Phase 2D 第一批：Run 租约/心跳/Fencing + Worker 自动执行一步
> 状态：库 + 真实 PostgreSQL 测试通过；**未部署 Worker 进程，未接生产 Model/Tool**

---

## 1. 本次完成

### 1.1 迁移 003

`agent_runtime/migrations/003_run_leases.sql`：

- `agent_runs.lease_owner`
- `agent_runs.lease_token_hash`
- `agent_runs.lease_expires_at`
- `agent_runs.heartbeat_at`
- claimable 索引

### 1.2 Store 租约原语

`RunStore`：

- `claim_run(worker_id, lease_seconds)`：
  - 只领取 `RUNNING` 且无 live lease 的 Run；
  - `FOR UPDATE SKIP LOCKED`；
  - 生成随机 token，只存 SHA-256；
- `assert_lease(run_id, token)`：非终态 + token + 未过期；
- `renew_lease`：心跳续租；
- `release_lease`：释放。

### 1.3 RuntimeWorker

`agent_runtime/worker.py`：

- 每次 claim 一个 Run，执行 **一个** `LoopController.step`；
- 后台 heartbeat 每 lease/3 续租；
- `LoopController` 的 guard 在每次写前调用 `assert_lease`；
- 租约丢失抛 `LeaseLost`，Worker 不继续写；
- 步骤结束释放租约，下一次 claim 继续下一步。

### 1.4 修正

- `assert_lease`/`renew_lease` 允许 `WAITING_TOOL` 等非终态（原先只允许 `RUNNING`，导致工具执行前进入 WAITING_TOOL 后误判租约丢失）。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| agent_runtime（store/loop/API/worker） | **15 passed**（真实 PostgreSQL） |

新增覆盖：

- claim 后第二个 worker 不能同时 claim；
- renew / wrong-token `LeaseLost`；
- release 后其他 worker 可 claim；
- Worker 每次 claim 执行一步：tool 步骤后 Run 仍 RUNNING，第二次 claim 执行 finish，最终 COMPLETED；
- 现有 loop/budget/cancel/restart 测试全部继续通过。

---

## 3. 限制

1. 没有生产 Model Gateway / Tool Runtime；Worker 只能配测试适配器。
2. 没有 Worker 进程 supervisor/部署；API 在 8002，Worker 未常驻。
3. 没有 lease 过期/双 worker 竞争的集成故障测试（只有 claim 互斥）。
4. `PENDING` invocation 崩溃窗口仍依赖工具自身按 idempotency key 去重。
5. 没有 Context Engine、子 Agent、人工审批。
6. 没有接 AgentGraph。
7. 未提交 git。

---

## 4. 下一步（Phase 2E）

1. 生产 Model Gateway/Tool Runtime 适配器（allowlist/超时/幂等）；
2. Worker 进程 + supervisor + metrics；
3. lease 过期/双 worker/工具超时故障测试；
4. Context 组装 + 子 Agent 委派；
5. AgentGraph 创建 Run + 投影；
6. 生产 API key/部署。

---

## 5. 结论

Runtime 现在有租约、心跳、fencing 和自动执行一步的 Worker；工具步骤后租约仍有效，第二个 worker 不会同时跑同一 Run。**但没有生产模型/工具适配器，Worker 未部署，产品未接入。**
