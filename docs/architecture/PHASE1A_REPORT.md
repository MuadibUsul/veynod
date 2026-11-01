# PHASE1A_REPORT — 契约基础与 P0 修复

> 日期：2026-10-31
> 范围：Phase 1 的第一批可验收增量（不是 Phase 1 全部完成）
> 状态：**Implemented + tested locally**；未提交 git，未完成取消/统一状态命令层/租户隔离/事件投影
> 现场：Kernel API 已用本目录代码重启（29 routes，含 evidence endpoint）；Gate Worker 已重启（:8030）；`schema_migrations` 已补齐 v4_002–v4_008。AgentGraph 容器尚未重建，code_engine 源码改动未进入运行镜像。

---

## 1. 本次完成

### 1.1 跨域契约包

新增 `contracts/`（无外部依赖，可独立测试）：

- `contracts/identifiers.py`：`IdentityChain`、UUID/commit 校验、correlation ID；
- `contracts/events.py`：`EventEnvelope`、事件类型/producer 注册、版本校验；
- `contracts/errors.py`：跨域 `ErrorCode` 与可重试集合；
- `contracts/idempotency.py`：规范化幂等键、绑定比较；
- `contracts/tests/test_contracts.py`：5 个契约测试。

协议文档：

- `docs/protocols/API_CONTRACTS.md`
- `docs/protocols/EVENT_CONTRACTS.md`

**未接线**：两个服务尚未在请求路径中使用这些定义；这是 Phase 1B/3 的工作。

### 1.2 Kernel merge 任务租约复核（P0）

`veynod_kernel/app/services/verification_gate.py`：

- 新增 `assert_task_mergeable()`：在 Git CAS 前用 `FOR UPDATE` 锁定 `dag_tasks`，要求：
  - `status='VERIFYING'`；
  - `lease_token_hash` 与 gate 的 lease 一致；
- 任务行锁一直持有到 merge 事务提交，`on_task_committed` 在同一事务内执行；reaper/重新租用不能插入。

测试：`tests/test_merge_coordinator.py::test_merge_fence_rejects_reassigned_task`。

### 1.3 真实 gate 证据（P0）

Kernel：

- 新增 `GET /v1/admin/tasks/{task_id}/evidence`（admin key）；
- 返回最新 `patch_submissions`、`merge_intents`、真实 `gate_results`、`merged_commit_sha`、`evidence_complete`。

AgentGraph：

- `KernelEngineClient.evidence(task_id)`；
- `gate_updates_from_evidence()`：只接受 `COMMITTED`/`MERGED` + commit 匹配 + 非空 gate_results；
- `_sync_lifecycle` 不再写死 `counts {passed:4, failed:0}` / `gates_passed:2`；
- 缺少 CI/TESTS 证据、证据 commit 不匹配、任一 gate 失败时，**不写 PASS、不推进 REVIEWING**。

测试：

- `AgentGraph/tests/test_code_engine.py` 5 个用例；
- `tests/test_integration.py::test_full_implementation_flow` 新增 evidence endpoint 断言（5 个 gate stage、merge intent MERGED、evidence_complete）。

### 1.4 生产安全默认值（P0）

`veynod_kernel/app/security.py`：

- `validate_api_production_settings()`：`VN_AUTH_MODE=prod` 必须有 ≥32 位随机 admin key，拒绝已知 dev/default key；
- `validate_worker_production_settings()`：`VN_AUTH_MODE=prod` 默认拒绝 `VN_GATE_SANDBOX=local`；只有显式 `VN_ALLOW_LOCAL_GATE_IN_PROD=true` 才允许（隔离主机场景）。

接入：

- API `app/runtime.py` lifespan；
- Worker `app/worker.py` run()。

测试：`tests/test_security_config.py` 7 个用例。

### 1.5 迁移 ledger（P1）

`sql/v4_006`、`v4_007`、`v4_008` 末尾增加：

```sql
INSERT INTO schema_migrations (version)
VALUES ('v4_00x_...')
ON CONFLICT (version) DO NOTHING;
```

测试：`tests/test_migration_ledger.py` 7 个参数化用例，确保每个 v4_002–v4_008 文件都记录版本。

### 1.6 测试夹具调整（仅测试）

为满足生产安全校验：

- 集成测试 admin key 改为 ≥32 字符；
- 测试 gate worker 显式设置 `VN_ALLOW_LOCAL_GATE_IN_PROD=true`；
- gate worker metrics 端口改为 `0`（避免与线上 8030 冲突）。

