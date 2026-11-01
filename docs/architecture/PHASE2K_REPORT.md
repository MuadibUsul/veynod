# PHASE 2K REPORT — 配额、Provider 健康、进程级 supervisor

日期：2026-10-31（UTC-4）
范围：补齐 Runtime 的运营面（配额查询/前置拒绝、Provider 健康、进程级守护），不改变 Run 状态机。

---

## 1. 交付

### 1.1 租户配额前置检查

- Runtime 新增 `GET /v1/quota/{tenant_id}`：
  - 返回 `policy`（`max_model_calls` / `max_prompt_tokens` / `max_completion_tokens` / `max_cost_usd`）；
  - 返回当前 `usage`、`exceeded`、`reason`；
  - 策略来自 `RUNTIME_TENANT_*` 环境变量（`QuotaPolicy.from_env()`）。
- AgentGraph 在 `create_task_run` 创建 Run **之前**调用该端点：
  - `exceeded=true` → HTTP **429**，不创建 Run，不消耗模型调用；
  - Runtime 没有配额端点（404，旧版本）→ 记录 warning 后跳过前置检查，保持向后兼容。
- Runtime 内部 `LoopController` 仍在每次模型调用前检查，两层防护。

### 1.2 Provider 健康

- 新增 `GET /v1/provider-health?window_minutes=15`：
  - 从 `invocations` 表按 `kind`（`model`/`tool`）聚合调用数、失败数、失败率、最后失败时间；
  - `status`: `healthy`（model 失败率 ≤10%）、`degraded`（>10%）、`unknown`（窗口内无调用）；
  - 这是“最近调用结果”的健康，不是主动探测。
- 新增 `RunStore.invocation_health(window_minutes)`（只读聚合查询）。

### 1.3 进程级 supervisor

- `scripts/runtime_supervisor.py`：
  - 启动 `python -m agent_runtime.worker_main` 子进程；
  - 子进程异常退出后按 1s → 2s → … → 30s 指数退避重启；
  - 稳定运行 60s 后重置退避；
  - 可选 metrics 健康检查（默认 30s 一次，连续 3 次失败则重启）；
  - 启动前检查 metrics 端口，避免重复启动；
  - 转发 SIGINT/SIGTERM，先 terminate 再 kill。
- Worker 自身 `/metrics` 仍暴露 `workers/started/ids/stopping`。

---

## 2. 线上验证

```text
GET http://127.0.0.1:8002/v1/quota/tenant-demo
{"tenant_id":"tenant-demo","policy":{"max_model_calls":null,...},
 "usage":{"model_calls":0.0,...},"exceeded":false,"reason":null}

GET http://127.0.0.1:8002/v1/provider-health
{"window_minutes":15,"status":"unknown","providers":{},"groups":[]}
```

AgentGraph 前置拒绝实测（把 Runtime 临时配置为 `RUNTIME_TENANT_MODEL_CALL_LIMIT=0`，然后创建一个新任务并调用 agent-run）：

```text
POST /v1/tasks/TASK-580E3436/agent-run
HTTP 429
{"error":{"code":"http_error","message":"tenant runtime quota exceeded: \
tenant model_calls exceeded max_model_calls=0.0", ...}}
```

测试后已恢复 Runtime 配额默认值（无限制），任务未创建 Run、未产生模型调用。

supervisor 崩溃重启实测：

```text
runtime supervisor: worker started pid=30648
RUNTIME_DATABASE_URL is required
runtime supervisor: worker exited with code 1 after 2.0s; restarting in 1s
runtime supervisor: worker started pid=26708
...
```

---

## 3. 测试

| 套件 | 结果 |
|---|---|
| `agent_runtime/tests`（含 Postgres 集成） | **28 passed** |
| `tests/test_api.py` 新增 quota/provider-health 断言 | passed |
| AgentGraph `test_agent_runtime.py`（quota 前置请求 + 客户端协议） | **2 passed** |
| AgentGraph 非集成 | **80 passed, 28 skipped** |
| supervisor `--help` / 崩溃重启实测 | 通过 |
| 线上 quota / provider-health | 200，字段符合预期 |

---

## 4. 限制

1. 配额策略是全局环境变量，不是每租户独立配置；多租户差异化配额尚未实现。
2. Provider 健康只看 `invocations` 最近窗口，不主动探测模型/工具端点。
3. AgentGraph 只在“创建 Run”时检查配额；长时间运行的 Run 不会因租户后续超额被中断（Runtime Loop 的前置检查仅在每次模型调用前生效）。
4. supervisor 是普通进程守护，不是 Windows 服务/K8s；生产建议 systemd `Restart=always` 或 NSSM。
5. 没有告警/通知：健康降级只暴露 API，不主动推送。
6. 未提交 git。

---

## 5. 结论

Runtime 现在有可查询、可前置拒绝的租户配额，有基于真实调用结果的 Provider 健康，以及可独立运行的进程守护。它解决的是“运营可见性和基本保护”，不是多租户计费/自动扩缩容。
