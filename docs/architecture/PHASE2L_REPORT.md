# PHASE2L_REPORT — AgentGraph → Runtime 接入

> 日期：2026-10-31
> 范围：Phase 2L 第一批：产品任务绑定 Agent Run、读取/投影、取消
> 状态：源码 + 单测通过；AgentGraph/Runtime 镜像已重建/重启；未跑真实 Worker/模型端到端

---

## 1. 本次完成

### 1.1 Runtime `/start`

`agent_runtime`：

- `RunStore.start_run()`：CREATED → QUEUED → INITIALIZING → PLANNING → READY → RUNNING；
- API `POST /v1/runs/{id}/start`；
- 线上 smoke：create 201 → start 200 RUNNING → get RUNNING → cancel CANCELLED。

### 1.2 AgentGraph 配置

`.env` 增加：

```text
AGENT_RUNTIME_URL=http://host.docker.internal:8002
AGENT_RUNTIME_KEY=dev-runtime
```

`app/config.py` 增加同名配置。

### 1.3 AgentGraph 服务

`AgentGraph/app/services/agent_runtime.py`：

- `AgentRuntimeClient`：
  - create run / start / get / cancel；
  - `X-Runtime-Key`；
  - 支持 httpx.MockTransport（测试）。
- `create_task_run(session, principal, task_key, budget)`：
  - 要求 `task:transition`；
  - 产品任务终态拒绝；
  - 已有 `input_context.agent_runtime.run_id` → 幂等返回；
  - 创建 Runtime Run 并 start；
  - 把 `run_id/base_url` 写入产品任务 `input_context`；
  - 写 audit + `TASK_UPDATED` 事件。
- `get_task_run`：
  - 读 Runtime 状态；
  - `runtime_status_action`：
    - COMPLETED + READY → claim + handoff REVIEWING；
    - FAILED → `fail_task`；
    - CANCELLED → `cancel_task(propagate_to_kernel=False)`；
    - REVIEWING/DONE 等不覆盖；
  - 返回 runtime + agentgraph 状态 + `projected`。
- `cancel_task_run`：取消 Runtime Run，再取消产品任务（不传播 Kernel）。

### 1.4 API

```text
POST /v1/tasks/{task_key}/agent-run
GET  /v1/tasks/{task_key}/agent-run
POST /v1/tasks/{task_key}/agent-run/cancel
```

### 1.5 线上

- AgentGraph 镜像已重建并启动；
- `/healthz` 200；
- OpenAPI 已出现 agent-run 两个路径；
- Agent Runtime :8002 正常。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| AgentGraph 非集成 | **80 passed, 28 skipped** |
| agent_runtime | **28 passed** |
| Runtime 线上 smoke | create/start/get/cancel 全部成功 |

新增单测：

- `runtime_status_action` 映射；
- `AgentRuntimeClient` create/start/get/cancel 路径和 `X-Runtime-Key`。

> 没有 AgentGraph 真实 DB + Runtime Run 的端到端测试；AgentGraph 集成测试仍受 pgvector 阻塞。

---

## 3. 限制

1. 没有常驻 Runtime Worker + 生产 Model/Tool，Agent Run 创建后会停在 RUNNING，不会自动完成。
2. 产品状态投影只在 GET 时触发；没有后台投影循环。
3. COMPLETED 只对 READY 任务做 claim+REVIEWING；IN_PROGRESS/TODO 不自动处理。
4. 没有产品 UI；只有 REST。
5. 没有 AgentGraph → Runtime 的失败补偿/对账。
6. 没有真实模型端到端验证。
7. 未提交 git。

---

## 4. 下一步（Phase 2M）

1. 常驻 Runtime Worker（生产 Model/Tool 端点）；
2. Agent Run 后台投影循环；
3. AgentGraph UI（创建/查看/取消 Agent Run）；
4. 失败补偿/对账；
5. 1,000 Runner 容量与生产认证。

---

## 5. 结论

AgentGraph 现在能为产品任务创建/启动/读取/取消 Agent Runtime Run，并在读取时投影 terminal 状态；线上路由已存在。**但 Runtime 没有常驻 Worker/真实模型，产品状态只在 GET 时同步，也没有 UI。**
