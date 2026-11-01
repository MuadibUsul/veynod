# PHASE2J_REPORT — Provider 路由、租户配额、后台对账

> 日期：2026-10-31
> 范围：Phase 2J 第一批：RoutingModelGateway、QuotaPolicy、ReconciliationLoop
> 状态：库 + 真实 PostgreSQL 测试通过；未启动常驻 Worker

---

## 1. 本次完成

### 1.1 Provider 路由

`agent_runtime/router.py`：

- `ModelRoute(name, gateway, priority)`；
- `RoutingModelGateway`：
  - 按 `priority` 排序；
  - Run budget 的 `model_provider` 优先；
  - `allowed_providers` 限制可用 route；
  - 某个 provider 失败 → 尝试下一个；
  - 全部失败 → `ModelGatewayError`。

### 1.2 租户配额

- `RunStore.tenant_usage(tenant_id)`：汇总该 tenant 所有 Run 的 model_calls/tokens/cost。
- `QuotaPolicy`：
  - `max_model_calls` / `max_prompt_tokens` / `max_completion_tokens` / `max_cost_usd`；
  - `from_env()` 读 `RUNTIME_TENANT_*` 环境变量。
- `LoopController` 在模型调用前检查 tenant usage；超限 → `BUDGET_EXCEEDED`，不调用模型。
- `RuntimeWorker` / `RuntimeWorkerPool` / `worker_main` 传递 quota。

### 1.3 后台委派对账

`agent_runtime/maintenance.py`：

- `ReconciliationLoop.run_once()` 调 `DelegationManager.reconcile_all()`；
- `run_forever()` 默认 30s（`RUNTIME_RECONCILE_INTERVAL_SECONDS`）；
- `worker_main` 启动该 loop，stop 时取消。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| agent_runtime | **28 passed**（真实 PostgreSQL + MockTransport） |

新增覆盖：

- tenant 已用 2 次 model call，quota=1 → 第二次 Run 直接 BUDGET_EXCEEDED，模型 0 调用；
- Router 首选 provider 失败 → fallback 到下一个；指定 `model_provider` 优先；
- ReconciliationLoop 恢复无 child 的 WAITING_CHILDREN 父 Run；
- 原有 store/loop/API/worker/pool/context/delegation 测试继续通过。

---

## 3. 限制

1. Provider 路由没有健康检查/动态权重/成本表；只按 priority/preferred/allowed。
2. 租户配额是单库聚合，没有分布式 quota service；没有配额管理 API。
3. ReconciliationLoop 只在 `worker_main` 中运行；API-only 部署没有对账。
4. 没有 OS 进程级 supervisor/metrics。
5. 没有接 AgentGraph。
6. 未提交 git。

---

## 4. 下一步（Phase 2K）

1. Provider 健康/成本/配额管理 API；
2. 进程级 supervisor/metrics；
3. AgentGraph 创建 Agent Run + 投影；
4. 真实模型端到端；
5. 多租户/多 workspace 边界。

---

## 5. 结论

Runtime 现在有 provider fallback、tenant 配额前置检查和后台委派对账；但没有 provider 健康/成本管理、OS supervisor 和产品接入。
