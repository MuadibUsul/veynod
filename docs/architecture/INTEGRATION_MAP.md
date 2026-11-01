# INTEGRATION_MAP — 运行时集成与调用链

> 目的：画出当前真实进程、API、数据库、事件和失败路径，作为 Phase 1 契约与 Phase 3 投影的基线。
> 证据：源码、OpenAPI 现场探测、进程/数据库检查；日期 2026-10-31。

---

## 1. 进程拓扑（当前）

```text
┌─────────────────────────────────────────────────────────────┐
│ Browser / Human / Agent                                     │
│  - 人类只登录 AgentGraph                                    │
│  - Agent 使用 AgentGraph token 或 Kernel vnk token          │
└───────────────┬───────────────────────────────┬─────────────┘
                │ AgentGraph REST/MCP/SSE        │ Kernel syscalls/MCP/SSE
                ▼                               ▼
┌──────────────────────────────┐   ┌──────────────────────────────────┐
│ AgentGraph API :8000         │   │ Veynod Kernel API :8001          │
│ Docker, 3.24.0               │   │ 4.0.0, 28 routes                 │
│ - /v1/*, /v1/human/*         │   │ - /v1/admin/*                    │
│ - /v1/code/*                 │   │ - /v1/syscalls/*                 │
│ - /mcp                       │   │ - /v2/syscalls/submit_patch      │
│ - /dashboard/*               │   │ - /v2/submissions/*              │
│ - SSE / inbox / webhook      │   │ - /v1/runner/*, /v1/events/stream│
│ - background loops           │   │ - /v1/dag*, /dashboard (unauth)  │
└──────────┬───────────────────┘   └──────────┬───────────────────────┘
           │ code_engine HTTP + X-Admin-Key    │
           └──────────────┬────────────────────┘
                          │
                          ▼
          ┌─────────────────────────────────────┐
          │ PostgreSQL 16.15 :55432             │
          │ - agentgraph DB（48 表）             │
          │ - veynod_dev（15 表 + probe）        │
          └──────────────┬──────────────────────┘
                         │
                         ▼
          ┌─────────────────────────────────────┐
          │ Kernel Gate Worker（独立进程）       │
          │ - GateWorker × 8                    │
          │ - MergeCoordinator                  │
          │ - pyright/ruff/pytest               │
          │ - Git worktree/update-ref           │
          │ - outbox/edge/worktree pool         │
          │ - metrics :8030                     │
          └─────────────────────────────────────┘

          ┌─────────────────────────────────────┐
          │ AgentGraph CLI runner（外部进程）    │
          │ - 领取 placement，启动 claude/codex  │
          │ - 不直接调用 Kernel                  │
          └─────────────────────────────────────┘

          ┌─────────────────────────────────────┐
          │ Kernel CLI runner（外部进程）        │
          │ - claim Kernel task                 │
          │ - worktree + 模型 + v2 submit       │
          └─────────────────────────────────────┘
```

### 1.1 现场状态

| 服务 | 地址 | 状态 |
|---|---|---|
| AgentGraph | `:8000`（Docker） | health 3.24.0；单 FastAPI 进程 |
| Kernel API | `:8001`（本地 Python） | 4.0.0；28 routes；v2 已启用 |
| Kernel Worker | 无固定端口，metrics `:8030` | 8 workers + MergeCoordinator |
| PostgreSQL | `:55432` | `veynod_dev` 15 表；`agentgraph` DB 由容器内部访问 |
| Kernel debug dashboard | `:8001/dashboard` | 未鉴权，200 |
| AgentGraph Code 页面 | `:8000/dashboard/code/` | 复用 AgentGraph 会话 |

---

## 2. API 契约

### 2.1 AgentGraph → Kernel（内部 Admin API）

认证：`X-Admin-Key`（`KERNEL_ADMIN_KEY`；Kernel `require_admin` 常量时间比较）。

