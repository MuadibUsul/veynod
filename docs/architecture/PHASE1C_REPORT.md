# PHASE1C_REPORT — 租户边界与终态约束

> 日期：2026-10-31
> 范围：Phase 1C 第一批：Kernel token workspace 绑定、debug 面鉴权、AgentGraph CANCELLED 终态
> 状态：本地实现 + 测试通过；未提交 git；AgentGraph 运行镜像未重建

---

## 1. 本次完成

### 1.1 Kernel runner token 绑定 workspace

- 迁移 `v4_010_token_workspace.sql`：
  - `agent_tokens.workspace_id`（nullable，ON DELETE CASCADE）；
  - 索引 + `schema_migrations`。
- `app/models.py`：`AgentToken.workspace_id`。
- `app/auth.py`：
  - `RunnerIdentity(worker_id, workspace_id)`；
  - `authenticate_worker_context()` 返回 token 的 workspace；
  - 缓存保存 identity；
  - `register_token` / `issue_token` 支持 workspace 绑定；未显式指定时使用 `VN_DEFAULT_WORKSPACE_ID`（仅当 workspace 存在）。
- Admin `POST /v1/admin/tokens` 接受 `workspace_id`，返回绑定信息。
- 旧 token（workspace_id NULL）保持兼容；新 token 优先绑定。

### 1.2 Runner 读取面隔离

- `/v1/runner/tasks/{task_id}`：token 绑定 workspace 与 task 不一致 → 404（不泄露存在性）。
- `/v1/runner/heartbeat`、`/v1/runner/release`：同样检查 task workspace。
- `/v1/events/stream`：绑定 token 只能订阅自己的 workspace；请求其他 workspace → 403。
- `/v1/syscalls/*`：单 workspace 平面继续使用 pinned/oldest workspace；绑定到其他 workspace 的 token → 403。
- `/v1/runner/ping` 返回 `workspace_id`。

### 1.3 Debug 面鉴权

- `/v1/dag`、`/v1/dag/plan`、`/v1/dag/events` 现在要求 `X-Admin-Key`；未授权 → 401。
- `/dashboard`：
  - 默认 `VN_DEBUG_DASHBOARD=false` → 404；
  - 开启后仅允许 loopback 客户端；
  - 仍不是产品入口。
- `/healthz` 保持无鉴权（liveness）。

### 1.4 AgentGraph CANCELLED 终态

- 新迁移 `v3_68_cancelled_terminal.sql`：
  - `enforce_task_immutability()` 把 `CANCELLED` 与 `DONE`/`FAILED` 一样视为 terminal；
  - 取消后不能改 status/acceptance/description。
- `_INCREMENTAL_MIGRATIONS` 增加 `3.68.0`，`CURRENT_SCHEMA_VERSION` 随之为 3.68.0。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| Kernel 非集成 | **95 passed**（含 v4_010 ledger） |
| AgentGraph 非集成 | **75 passed, 28 skipped, 448 deselected** |
| contracts | **5 passed** |
| Kernel 新增集成 | **2 passed**：workspace 绑定/debug auth、完整实现流程（DAG plan 带 admin key） |
| Kernel 最终回归 | **5 passed**：restart、horizontal、merge coordinator |
| 之前相关集成 | 13 passed（merge/取消/legacy/external/restart/horizontal/admission/work/edge） |

workspace 绑定测试覆盖：

- 未鉴权 `/v1/dag` → 401；
- `/dashboard` 默认 → 404；
- 绑定 token A 的 ping 返回 workspace A；
- token B 读取 workspace A 的 task → 404；
- token A 订阅 workspace B 的 SSE → 403；
- token B 调 syscall plane（pin 在 workspace A）→ 403。

---

## 3. 修改文件

### 新增

- `veynod_kernel/sql/v4_010_token_workspace.sql`
- `AgentGraph/sql/migrations/v3_68_cancelled_terminal.sql`
- `docs/architecture/PHASE1C_REPORT.md`

### 修改

- `veynod_kernel/app/auth.py`
- `veynod_kernel/app/models.py`
- `veynod_kernel/app/api/admin_routes.py`
- `veynod_kernel/app/api/runner_routes.py`
- `veynod_kernel/app/api/dag_routes.py`
- `veynod_kernel/app/mcp_server/syscalls.py`
- `veynod_kernel/app/config.py`
- `veynod_kernel/tests/test_integration.py`
- `veynod_kernel/tests/test_migration_ledger.py`
- `AgentGraph/app/database.py`
- `docs/protocols/API_CONTRACTS.md`
- `docs/architecture/CURRENT_STATE.md`（Phase 1 update 标注）

---

## 4. 尚未完成

1. **取消事件投影**：Kernel `TASK_CANCELLED` 仍未自动更新 AgentGraph 产品任务。
2. **in-flight sandbox 主动终止**：取消只阻止 merge，不杀正在跑的 pytest。
3. **token revoke/rotate/list API**：当前只有创建；泄露后仍要改 DB。
4. **legacy 未绑定 token**：workspace_id NULL 的旧 token 仍按旧的全局语义工作；生产应要求重新签发。
5. **多 workspace syscall**：仍是单 workspace pinned；token 绑定只是拒绝不匹配，不是多租户调度。
6. **AgentGraph 镜像重建**：v3_68 和 code_engine 改动未进入运行镜像。
7. **AgentGraph 集成测试**：仍受 pgvector 缺失阻塞；v3_68 未在完整 AgentGraph 集成套件中执行。
8. **没有 commit**。

---

## 5. 下一步（Phase 1D）

1. 取消事件投影到 AgentGraph（Kernel Outbox → AgentGraph projection）；
2. in-flight sandbox 主动终止（gate worker 轮询取消标志/终止进程树）；
3. token revoke/rotate/list + 生产禁止未绑定 token；
4. AgentGraph 镜像重建 + code_engine 端到端测试；
5. 统一 DagTaskCommandService 的其余命令（claim/release/commit）；
6. 多 workspace/Repository Registry 设计。

---

## 6. 结论

Phase 1C 把 Kernel 的读面从“任何有效 runner 可读任何 workspace/task”收紧为“绑定 token 只能读自己的 workspace”，并把 debug DAG 面移到 admin key 后面、默认关闭 dashboard；同时 AgentGraph 的 CANCELLED 成为数据库终态。**这仍不是完整多租户：syscall 平面还是单 workspace，旧 token 仍未绑定。**
