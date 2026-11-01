# PHASE2B_REPORT — Agent Loop / Tool 幂等 / Budget

> 日期：2026-10-31
> 范围：Phase 2B 核心：LoopController、ModelAction/ToolRuntime 协议、Tool Invocation、Usage/Budget
> 状态：库 + 真实 PostgreSQL 测试通过；**未接入 AgentGraph，未提供生产 Model Gateway/Tool Runtime**

---

## 1. 本次完成

### 1.1 LoopController

`agent_runtime/loop.py`：

```text
step(run_id):
  RUNNING? / budget?
  -> model.decide(run, checkpoint_state)
  -> persist MODEL step
  -> add usage
  -> budget exceeded? -> BUDGET_EXCEEDED
  -> finish? -> COMPLETED
  -> pause? -> PAUSING -> PAUSED
  -> tool? -> begin tool invocation
              already SUCCEEDED -> reuse (no side effect)
              else RUNNING -> WAITING_TOOL -> tool.execute
                   -> persist TOOL step + result
                   -> RUNNING/FAILED
  -> checkpoint
```

- 取消检查：step 开始时不是 RUNNING 直接返回；tool 前再检查。
- Budget：模型调用前检查已有 usage；模型调用后检查新 usage；超限不执行工具。
- Tool 幂等：`begin_invocation` 以 idempotency key 去重；已成功调用直接复用结果。
- Checkpoint：模型/工具步骤后持久化，重启后从 PostgreSQL 恢复。

### 1.2 存储扩展

`agent_runtime/store.py`：

- `agent_runs.usage`（JSONB）：model_calls / prompt_tokens / completion_tokens / cost_usd；
- `invocations` 表：kind（model/tool）、name、request、status、result、error、usage、幂等键；
- `record_step` / `next_step_no`；
- `begin_invocation` / `finish_invocation`；
- `add_usage`（行锁 + JSONB 累加）。

### 1.3 测试

```bash
RUNTIME_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test' \
python -m pytest -q agent_runtime/tests
```

结果：**11 passed**（真实 PostgreSQL）

新增覆盖：

- tool → finish 完整循环；
- 同一 tool idempotency key 不重复执行；
- 模型调用后超预算 → BUDGET_EXCEEDED，不执行 tool；
- 已超预算 → 不再调用模型；
- 已取消 Run → 不调用模型；
- 第一轮 tool 后重启 Store，第二轮 finish 完成，checkpoint 仍在。

---

## 2. 限制

1. 没有生产 Model Gateway/Tool Runtime，只有 Protocol + 测试适配器。
2. 没有 Run API / Worker / 租约；`step()` 是库调用，不是可调度进程。
3. `begin_invocation` 对 PENDING/崩溃窗口没有 reconciliation；依赖工具自身按 idempotency key 去重。
4. 没有版本化迁移文件；`init_schema` 直接建表/ALTER。
5. 没有 Context Engine/子 Agent/人工审批。
6. 没有接入 AgentGraph 产品任务。
7. 未提交 git。

---

## 3. 下一步（Phase 2C）

1. 版本化迁移（001_init/002_invocations_usage + schema_migrations）；
2. Runtime API（create/get/pause/resume/cancel/steps/checkpoints）；
3. Run Worker + 租约/心跳/恢复；
4. 生产 Model Gateway/Tool Runtime 适配器；
5. Context 组装 + 子 Agent 委派；
6. AgentGraph → Runtime 接入与投影；
7. 重启/重复事件/工具超时集成测试。

---

## 4. 结论

Agent Runtime 现在能真实执行一个持久化的“模型决策 → 工具调用 → checkpoint → 完成”循环，并具备工具幂等、预算阻断、取消检查和重启恢复。**但它还不是可调度服务，也没有产品接入。**
