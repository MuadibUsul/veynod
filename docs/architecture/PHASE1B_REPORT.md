# PHASE1B_REPORT — 取消闭环与状态语义

> 日期：2026-10-31
> 范围：Phase 1B 的第一批增量：Kernel/AgentGraph 取消闭环
> 状态：本地实现 + 测试通过；未提交 git；AgentGraph 运行镜像未重建
> 现场：Kernel API 已重启（30 routes，含 cancel/evidence）；`schema_migrations` 已有 v4_002–v4_009；`task_status` 枚举已含 CANCELLED；Gate Worker 已重启（:8030）。AgentGraph 容器仍未重建，产品侧取消传播只在源码中。

---

## 1. 本次完成

### 1.1 Kernel 新增 CANCELLED 状态

- `TaskStatus` 增加 `CANCELLED`（`app/models.py`、`app/mcp_server/syscalls.py`）；
- 新迁移 `sql/v4_009_cancellation.sql`：
  - `ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'CANCELLED'`；
  - `dag_tasks` 增加 `cancelled_at`、`cancel_reason`；
  - 写入 `schema_migrations`。
- `CANCELLED` 在取消命令中被视为终态，不会被 retry/merge。

### 1.2 Kernel 取消命令

新增 `app/engine/task_commands.py`：

- `cancel_task(session, task_id, reason, actor)`：
  - `FOR UPDATE` 锁根任务；
  - 递归取消所有下游依赖任务；
  - 已 CANCELLED → 幂等返回；
  - COMMITTED/FAILED/SPLIT_ARCHIVED → 409 冲突；
  - 清空 lease；
  - 将 QUEUED/RUNNING submission 置为 `FAILED` + `error_code=cancelled`；
  - 将 PENDING/RUNNING/REVALIDATE merge intent 置为 `REJECTED` + `error_code=cancelled`；
  - 发布 `TASK_CANCELLED` 事件；
  - 同步 Rust SchedulerCore。
- Admin API：`POST /v1/admin/tasks/{task_id}/cancel`（X-Admin-Key）。

### 1.3 防止取消后被合并

- Gate Worker：
  - 任务已 `CANCELLED` → submission 直接 `FAILED/task_cancelled`；
  - Gate 跑完后、创建 merge intent 前再次检查任务状态，CANCELLED 则不再创建 intent。
- Merge 临界区：
  - `assert_task_mergeable` 要求 `status='VERIFYING'`；CANCELLED/LEASED/FAILED 一律 `MergeFenceLost`，不推进 Git。

### 1.4 AgentGraph 取消改为 CANCELLED 并传播

- `AgentGraph/app/services/tasks.py`：
  - 取消状态从 `FAILED` 改为 `CANCELLED`；
  - 对 `input_context.code_engine == "veynod-kernel"` 的任务，先调用 Kernel 取消；失败则返回 502 `kernel_cancel_failed`，不把产品任务标记为取消。
- `AgentGraph/app/services/code_engine.py`：
  - `KernelEngineClient.cancel_task()`；
  - `cancel_kernel_run(task, reason)`。
- 错误码 `kernel_cancel_failed` 已注册并重新生成错误文档/Agent Guide。
- 4 个原有测试的取消期望从 `FAILED` 更新为 `CANCELLED`。

### 1.5 契约更新

- `contracts/events.py` 增加 `KernelTaskCancelled` 事件类型。
- `docs/protocols/API_CONTRACTS.md` 增加 cancel endpoint/状态说明。
- `docs/protocols/EVENT_CONTRACTS.md` 增加取消事件映射。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| Kernel 非集成 | **94 passed**（含 v4_009 ledger test） |
| AgentGraph 非集成 | **75 passed, 28 skipped, 448 deselected** |
| contracts | **5 passed** |
| Kernel 最终端到端回归 | **9 passed**：merge coordinator（3）、full flow、cancel、legacy sync、external conflict、restart、horizontal |
| Kernel 队列/租约回归 | 之前 **4 passed**：admission、work leases（2）、edge outbox |
| 合计相关集成场景 | **13 passed** |

取消集成测试覆盖：

- 取消 READY 任务 → `CANCELLED`；
- 下游 BLOCKED 任务一并 `CANCELLED`；
- 重复取消幂等（`idempotent=true`）；
- 管理员取消接口可用。

---

## 3. 尚未完成/已知限制

1. **正在运行的 sandbox/gate 进程不会被主动杀死**：取消后不会创建新 merge intent，merge 会被 fence 拒绝；但已启动的 pytest 可能跑到超时/结束。
2. **AgentGraph 运行镜像未重建**：源码已改，线上 AgentGraph 仍是旧镜像；重建后才能让产品取消调用 Kernel。
3. **CANCELLED 在 AgentGraph DB trigger 中还不是 terminal**：`trg_enforce_task_immutability` 仍只保护 DONE/FAILED；后续迁移需补。
4. **取消的跨域一致性**：AgentGraph 先调 Kernel，再改本地状态；若本地提交失败，Kernel 已取消但产品未取消，需要补偿/对账。
5. **没有取消事件投影到 AgentGraph**：Kernel `TASK_CANCELLED` 目前不会自动更新产品任务。
6. **submission 仍用 FAILED + error_code=cancelled**，没有独立 CANCELLED 提交状态（schema check 限制）。
7. **没有 commit**；所有改动未提交 git。

---

## 4. 下一步（Phase 1C）

1. 在 AgentGraph DB 迁移中把 CANCELLED 设为 terminal；
2. Kernel cancel 事件投影到 AgentGraph（Phase 3 的预演）；
3. 统一 DagTaskCommandService 的其余命令（claim/release/commit 等）；
4. 租户隔离 + DAG/dashboard 鉴权；
5. AgentGraph 镜像重建与 code_engine 端到端测试；
6. 主动终止 in-flight sandbox。

---

## 5. 结论

取消已经不是“只改产品表”：Kernel 有 CANCELLED、取消 API、下游传播、submission/intent 终止、merge fence；AgentGraph 取消会调用 Kernel 并改为 CANCELLED。**但跨域投影、sandbox 主动终止和运行镜像重建仍未完成。**
