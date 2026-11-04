# PHASE2C_REPORT — Runtime 版本化迁移与内部 API

> 日期：2026-10-31
> 范围：Phase 2C 第一批：版本化 migration + Runtime FastAPI + 内部认证
> 状态：测试通过；内部服务已在 :8002 运行；未接 AgentGraph

---

## 1. 本次完成

### 1.1 版本化迁移

`agent_runtime/migrations/`：

- `001_init.sql`：agent_runs / agent_steps / run_checkpoints / run_events / runtime_outbox / runtime_idempotency；
- `002_invocations_usage.sql`：agent_runs.usage、invocations、索引。

`RunStore.init_schema()`：

- 创建 schema + `schema_migrations`；
- 按文件名顺序应用未记录的迁移；
- 每个版本记录到 `schema_migrations`；
- `reset=True` 仅用于测试/开发。

### 1.2 Runtime API

`agent_runtime/api.py`：

- `POST /v1/runs` 创建 Run（支持 budget/idempotency key）；
- `GET /v1/runs/{id}`；
- `POST /v1/runs/{id}/transition`；
- `POST /v1/runs/{id}/pause`（RUNNING→PAUSING→PAUSED）；
- `POST /v1/runs/{id}/resume`（PAUSED→RESUMING→RUNNING）；
- `POST /v1/runs/{id}/cancel`；
- `POST /v1/runs/{id}/checkpoint`；
- `GET /v1/runs/{id}/checkpoints`；
- `GET /v1/runs/{id}/events`；
- `GET /healthz`。

认证：`X-Runtime-Key`（内部服务密钥）；未授权 401。

### 1.3 线上内部服务

已启动：

```text
http://127.0.0.1:8002
RUNTIME_DATABASE_URL=veynod_dev
RUNTIME_API_KEY=dev-runtime（开发）
```

验证：

- `/healthz` 200；
- 未授权 `/v1/runs/...` 401；
- `agent_runtime` schema 已创建；
- `schema_migrations` 有 `001_init`、`002_invocations_usage`。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| agent_runtime（store/loop/API） | **13 passed**（真实 PostgreSQL） |

新增 API 测试覆盖：

- 认证 401/200；
- create → 完整状态链；
- pause/resume；
- checkpoint + list + events；
- cancel；
- 非法转换 409。

---

## 3. 限制

1. 没有 Run Worker/claim/lease/heartbeat；API 只管理状态，不自动执行 `LoopController`。
2. 没有生产 Model Gateway/Tool Runtime 适配器。
3. `RUNTIME_API_KEY` 默认 `runtime-dev`，生产必须换强 key；没有启动时生产校验。
4. API 只监听本机；未提供 compose/K8s 部署。
5. 没有 Context Engine、子 Agent、人工审批。
6. 没有接 AgentGraph；产品任务不知道 Agent Run。
7. 没有 AgentGraph/Runtime 投影和端到端测试。
8. 未提交 git。

---

## 4. 下一步（Phase 2D）

1. Runtime Worker：claim run/step + lease/heartbeat/fencing；
2. 生产 Model Gateway（OpenAI/兼容）与 Tool Runtime（allowlist/超时/幂等）；
3. Context 组装 + 子 Agent 委派；
4. AgentGraph 创建 Product Task 时创建 Agent Run，并投影状态；
5. Run API 到 AgentGraph 的认证/授权边界；
6. 崩溃/重启/重复工具调用集成测试；
7. 生产 key 校验与部署。

---

## 5. 结论

Agent Runtime 现在有版本化 schema 和内部 API，能创建/暂停/恢复/取消/写 checkpoint；它还没有 Worker 去自动执行 Loop，也没有生产模型/工具适配器和产品接入。
