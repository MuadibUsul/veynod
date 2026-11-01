# PHASE2H_REPORT — Runtime Worker Pool

> 日期：2026-10-31
> 范围：Phase 2H 第一批：多 Worker 监督池 + worker_main 接入
> 状态：库 + 真实 PostgreSQL 测试通过；未启动常驻 Worker，未做 Provider 配额

---

## 1. 本次完成

### 1.1 RuntimeWorkerPool

`agent_runtime/pool.py`：

- 启动 N 个 `RuntimeWorker` 循环；
- 每个 worker 独立 worker_id；
- 某个 worker loop 异常退出时 supervisor 重新启动；
- `stop()` 停止并等待所有 task；
- `snapshot()` 返回 workers/started/ids/stopping。

### 1.2 worker_main

- 从 `RUNTIME_WORKER_COUNT`（默认 2）启动 Pool；
- 每个 Worker 共享：
  - HTTPModelGateway；
  - HTTPToolRuntime；
  - ContextBuilder（`RUNTIME_CONTEXT_TOKEN_BUDGET`）；
- SIGINT/SIGTERM → stop pool → 关闭 HTTP client/store。

### 1.3 测试

- 两个 Worker 同时轮询；一个 claim 到 RUNNING Run 并完成，另一个空闲；
- `snapshot` 显示 2 个 worker；
- `stop` 正常结束；
- 现有 24 个 runtime 测试全部通过。

---

## 2. 测试结果

```bash
RUNTIME_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test' \
python -m pytest -q agent_runtime/tests
```

**24 passed**（真实 PostgreSQL + MockTransport）

---

## 3. 限制

1. Pool 是**进程内** asyncio 监督，不是 OS 进程 supervisor；进程崩溃不会自动重启。
2. 没有 metrics/health endpoint/自动扩缩容。
3. 没有 Provider 路由/配额/成本上限。
4. 委派仍没有独立幂等键，父状态变化可能留下 orphan child。
5. 没有接 AgentGraph。
6. 未提交 git。

---

## 4. 下一步（Phase 2I）

1. Provider 路由/配额/成本；
2. 委派幂等与 orphan reconciliation；
3. 进程级 supervisor/metrics（或 K8s deployment）；
4. AgentGraph 创建 Agent Run + 投影；
5. 真实模型端到端测试。

---

## 5. 结论

Runtime 现在有一个进程内多 Worker 监督池，worker_main 可以按 `RUNTIME_WORKER_COUNT` 启动；但它不是 OS 级 supervisor，没有 metrics/配额，也没有产品接入。
