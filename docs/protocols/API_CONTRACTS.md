# API_CONTRACTS — 跨域 API 契约（Phase 1A）

> 状态：Proposed；已建立契约模块和部分 Kernel/AgentGraph 对接，尚未完成全链路投影。
> 共享定义：`contracts/identifiers.py`、`contracts/errors.py`、`contracts/idempotency.py`。

---

## 1. 身份链

```text
tenant_id
└── workspace_id
    └── project_id
        └── coding_session_id
            └── product_task_id
                └── agent_run_id
                    └── workflow_id + workflow_version
                        └── kernel_task_id
                            └── attempt_id
                                ├── submission_id
                                ├── verification_run_id
                                └── commit_sha
                                    └── delivery_id
```

当前只有部分字段存在：

| 字段 | 当前来源 | 状态 |
|---|---|---|
| tenant_id | AgentGraph `tenants` | 有 |
| workspace_id | AgentGraph / Kernel `workspaces` | 有 |
| project_id | 无 | MISSING |
| repository_id | Kernel `workspaces.repo_path`（非实体） | PARTIAL |
| coding_session_id | 无 | MISSING |
| product_task_id | AgentGraph `collab_tasks.task_key` | 有（key 非 UUID） |
| agent_run_id | 无 | MISSING |
| workflow_id/version | 无 | MISSING |
| kernel_task_id | Kernel `dag_tasks.task_id` | 有 |
| attempt_id | 只有 `retry_count` | PARTIAL |
| submission_id | Kernel `patch_submissions.submission_id` | 有 |
| verification_run_id | 无独立实体 | PARTIAL |
| commit_sha | Kernel `result_commit_sha` / Git | 有 |
| delivery_id | 无 | MISSING |

契约要求：新增对象必须使用 UUID（或明确的稳定 key），不得复用 ID；跨域只通过 ID/事件关联，不跨库 FK。

---

## 2. AgentGraph → Kernel（Admin API）

认证：`X-Admin-Key`；**不得暴露公网**。

| 方法 | 路径 | 用途 | Phase 1A 状态 |
|---|---|---|---|
| POST | `/v1/admin/workspaces` | 创建代码 workspace | 已有 |
| GET | `/v1/admin/workspaces` | 列出 workspace | 已有 |
| POST | `/v1/admin/artifacts` | 注册 stub/test artifact | 已有 |
| GET | `/v1/admin/artifacts` | 列出 artifact | 已有 |
| POST | `/v1/admin/tasks` | 创建 DAG task | 已有 |
| GET | `/v1/admin/tasks` | 列出 task | 已有 |
| GET | `/v1/admin/tasks/{id}` | 读取 task | 已有 |
| **GET** | **`/v1/admin/tasks/{id}/evidence`** | **读取真实 submission/merge/gate 证据** | **Phase 1A 新增** |
| **POST** | **`/v1/admin/tasks/{id}/cancel`** | **取消任务、下游、submission、merge intent** | **Phase 1B 新增** |
| POST | `/v1/admin/tokens` | 签发 runner token；可绑定 `workspace_id` | 已有（Phase 1C 增加绑定） |
| GET | `/v1/admin/tokens` | 列出 token（无 secret） | Phase 1D 新增 |
| POST | `/v1/admin/tokens/{id}/revoke` | 吊销 token、清缓存、释放租约 | Phase 1D 新增 |
| POST | `/v1/admin/tokens/{id}/rotate` | 轮换 secret；旧 secret 立即失效 | Phase 1D 新增 |
| GET/POST | `/v1/admin/metrics` | 指标/reset | 已有 |

### 2.1 Evidence 响应

```json
{
  "task_id": "uuid",
  "task_status": "COMMITTED",
  "result_commit_sha": "40-hex",
  "submission": {
    "submission_id": "uuid",
    "status": "COMMITTED",
    "task_status": "COMMITTED",
    "result_commit_sha": "40-hex",
    "merge_mode": "gate_rerun",
    "gate_results": [{"stage": "WRITE_SET_BOUNDARY", "passed": true, "exit_code": 0}],
    "error_code": null,
    "attempts": 1
  },
  "merge_intent": {
    "merge_id": "uuid",
    "status": "MERGED",
    "merged_commit_sha": "40-hex",
    "gate_results": [],
    "merge_timing": {}
  },
  "gate_results": [],
  "evidence_complete": true
}
```

AgentGraph 只能投影 `submission`/`merge_intent` 的真实 `gate_results`；**禁止**根据 `COMMITTED` 推导 PASS。

---

## 3. Runner → Kernel（syscalls）

认证：`Bearer vnk_...`；Phase 1C 起新 token 可绑定 workspace。

| 方法 | 路径 | 用途 | 状态 |
|---|---|---|---|
| POST | `/v1/syscalls/pull_task_bundle` | claim + 任务包 | 已有 |
| POST | `/v2/syscalls/submit_patch` | 异步提交，202 | 已有 |
| GET | `/v2/submissions/{id}` | 提交终态 | 已有 |
| GET | `/v2/submissions/find` | 按幂等键查询 | 已有 |
| POST | `/v1/syscalls/submit_patch` | 旧同步提交 | 默认 410 |
| POST | `/v1/syscalls/request_task_split` | 拆任务 | 已有 |
| POST | `/v1/syscalls/escalate_contract_defect` | 契约缺陷升级 | 已有 |
| POST | `/v1/syscalls/inject_dependency_task` | 注入依赖任务 | 已有 |
| POST | `/v1/syscalls/query_symbol_signature` | 查询签名 | 已有 |
| POST | `/mcp` | MCP JSON-RPC | 已有；`submit_patch` 仍绑 v1，待改 v2 |

### 3.1 Workspace 绑定（Phase 1C）

