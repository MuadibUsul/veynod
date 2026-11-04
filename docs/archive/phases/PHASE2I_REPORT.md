# PHASE2I_REPORT — 委派幂等与孤儿回收

> 日期：2026-10-31
> 范围：Phase 2I 第一批：delegation_key 幂等 + WAITING_CHILDREN reconciliation
> 状态：真实 PostgreSQL 测试通过；8002 已应用迁移 005

---

## 1. 本次完成

### 1.1 迁移 005

`agent_runtime/migrations/005_delegation_key.sql`：

- `agent_runs.delegation_key TEXT`；
- 唯一索引 `(parent_run_id, delegation_key) WHERE delegation_key IS NOT NULL`。

### 1.2 委派幂等

`DelegationManager.delegate(..., idempotency_key=...)`：

- 先按 `(parent_run_id, delegation_key)` 查已有 child；
- 已存在 → 返回原 child + children，`idempotent=true`，不重复创建/不重复转换父状态；
- 新建时把 key 写入 child 的 `delegation_key`；
- create 的 idempotency key 使用 `delegate:{parent}:{key}`。

### 1.3 孤儿回收

- `reconcile(parent_run_id)`：
  - 父不是 WAITING_CHILDREN → `ready=false`；
  - 没有 children → 父回到 RUNNING；
  - children 全部终态 → 复用 `collect` 恢复父；
  - 仍有非终态 → `ready=false`。
- `reconcile_all()`：扫描所有 WAITING_CHILDREN 父 Run，逐个 reconcile。
- API：`POST /v1/delegations/reconcile`。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| agent_runtime | **25 passed**（真实 PostgreSQL + MockTransport） |

新增覆盖：

- 同一 delegation key 重复 delegate → 返回同一 child，`idempotent=true`；
- WAITING_CHILDREN 但无 child 的父 Run → reconcile 回 RUNNING；
- 原有 delegation/API/worker/pool/context/loop 测试继续通过。

---

## 3. 限制

1. 没有 Provider 路由/配额/成本上限。
2. 没有 OS 进程级 supervisor/metrics。
3. `reconcile_all` 没有后台定时器；目前只能手动/API 调用。
4. delegation key 只保证同一父 Run 下唯一；跨父不冲突。
5. 没有接 AgentGraph。
6. 未提交 git。

---

## 4. 下一步（Phase 2J）

1. Provider 路由/配额/成本；
2. 进程级 supervisor/metrics；
3. reconcile_all 后台循环；
4. AgentGraph 创建 Agent Run + 投影；
5. 真实模型端到端测试。

---

## 5. 结论

委派现在可重试且不会重复创建 child；WAITING_CHILDREN 父 Run 有手动/接口级孤儿回收。**Provider 配额、进程 supervisor、后台 reconciliation 和产品接入仍未完成。**