这些不降低断言，只让测试配置符合新的生产规则。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| contracts | **5 passed** |
| Kernel 非集成 | **93 passed**（79 原有 + 7 security + 7 ledger） |
| AgentGraph 非集成 | **73 passed, 28 skipped, 448 deselected** |
| Kernel selected integration | **7 passed**（admission / work leases / merge coordinator / edge outbox） |
| Kernel end-to-end merge | **3 passed**（full flow、legacy sync、external conflict） |
| Kernel v2 restart durability | **1 passed** |
| Kernel horizontal API/claim | **1 passed** |
| 合计新增/复跑集成 | **12 passed**（含重复复跑前） |

> 没有跑 AgentGraph 448 个集成测试（缺 pgvector）；没有跑 1,000-runner 容量；没有跑 Docker sandbox。

---

## 3. 修改文件

### 新增

- `contracts/__init__.py`、`identifiers.py`、`events.py`、`errors.py`、`idempotency.py`、`README.md`、`tests/test_contracts.py`
- `veynod_kernel/app/security.py`
- `veynod_kernel/tests/test_security_config.py`
- `veynod_kernel/tests/test_migration_ledger.py`
- `AgentGraph/tests/test_code_engine.py`
- `docs/protocols/API_CONTRACTS.md`
- `docs/protocols/EVENT_CONTRACTS.md`
- `docs/architecture/PHASE1A_REPORT.md`

### 修改

- `veynod_kernel/app/services/verification_gate.py`
- `veynod_kernel/app/api/admin_routes.py`
- `veynod_kernel/app/config.py`
- `veynod_kernel/app/runtime.py`
- `veynod_kernel/app/worker.py`
- `veynod_kernel/sql/v4_006_admission_permits.sql`
- `veynod_kernel/sql/v4_007_unlogged_admission_permits.sql`
- `veynod_kernel/sql/v4_008_active_lock_indexes.sql`
- `veynod_kernel/tests/test_merge_coordinator.py`
- `veynod_kernel/tests/test_integration.py`
- `veynod_kernel/tests/test_horizontal_api.py`
- `veynod_kernel/tests/test_submission_restart_e2e.py`
- `AgentGraph/app/services/code_engine.py`

### 未提交

- `code_engine.py` 仍是 AgentGraph git 的未跟踪文件；本轮没有执行 `git add/commit`。
- 新增 contracts/docs 也未提交（根目录不是 git 仓库）。

---

## 4. 还没有完成（不要误报）

### P0 仍开放

1. **取消**：Phase 1B 已实现 Kernel CANCELLED + 取消 API + AgentGraph 传播（见 `PHASE1B_REPORT.md`）；仍缺 in-flight sandbox 主动终止和取消事件投影。
2. **统一状态命令层**：AgentGraph 9+、Kernel 6+ 写入方仍未收口。
3. **租户隔离**：Kernel token 不绑 workspace；DAG/dashboard 未鉴权。
4. **沙箱**：生产 guard 只拒绝 `local`；Docker 镜像/非 root/网络策略/测试仍未完成。
5. **AgentGraph 端到端 code_engine 测试**：只有 pure mapping 单测；没有 AgentGraph DB + Kernel 的集成测试。
6. **事件投影**：Kernel→AgentGraph 仍靠 GET 轮询；没有 consumer/projection/DLQ。
7. **SSE seq**：AgentGraph `/v1/stream` 仍用 `row.id`。

### Phase 1 其余

- ID/契约接入两个服务的请求路径；
- TaskCommandService / DagTaskCommandService；
- 取消/重试/超时语义；
- MCP submit 改 v2；
- token revoke/rotate；
- 跨服务 contract tests（HTTP 级）。

---

## 5. 下一步（Phase 1B）

按依赖顺序：

1. 把 `contracts` 接入 Kernel submission/event 生产路径和 AgentGraph projection；
2. 建 `DagTaskCommandService`，先覆盖 cancel/heartbeat/merge 写路径；
3. 增加 Kernel cancel API + CANCELLED 状态 + worker/sandbox 停止；
4. 建 AgentGraph `TaskCommandService`，收口 9+ 写路径；
5. 修 SSE seq 与事件 envelope；
6. Kernel token 绑定 workspace、DAG/dashboard 鉴权；
7. code_engine 端到端测试（AgentGraph DB + 真实 Kernel admin）；
8. 再进入 Phase 2 的 Agent Runtime。

---

## 6. 结论

本轮把四个“会直接造成错误交付/安全问题”的点修掉了：

- merge 不再能在 task lease 失效后推进 main；
- AgentGraph 不再伪造 gate PASS，只投影真实证据；
- Kernel 生产启动不再静默接受弱 key / local gate；
- 迁移版本表能记录 006–008。

但 Phase 1 **没有完成**，Phase 2 更不能开始。下一步是统一状态命令层和取消传播。
