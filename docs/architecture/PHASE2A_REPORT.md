# PHASE2A_REPORT — Durable Agent Runtime 持久化核心

> 日期：2026-10-31
> 范围：Phase 2A：Agent Run 状态机、Step/Checkpoint/Event/Outbox/幂等持久化
> 状态：独立包 + 真实 PostgreSQL 测试通过；**未接入 AgentGraph**，不含模型/工具循环

---

## 1. 本次完成

新增 `agent_runtime/` 包：

```text
agent_runtime/
├── __init__.py
├── errors.py
├── states.py
├── store.py
├── README.md
├── pytest.ini
└── tests/test_runtime.py
```

### 1.1 PostgreSQL schema `agent_runtime`

- `agent_runs`：run_id、tenant/workspace、product_task_id、workflow、status、version、budget、correlation、时间、last_error；
- `agent_steps`：step_no、kind、status、payload、idempotency_key；
- `run_checkpoints`：version、state JSONB、idempotency_key；
- `run_events`：事件日志（BIGSERIAL）；
- `runtime_outbox`：与 run_events 同事务写入；
- `runtime_idempotency`：命令幂等记录。

### 1.2 Run 状态机

按计划实现：

```text
CREATED → QUEUED → INITIALIZING → PLANNING → READY → RUNNING
RUNNING → WAITING_MODEL/WAITING_TOOL/WAITING_CHILDREN/BLOCKED/
          CHECKPOINTING/REPLANNING/PAUSING/VERIFYING/
          COMPLETED/FAILED/CANCELLED/TIMED_OUT/BUDGET_EXCEEDED
PAUSING → PAUSED → RESUMING → RUNNING
```

- 显式 `ALLOWED` 转换矩阵；
- 非法转换抛 `InvalidTransition`；
- 终态集合明确。

### 1.3 RunStore

- `create_run`（支持幂等键）；
- `transition(expected_version)`：
  - 行锁 + `version` 条件；
  - 状态机校验；
  - 同事务写 `run_events` + `runtime_outbox`；
  - 幂等键重复返回原结果，不同命令返回冲突；
- `checkpoint`：版本递增、同事务事件/outbox、幂等键；
- `get_run` / `list_events` / `list_checkpoints`；
- 重启后从 PostgreSQL 重新读取 run/checkpoint。

### 1.4 测试

```bash
RUNTIME_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test' \
python -m pytest -q agent_runtime/tests
```

结果：**5 passed**（真实 PostgreSQL，schema `agent_runtime`）。

覆盖：

- 完整状态链 + 版本冲突；
- 非法转换 / 不存在 run；
- create/transition 幂等键；
- checkpoint 幂等、事件、重启后重读；
- FAILED 记录 last_error/finished_at。

---

## 2. 没有完成（不要误报）

1. 没有 Loop Controller、Model Gateway、Tool Runtime；
2. 没有 Context 组装、Budget 执行、子 Agent 委派；
3. 没有接入 AgentGraph（产品任务/审批/UI 不知道 Agent Run）；
4. 没有 Worker/租约/恢复进程；
5. 没有运行时 API/CLI；
6. 没有迁移文件体系（`init_schema` 直接建表，Phase 2B 要改成版本化迁移）；
7. 没有并发热点/容量测试；
8. 未提交 git。

---

## 3. 下一步（Phase 2B）

1. 版本化 schema 迁移（`agent_runtime/migrations/`）；
2. Run API（create/get/pause/resume/cancel）；
3. Loop Controller + Model Gateway/Tool Runtime 协议；
4. Checkpoint 在模型决策/工具调用前后的持久化；
5. 工具副作用幂等/对账；
6. Budget 强制；
7. AgentGraph → Runtime 调用与投影；
8. 重启恢复/重复事件测试。

---

## 4. 结论

Phase 2A 建立了真实、可重启、带状态机/checkpoint/outbox/幂等的 Agent Run 存储，但还没有 Agent Loop、模型/工具执行和产品接入。**这是 Agent Runtime 的地基，不是可用产品。**