| 方法 | 路径 | 用途 | 证据 |
|---|---|---|---|
| POST | `/v1/admin/workspaces` | 创建代码 workspace（repo_path/main_branch） | `admin_routes.py:218` |
| GET | `/v1/admin/workspaces` | 列出 workspace | `admin_routes.py:261` |
| POST | `/v1/admin/artifacts` | 注册 stub/test artifact（FROZEN） | `admin_routes.py:317` |
| GET | `/v1/admin/artifacts` | 列出 artifact | `admin_routes.py:350` |
| POST | `/v1/admin/tasks` | 创建 DAG task | `admin_routes.py:372` |
| GET | `/v1/admin/tasks` | 列出 task（Code 引擎用） | `admin_routes.py:474` |
| GET | `/v1/admin/tasks/{id}` | 读取 task | `admin_routes.py:498` |
| POST | `/v1/admin/workspaces/{id}/scan-stubs` | 扫描 stub | `admin_routes.py:514` |
| POST | `/v1/admin/workspaces/{id}/edges/rebuild` | 重建符号边 | `admin_routes.py:750` |
| POST | `/v1/admin/tokens` | 签发 runner token | `admin_routes.py:284` |
| GET/POST | `/v1/admin/metrics` | 运行时指标/reset | `admin_routes.py:301-311` |

AgentGraph 实际使用：`KernelEngineClient.create_artifact/create_task/list_tasks`（`code_engine.py:53-111`）。**没有使用** artifact snapshot、scan-stubs、edges rebuild、token 签发。

### 2.2 Runner → Kernel（syscalls / v2）

认证：`Bearer vnk_...`（`agent_tokens` 哈希；dev 模式接受任意格式合法 token）。

| 方法 | 路径 | 用途 | 证据 |
|---|---|---|---|
| POST | `/v1/syscalls/pull_task_bundle` | claim + 返回任务包 | `syscalls.py:654` |
| POST | `/v2/syscalls/submit_patch` | 异步提交，202 | `syscalls.py:717` |
| POST | `/v1/submissions` | 隐藏别名，202 | `syscalls.py:723` |
| GET | `/v2/submissions/find` | 按 idempotency key 查 | `syscalls.py:833` |
| GET | `/v2/submissions/{id}` | 状态/终态 | `syscalls.py:870` |
| POST | `/v1/syscalls/submit_patch` | 旧同步提交；默认 410 | `syscalls.py:907` |
| POST | `/v1/syscalls/request_task_split` | 拆任务 | `syscalls.py:947` |
| POST | `/v1/syscalls/escalate_contract_defect` | 契约缺陷升级 | `syscalls.py:974` |
| POST | `/v1/syscalls/inject_dependency_task` | 注入依赖任务 | `syscalls.py:993` |
| POST | `/v1/syscalls/query_symbol_signature` | 查询符号签名 | `syscalls.py:1007` |
| POST | `/mcp` | MCP JSON-RPC | `syscalls.py:1074` |

**注意：** MCP `submit_patch` 仍绑定旧 v1 handler（`syscalls.py:1046-1050`），默认 410；CLI 走 `/v2`。

### 2.3 Runner → Kernel（runner/SSE）

| 方法 | 路径 | 用途 | 证据 |
|---|---|---|---|
| GET | `/v1/events/stream` | SSE；`since`/Last-Event-ID；默认最新 | `runner_routes.py:130` |
| POST | `/v1/runner/heartbeat` | 续 task lease | `runner_routes.py:208` |
| POST | `/v1/runner/release` | 释放 task | `runner_routes.py:253` |
| GET | `/v1/runner/tasks/{id}` | 读 task 状态（**无 workspace 校验**） | `runner_routes.py:316` |
| GET | `/v1/runner/ping` | 健康/身份 | `runner_routes.py:340` |

### 2.4 Kernel → AgentGraph

Kernel 不主动调用 AgentGraph；由 AgentGraph 的 `code_engine` 轮询 Kernel 并回写：

