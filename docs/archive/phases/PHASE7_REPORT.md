# PHASE 7 REPORT — 产品体验与开发者平台

日期：2026-11-01
范围：Project 工作区、Execution Graph、Delivery View、Runtime Observability、计量与租户限额、CLI。

---

## 1. 交付

### 1.1 数据模型（`v3_71_projects_metering.sql`）

- `projects(project_id, workspace_id, name, repo, description)`。
- `runtime_usage_metered(run_id PK, workspace_id, tenant_id, model_calls, prompt_tokens, completion_tokens, cost_usd)`：每个 Runtime Run 只计量一次。
- `tenant_runtime_limits(tenant_id, max_model_calls, max_cost_usd)`：产品层租户限额。

### 1.2 API

| 路径 | 作用 |
|---|---|
| `GET/POST /v1/projects` | 项目列表/创建或更新 |
| `GET /v1/execution-graph?project=` | 任务节点、parent 边、状态汇总 |
| `GET /v1/projects/{name}/execution` | 项目执行图 |
| `GET /v1/deliveries` | Kernel delivery record 列表 |
| `GET /v1/projects/{name}/deliveries` | 项目交付列表 |
| `GET /v1/runtime/observability` | Agent Runs + Provider health |
| `GET /v1/usage/runtime` | Runtime 计量汇总 |
| `GET/PUT /v1/tenant/runtime-limit` | 租户模型调用/成本限额 |

### 1.3 UI

- 新页面 `/dashboard/projects/`（静态模板 `app/templates/projects.html`）：
  - Projects 列表 + 创建；
  - Execution Graph 表格；
  - Deliveries；
  - Runtime Observability / usage / provider health / tenant limit。
- 侧边栏新增 “Projects & Delivery”。
- 现有 `/dashboard/agent-runs/` 保留。

### 1.4 CLI

`python -m app.cli` 新增：

```text
projects    --workspace-id
execution   --workspace-id [--project]
usage       --workspace-id
deliveries  --workspace-id [--project]
```

### 1.5 计量

- Runtime Run 终态时，`meter_runtime_run()`：
  - 写 `runtime_usage_metered`（幂等）；
  - 写 `usage_ledger category=agent_runtime`，amount=prompt+completion tokens，cost 由 Runtime 返回或回退价格计算。
- `PUT /v1/tenant/runtime-limit` 设置的限额会在 AgentGraph 创建 Run 前检查；Runtime 自身的 `QuotaPolicy` 仍是第二层。

---

## 2. 验证

| 验证 | 结果 |
|---|---|
| AgentGraph 集成：project 创建/列表 | passed |
| AgentGraph 集成：execution graph 包含任务 | passed |
| AgentGraph 集成：deliveries 空列表、usage 初始 0 | passed |
| AgentGraph 集成：tenant limit PUT/GET | passed |
| AgentGraph 集成：metering 幂等（第二次返回 False） | passed |
| 线上页面 `/dashboard/projects/` | 200 |
| 线上 CLI 代码路径 | 已接入 services.projects |

---

## 3. 限制

1. UI 是 FastAPI 静态模板，不是 Next.js 原生页面；没有复杂的拖拽/画图交互。
2. 没有 Project 级别的详情页/成员权限；project 只是任务分组。
3. Execution Graph 的边只有 `parent_task_id`，没有展示 Kernel DAG 的 `task_dependencies`。
4. Deliveries 需要对每个 code task 调用 Kernel，列表上限 200；没有本地缓存/分页游标。
5. 计量成本默认 0，除非 Runtime 返回 `cost_usd` 或设置 `RUNTIME_*_USD_PER_1K`；没有价格表/账单/发票。
6. CLI 是 Python 运维 CLI，不是 npm 客户端的完整项目/交付命令。
7. 没有 SDK 生成、用量导出、企业权限模型扩展。

---

## 4. 结论

Phase 7 提供了一个可用的产品面：项目、执行图、交付、Runtime 观测、用量与租户限额都有 API 和页面/CLI 入口；计量是真实幂等的。它仍是轻量控制台，不是完整的计费/企业平台。
