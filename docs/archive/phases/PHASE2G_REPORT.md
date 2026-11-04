# PHASE2G_REPORT — 子 Agent 委派

> 日期：2026-10-31
> 范围：Phase 2G 第一批：parent/child Run 委派、等待与回收
> 状态：库 + API + 真实 PostgreSQL 测试通过；未部署 Worker，未接 AgentGraph

---

## 1. 本次完成

### 1.1 迁移 004

`agent_runtime/migrations/004_delegation.sql`：

- `agent_runs.parent_run_id`（自引用 FK，ON DELETE CASCADE）；
- `(parent_run_id, created_at)` 索引。

### 1.2 Store

- `create_run(..., parent_run_id=...)`；
- `get_run` 返回 `parent_run_id`；
- `list_children(parent_run_id)`。

### 1.3 DelegationManager

`agent_runtime/delegation.py`：

- `delegate(parent_run_id, ...)`：
  - 父必须是 RUNNING；
  - 创建 child Run（继承 tenant/workspace/correlation）；
  - child 推进到 RUNNING；
  - 父 RUNNING → WAITING_CHILDREN；
- `collect(parent_run_id)`：
  - 父不是 WAITING_CHILDREN → `ready=false`；
  - 任一 child 非终态 → `ready=false`；
  - 全部终态 → 父 WAITING_CHILDREN → RUNNING；
  - 返回 children 结果。

### 1.4 Runtime API

新增：

| 方法 | 路径 |
|---|---|
| POST | `/v1/runs/{id}/delegate` |
| GET | `/v1/runs/{id}/children` |
| POST | `/v1/runs/{id}/collect` |

均要求 `X-Runtime-Key`。

### 1.5 线上

- 8002 Runtime API 已重启；
- `agent_runtime.schema_migrations` 已有 `001`–`004`；
- `agent_runs.parent_run_id` 列已存在。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| agent_runtime | **23 passed**（真实 PostgreSQL + MockTransport） |

新增覆盖：

- 父 RUNNING 委派 child → 父 WAITING_CHILDREN，child RUNNING、parent_run_id 正确、继承 tenant/workspace；
- child 未终态时 collect `ready=false`；
- child COMPLETED 后 collect `ready=true`，父回到 RUNNING；
- API delegate/children/collect 全链路；
- 现有 store/loop/API/worker/gateway/context 测试继续通过。

---

## 3. 限制

1. 没有 Provider 路由/配额/成本上限。
2. 没有 Worker supervisor/部署。
3. 委派没有独立的幂等语义（create idempotency 只管 child 创建，父状态转换可能冲突）。
4. child 创建后若父状态变化，child 可能成为孤儿；需要 reconciliation。
5. 没有 Context 的产品文档/代码快照接入。
6. 没有接 AgentGraph。
7. 未提交 git。

---

## 4. 下一步（Phase 2H）

1. Provider 路由/配额/成本；
2. Worker supervisor/metrics；
3. 委派幂等与孤儿 child reconciliation；
4. AgentGraph 创建 Agent Run + 状态投影；
5. 真实模型端到端测试。

---

## 5. 结论

Runtime 现在支持父 Run 委派 child Run、父进入 WAITING_CHILDREN、全部 child 终态后 collect 恢复父 Run；API 已暴露 delegate/children/collect。**但还没有 Provider 配额、Worker supervisor、委派幂等和产品接入。**