| AgentGraph 内部调用 | 用途 | 证据 |
|---|---|---|
| `POST /v1/quality/gate-results` | 写 CI/TESTS PASS（当前合成） | `code_engine.py:291-302` |
| `POST /v1/tasks/{key}/claim` | 接管任务 | `code_engine.py:304-309` |
| `POST /v1/tasks/{key}/handoff` | 进入 REVIEWING | `code_engine.py:310-327` |
| `GET /v1/quality/tasks/{key}/audit-view` | 审计视图 | `code_engine.py:365-372` |

**没有 Kernel→AgentGraph 的事件/回调。** 这是 Phase 3 要补的投影/事件。

### 2.5 AgentGraph 产品 API（摘要）

- `/v1/tasks` create/list/get/claim/renew/release/handoff/review/escalate/cancel/subtasks/tree；
- `/v1/human/*` 人类控制台；
- `/v1/code/runs` code run；
- `/v1/documents`、`/v1/events`、`/v1/inbox/stream`、`/v1/runner/*`、`/v1/loop/*`、`/v1/diagrams/*`；
- `/mcp` 113 tools / 3 resources；
- `/v1/admin/*` operator。

---

## 3. 完整代码任务时序（当前）

### 3.1 创建

```text
User/Agent
  -> AgentGraph POST /v1/code/runs
     -> require task:create
     -> repo_path == KERNEL_REPO_PATH
     -> Kernel POST /v1/admin/artifacts ×2 (stub, test; FROZEN)
     -> Kernel POST /v1/admin/tasks (IMPLEMENT_BODY)
     -> AgentGraph create collab_task READY
        input_context.code_engine=veynod-kernel
        input_context.kernel={workspace_id, task_id, base_url}
  <- {agentgraph_task_key, kernel_task_id, ...}
```

失败模式：

- Kernel artifact/task 创建失败 → AgentGraph 不建任务；
- Kernel task 创建成功、AgentGraph task 创建失败 → **孤儿 Kernel task**；
- 重复请求 → 创建重复 Kernel task/AgentGraph task（无端到端幂等键）。

### 3.2 领取与执行

```text
Kernel CLI runner
  -> POST /v1/syscalls/pull_task_bundle
     -> Kernel claim READY→LEASED
     -> base_commit_sha=workspaces.head_commit_sha
     -> lease_token_hash/expires_at
  <- bundle: task, spec, artifacts, symbols, write_locks
  -> create detached worktree at base_commit_sha
  -> run claude/codex/custom
  -> local pytest/preflight
  -> git diff -> POST /v2/syscalls/submit_patch
```

Runner 每 30–300s 心跳；SSE 监听 `TASK_ABORT` 以杀死 agent。`TASK_ABORT` 只在契约升级时发出，不是通用取消。

### 3.3 提交/门禁/合并

```text
POST /v2/syscalls/submit_patch
  -> validate lease (status LEASED, hash, runner, not expired)
  -> idempotency key bind (task, lease, patch_sha)
  -> INSERT patch_submissions QUEUED
  <- 202 {submission_id, status_url, Retry-After}

GateWorker.claim:
  FOR UPDATE SKIP LOCKED
  QUEUED -> RUNNING
  work_lease_id/owner/expires
  attempts += 1

GateWorker.process:
  revalidate task lease/status
  gate.verify_patch:
    1 WRITE_SET_BOUNDARY (git apply, actual files vs file locks, symlink reject)
    2 AST_SCOPE_DIFF (changed symbols vs write_locks)
    3 CONTRACT_HASH_GUARD (frozen/committed signature_hash)
    4 STATIC_TYPECHECK (pyright, then ruff)
    5 UNIT_TEST_SUITE (pytest; declared test artifacts)
  -> VerifiedPatch {base_sha, task_commit_sha, changed_files, gate_results}
  -> INSERT merge_intents PENDING

MergeCoordinator:
  acquire per-workspace lease + fencing token
  claim PENDING/RUNNING intent
  if main advanced:
      if dependency/test changed or !fast_forward: REVALIDATE
      else tree merge or retry
  else:
      git update-ref <new> <old> CAS
      on_task_committed: task VERIFYING->COMMITTED, workspace head, downstream in_degree
  finish intent MERGED/REJECTED

Runner polls GET /v2/submissions/{id}
  -> COMMITTED / FAILED + commit_sha + gate_results
```