- `POST /v1/admin/tokens` 可传 `workspace_id`；响应返回绑定。
- 绑定 token：
  - 只能订阅自己的 `/v1/events/stream` workspace；
  - 只能读/心跳/释放自己的 `/v1/runner/tasks/{id}`；
  - 调 `/v1/syscalls/*` 时若部署 pinned 到其他 workspace → 403。
- 旧 token 的 `workspace_id` 可为 NULL（legacy 全局语义）；生产应重新签发。
- `GET /v1/admin/tokens` 只返回元数据；`revoke` 立即清缓存并释放该 runner 的 LEASED/VERIFYING 任务；`rotate` 返回一次新 secret，旧 secret 立即失效；两者写 `TOKEN_REVOKED`/`TOKEN_ROTATED` 事件（绑定 workspace 时）。
- `VN_REQUIRE_BOUND_TOKENS=true` 时，`workspace_id IS NULL` 的 legacy token 会在认证时被拒绝；默认 false 以保留迁移窗口。
- `/v1/dag*` 需要 `X-Admin-Key`；`/dashboard` 默认关闭，开启后仅 loopback。

---

## 4. 提交与状态契约

### 4.1 v2 submit

```http
POST /v2/syscalls/submit_patch
Authorization: Bearer vnk_...
Idempotency-Key: optional opaque <=200 chars
Content-Type: application/json

{
  "task_id": "uuid",
  "lease_token": "opaque",
  "git_diff": "diff --git ...",
  "touched_symbols": ["src/a.py::f"],
  "local_test_passed": true
}
```

响应：`202 Accepted`（新提交或已完成的重复提交都可能返回 202）。

```json
{
  "schema_version": "v2",
  "submission_id": "uuid",
  "status": "QUEUED",
  "final": false,
  "status_url": "/v2/submissions/uuid"
}
```

**202 不等于完成**；调用方必须轮询 `status_url` 并读 `final/status`。

### 4.2 幂等

- 绑定：`(task_id, lease_token_hash, patch_sha256)`；
- 客户端 `Idempotency-Key` 与绑定冲突 → 409 `idempotency_key_conflict`；
- 同一绑定重复提交 → 返回原结果（`deduplicated=true`）；
- 换 patch/lease 复用 key → 409。

### 4.3 状态

```text
QUEUED -> RUNNING -> COMMITTED | FAILED
             ^          |
             +- requeue +
```

- `final=true` 仅 `COMMITTED|FAILED`；
- 任务取消时 submission 以 `FAILED` + `error_code=cancelled` 结束（schema 暂无 CANCELLED submission 状态）；
- 任务 `dag_tasks.status` 与 submission 状态不是同一个事实。

### 4.4 取消

```http
POST /v1/admin/tasks/{task_id}/cancel
X-Admin-Key: ...
{"reason": "duplicate of another task"}
```

行为：

- 根任务及所有下游依赖任务 → `CANCELLED`；
- 清空 lease；
- QUEUED/RUNNING submission → `FAILED` + `error_code=cancelled`；
- PENDING/RUNNING/REVALIDATE merge intent → `REJECTED` + `error_code=cancelled`；
- 发布 `TASK_CANCELLED`；
- 重复取消幂等；已 COMMITTED/FAILED → 409。

AgentGraph 的取消对 code run 会先调用该接口；调用失败返回 502 `kernel_cancel_failed`，不会只改产品表。

---

## 5. Merge 契约（Phase 1A 修复）

- Merge Coordinator 持有 per-workspace lease + fencing token；
- **新增**：在 Git CAS 前，校验 `dag_tasks.status='VERIFYING'` 且 `lease_token_hash` 与 gate 的 lease 一致，并持有该行锁直到提交；
- 如果任务已被 reaper 重置/重新租用，抛 `MergeFenceLost`，不推进 main；
- 行锁覆盖 Git CAS 与 `on_task_committed`，因此 reaper/重新租用不能在这两者之间插入。

错误：`merge_fence_lost`（见 `contracts/errors.py`）。

---

## 6. 错误码

共享 `ErrorCode` 包括：

- `unauthenticated` / `forbidden` / `tenant_mismatch` / `workspace_mismatch`；
- `task_not_found` / `invalid_state_transition` / `version_conflict`；
- `lease_not_active` / `lease_expired` / `lease_lost` / `fenced_out`；
- `submission_not_found` / `idempotency_key_conflict` / `patch_binding_conflict`；
- `gate_rejected` / `gate_failed` / `gate_evidence_missing`；
- `merge_conflict` / `merge_fence_lost` / `merge_rejected`；
- `delivery_not_ready` / `rate_limited` / `backpressure` / `internal_error`。

HTTP 映射由各服务决定，但同一条件不得在不同服务用不同语义。

---

## 7. 版本与追踪

- API 版本：AgentGraph 已有 `2026-11` 协商；Kernel v2 是提交协议版本；
- 事件版本：`EventEnvelope.event_version`；
- 幂等键：见 `contracts/idempotency.py`；
- 追踪：目标 `request_id -> product_task_id -> ... -> commit_sha -> delivery_id`；当前 Kernel 无 correlation_id，Phase 3 补；
- 重试：区分 Run/Step/DAG Task/Attempt/Submission/Gate/Merge，不能都叫 `retry`。

---

## 8. 未完成（Phase 1B/3）

- 统一 ID 生成/校验接入两个服务；
- `TaskCommandService` / `DagTaskCommandService`；
- 取消传播；
- Kernel token 绑定 workspace；
- `/v1/dag*`、`/dashboard` 鉴权；
- MCP submit 改 v2；
- SSE `seq` 一致性；
- 事件投影/消费者/DLQ；
- code_engine 端到端契约测试。
