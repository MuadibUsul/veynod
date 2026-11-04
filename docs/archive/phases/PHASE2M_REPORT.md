# PHASE 2M REPORT — 常驻 Runtime Worker、后台投影与产品 UI

日期：2026-10-31（UTC-4）
范围：Agent Runtime 的常驻执行、AgentGraph 的后台状态投影、失败/取消补偿、产品 UI 入口。

---

## 1. 交付

### 1.1 常驻 Runtime Worker

- `agent_runtime/worker_main.py` 常驻进程：`RuntimeWorkerPool` + `ReconciliationLoop` + `/metrics`（默认 `127.0.0.1:8031`）。
- 生产 Model Gateway：OpenAI 兼容 `/chat/completions`（已用 DeepSeek `deepseek-chat` 实测）。
- 生产 Tool Runtime：HTTP allowlist + `Idempotency-Key`（本地 demo 工具 `echo`）。
- Worker 池内崩溃自动重启；进程级重启由 `scripts/runtime_supervisor.py` 负责（见 PHASE2K_REPORT）。

当前线上进程（本机）：

```text
python -m agent_runtime.worker_main
RUNTIME_MODEL_NAME=deepseek-chat
RUNTIME_TOOL_ALLOWLIST=echo
RUNTIME_WORKER_COUNT=1
RUNTIME_WORKER_METRICS_PORT=8031

GET http://127.0.0.1:8031/metrics
{"workers": 1, "started": 1, "ids": ["runtime-0-3ddd59"], "stopping": false}
```

### 1.2 真实模型端到端（不是模拟）

AgentGraph 产品任务 → Agent Runtime Run → DeepSeek 调用 → checkpoint → COMPLETED → AgentGraph REVIEWING：

| 任务 | Runtime Run | 结果 | checkpoint |
|---|---|---|---|
| TASK-EFB0456A | `2b91d2bc-e6d7-4acb-8bf8-ada63b98e52b` | COMPLETED | `{"step":1,"result":{"summary":"bg2"}}` |
| TASK-6CB009F2 | `71241fd3-9937-4fa8-a573-0618bdbc7b9b` | COMPLETED | `{"step":1,"result":{"summary":"background projection ok"}}` |
| TASK-9EE2FD10 | `131bb010-755d-4bca-aa4b-e0c4f9a62bef` | COMPLETED | `{"step":1,"result":{"summary":"agentgraph runtime ok"}}` |

三个任务均已在 AgentGraph 中处于 `REVIEWING`；Runtime usage 记录为 1 次 model call、约 414–426 prompt tokens、34–46 completion tokens。

> 任务描述本身就是测试目标（“Return exactly one JSON action …”），因此这是真实的模型调用链路，不是靠模拟状态通过。

### 1.3 后台投影

- `project_code_runs_once` / `code_projection_loop`：Kernel 代码任务状态投影。
- `project_code_events_once`：Kernel 事件游标投影（多 workspace 发现）。
- `project_agent_runs_once` / `agent_run_projection_loop`：扫描带 `agent_runtime.run_id` 的 TODO/READY/IN_PROGRESS 任务，将 COMPLETED 投影为 REVIEWING，FAILED → FAILED，CANCELLED → CANCELLED。
- 每个循环独立捕获异常，单条失败不终止循环；每次 tick 记录 `scanned/projected/skipped/errors`。
- 投影是幂等的：已经 REVIEWING/终态的任务不会再次迁移。

### 1.4 产品 UI

- `GET /dashboard/agent-runs/`（`app/templates/agent_runs.html`）：登录会话令牌下创建/查看/取消 Agent Run。
- `dashboard/app/page.tsx` 侧边栏加入 Agent Runs 链接。

---

## 2. 失败/取消补偿

| Runtime 状态 | AgentGraph 动作 | 说明 |
|---|---|---|
| COMPLETED | READY → claim → REVIEWING | 只有 READY 任务自动进入评审；IN_PROGRESS/TODO 不自动 claim |
| FAILED | `fail_task` → FAILED | 使用 Runtime `last_error` 作为失败原因 |
| CANCELLED | `cancel_task` → CANCELLED | 不向 Kernel 传播（Run 不是 Kernel 任务） |

所有补偿都通过任务服务命令执行（审计 + 事件），不是直接改状态字段。

---

## 3. 测试

| 套件 | 结果 |
|---|---|
| `agent_runtime/tests`（含 Postgres 集成） | **28 passed** |
| AgentGraph 非集成 | **80 passed, 28 skipped** |
| AgentGraph Runtime client | **2 passed** |
| 线上 Runtime `/healthz` | 200 |
| 线上 Worker `/metrics` | 200 |
| 真实 DeepSeek E2E | 3 个 Run COMPLETED，任务 REVIEWING |

---

## 4. 限制

1. 自动评审只处理 READY 任务；处于 IN_PROGRESS/TODO 的完成 Run 只记录状态，不自动 claim。
2. 没有对模型输出的语义审查；REVIEWING 仍需人类/Auditor 决策。
3. 没有自动重试 FAILED Agent Run；重试需要重新创建 Run（Runtime 允许，产品层未暴露）。
4. 投影是轮询（默认 5s），不是事件驱动；Kernel 侧事件已使用游标，Runtime 侧尚未消费 outbox。
5. 没有 AgentGraph 到 Runtime 的分布式事务；创建 Run 成功但写任务上下文失败时会留下孤立 Run（可见但无人引用）。
6. 常驻 Worker 目前是单机进程；没有多机调度/leader election。
7. 未提交 git。

---

## 5. 结论

AgentGraph → Agent Runtime 已从“只有 REST 适配器”升级为**常驻 Worker + 真实模型 + 后台投影 + 产品 UI** 的可用闭环；三个真实任务已完成并进入 REVIEWING。该闭环仍是单机、轮询、人工评审，不是生产级多租户调度。