### 3.4 AgentGraph 同步

```text
GET /v1/code/runs/{task}
  -> Kernel list_tasks -> task status
  -> if COMMITTED:
       record CI PASS + TESTS PASS (hardcoded counts 4/0)
       claim AgentGraph task
       handoff REVIEWING
  -> return {agentgraph_status, kernel_status, commit_sha, ...}
```

**没有后台同步 worker**；只有人/Agent 主动 GET 时才同步。`list_runs` 也只读，不同步。

---

## 4. 事件流

### 4.1 AgentGraph

```text
Business transaction
  -> row change triggers notify_workspace_change()
  -> workspace_events (targets computed, seq deferred at commit)
  -> SSE /v1/stream (LISTEN + replay) / inbox stream / webhook outbox
```

- 投递：至少一次；消费者按 event_id 去重、按 seq 续读。
- Poll：`/v1/events` 或 `workspace_sync(since)`；cursor 过期 410，要求 resync。
- SSE：`/v1/stream` 用 `row.id` 作为 seq（与 poll 的 `workspace_events.seq` 不同）；重放上限 500；队列满丢最旧，依赖重连重放。
- Inbox：`workspace_events.targets` + watermark；无独立 inbox 表。
- Webhook：HMAC、幂等键、重试、DEAD；无 DEAD 重放 API。

### 4.2 Kernel

```text
Business transaction
  -> EventBus.publish()
  -> kernel_events row + event_outbox row (same transaction)
  -> OutboxDispatcher: pg_notify('veynod_kernel_events')
  -> EventHub polls kernel_events -> SSE /v1/events/stream
```

- `kernel_events.event_seq` 是**全局 BIGSERIAL**，不是 per-workspace gapless；SSE `id:` 用它。
- 仓库内没有 LISTEN consumer；pg_notify 实际是 write-only（延迟由 EventHub 轮询 1s 决定）。
- 无 DLQ、无 retention、无 correlation_id/causation_id。
- AgentGraph 不消费 Kernel 事件；靠 HTTP 轮询。

### 4.3 跨域事件缺口

| 需要的事件 | 当前 |
|---|---|
| ProductTaskCreated/Cancelled | AgentGraph 内部有；Kernel 不知道 |
| AgentRun* | 不存在 |
| PlanCreated/Revised | 不存在 |
| KernelTaskLeased/Blocked/Completed/Failed | Kernel 有 `TASK_LEASED` 等；AgentGraph 不消费 |
| PatchSubmitted | Kernel submission row；无事件给 AgentGraph |
| GateStarted/Passed/Failed | Kernel gate_results；AgentGraph 合成 CI/TESTS |
| MergeStarted/Committed/Conflict | Kernel merge_intents；AgentGraph 只在 GET 时读 |
| DeliveryReady/Approved | 不存在 |

---

## 5. 失败与恢复路径

### 5.1 已实现/有测试

| 场景 | 行为 | 证据 |
|---|---|---|
| Gate 拒绝 | task 回 LEASED（retry）或 FAILED；submission FAILED | `verification_gate.py:1302-1338`；集成测试 |
| 外部 commit 改依赖 | merge REVALIDATE，重跑 gate | `test_integration.py:1432-1500` |
| 外部 commit 同行冲突 | 不合并，保留外部 commit | `test_integration.py:1502-1575` |
| Git 已更新/DB 未更新 | startup reconcile 从 intent 重建 DB | `test_merge_coordinator.py:141-170` |
| DB 说 COMMITTED/Git 没有 | 清 result_commit，task LEASED/FAILED，intent REVALIDATE | `test_merge_coordinator.py:174-226` |
| Worker 崩溃后 work lease 过期 | 可被其他 worker 重新 claim | `test_work_leases.py` |
| Worker 在 VERIFYING 后死亡 | submission FAILED interrupted，不自动重放 gate | `gate_worker.py:257-268` |
| 两个 API 竞争 claim | 一个 winner + authority row | `test_horizontal_api.py` |
| v2 提交 API 重启 | submission 持久化，worker 继续 | `test_submission_restart_e2e.py` |

