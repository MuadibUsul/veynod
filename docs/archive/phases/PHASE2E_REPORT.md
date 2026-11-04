# PHASE2E_REPORT — HTTP Model/Tool 适配器与 Worker 入口

> 日期：2026-10-31
> 范围：Phase 2E 第一批：可部署的 HTTP Model Gateway / Tool Runtime + Worker 进程入口
> 状态：测试通过；未调用真实外部模型；未启动常驻 Worker

---

## 1. 本次完成

### 1.1 HTTPModelGateway

`agent_runtime/gateway.py`：

- OpenAI-compatible `{base_url}/chat/completions`；
- 强制模型返回一个 JSON `ModelAction`（tool/finish/pause）；
- 解析 `usage.prompt_tokens/completion_tokens`；
- 429/5xx/timeout 指数退避重试（有上限）；
- 4xx 直接失败；API key 只放请求头，不写日志；
- `httpx.MockTransport` 测试。

### 1.2 HTTPToolRuntime

- Tool allowlist：不在列表直接拒绝，不发 HTTP；
- 请求 `{base_url}/tools/{name}`，携带 `Idempotency-Key`；
- 可选 Bearer key；
- 429/5xx/timeout 重试（同一 idempotency key）；
- 4xx 返回 ToolResult(ok=false,error)；
- 测试覆盖 allowlist、idempotency header、HTTP 错误。

### 1.3 Worker 进程入口

`agent_runtime/worker_main.py`：

- `python -m agent_runtime.worker_main`；
- 环境变量：
  - `RUNTIME_DATABASE_URL`
  - `RUNTIME_MODEL_URL/API_KEY/NAME`
  - `RUNTIME_MODEL_TIMEOUT_SECONDS/MAX_RETRIES`
  - `RUNTIME_TOOL_URL/API_KEY/ALLOWLIST`
  - `RUNTIME_WORKER_ID/LEASE_SECONDS/POLL_SECONDS`
- 启动时 `init_schema`（版本化迁移）；
- SIGINT/SIGTERM 优雅停止，关闭 HTTP client/store；
- 未配置模型/工具时拒绝启动。

### 1.4 依赖清单

`agent_runtime/requirements.txt`：fastapi、uvicorn、sqlalchemy、asyncpg、httpx、pydantic。

---

## 2. 测试结果

```bash
RUNTIME_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test' \
python -m pytest -q agent_runtime/tests
```

**19 passed**（真实 PostgreSQL + MockTransport）

新增覆盖：

- Model Gateway 解析 action/usage；非 JSON 报错；
- Tool Runtime allowlist；`Idempotency-Key` 透传；HTTP 400 错误；
- 原有 store/loop/API/worker 测试继续通过。

---

## 3. 限制

1. 没有调用真实模型 Provider；只验证了协议/解析/重试路径。
2. 没有 Provider 路由、配额、fallback、成本上限（模型端）。
3. 没有 Tool 级 schema 校验/网络 allowlist/沙箱；只有 URL + allowlist。
4. 没有 Worker supervisor/部署；未启动常驻 Worker。
5. 没有 Context Engine、子 Agent、人工审批。
6. 没有接 AgentGraph。
7. 未提交 git。

---

## 4. 下一步（Phase 2F）

1. Context Engine（产品任务/文档/Snapshot/符号上下文）；
2. 子 Agent 委派；
3. Provider 路由/配额/成本预算；
4. Worker supervisor/metrics；
5. AgentGraph 创建 Agent Run + 投影；
6. 真实模型端到端测试（需 API key/endpoint）。

---

## 5. 结论

Runtime 现在有可部署的 HTTP 模型/工具适配器和 Worker 入口，但没有真实模型调用、Context、委派、部署 supervisor，也没有产品接入。