### 5.2 未实现/高风险

| 场景 | 当前 | 风险 |
|---|---|---|
| AgentGraph 取消 | 只改 AgentGraph task（FAILED）；Kernel 继续 gate/merge | 取消无效，代码仍可能合并 |
| Kernel 取消 | 无 API/状态 | 不能停止 sandbox/merge |
| Worker 丢失 task lease 后 merge | merge 不复核 task lease；可能推进 Git | Git/DB 分叉 |
| PENDING intent + submission FAILED | coordinator 仍可 merge intent | Git 有 commit，submission 失败 |
| 多个 submission 同一 lease | 第二个可能在 task COMMITTED 后返回 COMMITTED 未跑 gate | 假成功 |
| test-time mutation | pytest 后重新 stage/commit，不重跑 AST/contract | 未验证代码被提交 |
| tree merge | 默认关闭；无端到端测试；snapshot 复制可能不匹配 | 静默覆盖/过期快照 |
| API-only restart | 只对账 HEAD；不跑 merge reconcile（只在 worker 启动） | 不一致状态遗留 |
| 事件重复/乱序 | 至少一次；无 aggregate version 防护 | 投影可能回退 |

---

## 6. 部署/启动/迁移

### 6.1 AgentGraph

```bash
cd E:/Codes/veynod/AgentGraph
docker compose up -d --build
```

需要配置：`KERNEL_URL`、`KERNEL_ADMIN_KEY`、`KERNEL_WORKSPACE_ID`、`KERNEL_REPO_PATH`。AgentGraph 是单 FastAPI 进程；后台循环在 lifespan 内。

### 6.2 Kernel

```bash
cd E:/Codes/veynod/veynod_kernel
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_dev
export VN_AUTH_MODE=dev
export VN_ADMIN_API_KEY=dev-admin
export VN_DEFAULT_WORKSPACE_ID=<workspace-id>
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001

# 必须独立启动
python -m app.worker --workers 8 --database-url "$DATABASE_URL"
```

启动时 `init_db` 重放 genesis + 所有迁移（依赖幂等）。API 与 worker 都可跑迁移，使用 advisory lock 序列化；多实例同时启动安全。

### 6.3 迁移

- AgentGraph：69 个迁移；生产迁移需单独执行/备份。
- Kernel：`v4_001` + `v4_002`–`v4_008`；`schema_migrations` 只记录 v4_002–v4_005，v4_006–008 缺失记录。
- 回滚：当前没有 down migration/演练；Phase 1/4 必须补。

### 6.4 端口/网络

- AgentGraph :8000 对外；Kernel :8001 应只内网/loopback。
- 当前 `/v1/dag*`、`/dashboard` 未鉴权；`/v1/admin` 需 key，但默认 dev key 弱。
- Kernel docs 有 `0.0.0.0` 示例；生产必须禁止。
- Runner CLI 默认 API URL 是 8000（AgentGraph），使用 Kernel runner 时必须显式 `--api http://127.0.0.1:8001`。

---

## 7. 集成结论

1. **当前主链路可用，但是“百级 + 人工触发同步”的链路**：AgentGraph 创建 → Kernel 执行 → AgentGraph GET 同步 REVIEWING。
2. **没有跨域事件投影**：AgentGraph 不知道 Kernel 的 LEASED/VERIFYING/FAILED；Kernel 不知道 AgentGraph 的取消/重试。
3. **没有统一身份**：`input_context` JSONB 是唯一关联；没有 correlation_id。
4. **Gate 证据不可信**：AgentGraph 合成 PASS；必须改为读取 `patch_submissions.gate_results` / `merge_intents.gate_results`。
5. **取消/失败/重试未闭环**：这是 Phase 3 的核心工作。
6. **多仓库/多租户未完成**：syscalls 默认 workspace；token 全局；不能直接扩展为多租户服务。

Phase 1 应先冻结 API/事件契约，Phase 3 再做投影和取消闭环；不要先拆服务。
