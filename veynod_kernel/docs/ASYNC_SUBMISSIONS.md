# 版本化异步补丁提交（v2）

> 日期：2026-10-31  
> 目标：`POST` 只做租约/写集校验 + 不可变补丁持久化并返回 202；pytest/Git 合并由持久化独立 Gate Worker 执行，进程重启后可恢复。  
> 保留：Rust 调度核心、写入集锁代数、Git `update-ref <new> <old>` 条件更新语义。  
> 数据库迁移：[`sql/v4_002_patch_submissions.sql`](../sql/v4_002_patch_submissions.sql)

---

## 1. 接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| POST | `/v2/syscalls/submit_patch` | 接受提交，返回 **202**、`submission_id`、`status_url`、`patch_sha256` |
| GET | `/v2/submissions/{submission_id}` | 查询最终状态（仅创建该提交的 Runner token） |
| GET | `/v2/submissions/find?idempotency_key=...` | 按幂等键查询；响应丢失时使用 |
| POST | `/v1/submissions` | 兼容别名（202，等价 v2） |
| GET | `/v1/submissions/...` | 兼容别名 |
| POST | `/v1/syscalls/submit_patch` | **已弃用**；默认返回 410 + 迁移指引 |

### 1.1 202 响应

```json
{
  "schema_version": "v2",
  "submission_id": "…",
  "task_id": "…",
  "status": "QUEUED",
  "status_url": "/v2/submissions/…",
  "idempotency_key": "…",
  "patch_sha256": "…",
  "derived_idempotency_key": false,
  "deduplicated": false,
  "accepted_at": "2026-10-31T…"
}
```

### 1.2 状态响应

`status` 为 `QUEUED | RUNNING | COMMITTED | FAILED`；`final` 为终态标志。终态包含：

- `task_status`：门禁返回的 `COMMITTED / FAILED / LEASED`；
- `result_commit_sha`、`merge_mode`、`gate_results`、`merge_timing`、`unlocked_downstream_task_ids`；
- `error_code`（`lease_expired`、`lease_lost`、`gate_rejected`、`gate_failed`、`validation_error`、`interrupted_while_verifying`、`internal_error` 等）与 `error_detail`；
- `attempts`、`created_at`、`started_at`、`completed_at`。

非终态响应带 `Retry-After: 1`。

---

## 2. 幂等键绑定

- 客户端可用 `Idempotency-Key` header 或 body `idempotency_key`；两者同时出现且不同 → 422。
- 服务端计算 `patch_sha256 = SHA-256(git_diff 原文)`，并把键绑定到：
  - `task_id`
  - `runner_id` + `lease_token_hash`（租约身份）
  - `patch_sha256`
- 数据库约束：
  - `UNIQUE(idempotency_key)`
  - `UNIQUE(task_id, lease_token_hash, patch_sha256)`
- 规则：
  - 同键 + 同绑定 → 返回**同一** `submission_id`，`deduplicated=true`，不产生第二次可见结果；
  - 同键 + 不同 task/lease/patch → **409 `idempotency_key_conflict`**；
  - 同绑定 + 不同键 → 也返回已有提交（`deduplicated_by_binding=true`）；
  - 未提供键时，服务端从上述材料派生确定性键（`derived_idempotency_key=true`）。
- 持久化内容（`git_diff`、`touched_symbols`、`local_test_passed`、身份字段）由数据库触发器 `patch_submissions_immutable` 保护，UPDATE 直接报错；状态/结果字段可更新。

---

## 3. 事务与执行边界

### 接受路径（必须快）

1. 认证 Runner，`verify_active_lease` 校验 `LEASED`、token hash、runner、未过期；
2. 符号/文件写集校验；
3. INSERT 不可变 submission（一次短事务）→ 202；
4. **不**运行 pytest/Git，不把请求绑定到 gate 生命周期。

### 独立 Gate Worker

1. **API 进程不运行 pytest/Git**；Gate 由独立进程 `python -m app.worker` 执行。
2. `FOR UPDATE SKIP LOCKED` 原子领取一行 `QUEUED`（或 work lease 已过期的 `RUNNING`），写入随机 `work_lease_id` 并立即提交；
3. 新会话重新校验租约（状态、hash、runner、`lease_expires_at`）；
4. 新会话调用原门禁；门禁的 `LEASED → VERIFYING` 条件更新包含 `lease_expires_at > NOW()`；**在 pyright/ruff/pytest 前 commit 元数据读取**（`verification_gate` 已补 `session.commit()`）；
5. 心跳每 `VN_WORK_HEARTBEAT_SECONDS` 续租；结果 UPDATE 必须匹配 `status='RUNNING' AND work_lease_id=当前租约`，旧 worker 无法覆盖新结果；
6. 门禁完成后再用独立事务写入 `COMMITTED / FAILED`、结果与 `completed_at`；
7. 门禁拒绝（`LEASED`）时 worker 释放租约（不重复记 retry），submission 记 `FAILED/gate_rejected`；
8. 临时内部错误按指数退避重新入队，最多 `VN_WORK_MAX_ATTEMPTS`；崩溃进程的租约过期后可被新 worker 重新领取。

任何 pytest/Git 长操作期间都不持有数据库事务；短读事务在进入 Git/子进程前提交。

### 重启/崩溃恢复

Worker 的 claim 查询会领取 `QUEUED` 以及 work lease 已过期的 `RUNNING`；随后按任务状态收敛：

| 任务状态 | 处理 |
| :--- | :--- |
| `COMMITTED` | submission → `COMMITTED` |
| `FAILED` | submission → `FAILED/task_failed` |
| `LEASED` 且租约有效 | 继续处理（旧 `RUNNING` 行已被 claim 续领） |
| `LEASED` 且已过期 | `FAILED/lease_expired`，任务释放回 READY |
| `VERIFYING` | `FAILED/interrupted_while_verifying`（不自动重放门禁，避免重复合入） |
| 其他 | `FAILED/lease_lost` |

因此“202 后立即重启”会恢复 QUEUED 行并最终提交；若重启发生在门禁已进入 VERIFYING 之后，至少会得到明确的 FAILED，而不是永久悬挂。

API 进程不提供启动/恢复 gate 的端点。Gate worker 是独立进程：

```bash
python -m app.worker --workers 8 --database-url postgresql+asyncpg://...
```

---

### 3.1 Agent 执行隔离

Gate 在独立 worker 中通过 [`app/services/gate_sandbox.py`](../app/services/gate_sandbox.py) 执行：

- `VN_GATE_SANDBOX=docker`（生产推荐）：worktree 是唯一挂载；`--network none`、只读根文件系统、tmpfs `/tmp`、CPU/内存/PID 限制、`no-new-privileges`；不传入 `DATABASE_URL`/admin key/token。
- `VN_GATE_SANDBOX=local`（开发/CI）：环境变量 allowlist（拒绝所有数据库/密钥），每次命令独立 HOME/TMP；POSIX 上设置 CPU/AS/NPROC/FSIZE rlimit 与超时进程树终止。**local 不是安全沙箱，生产必须用 Docker 镜像。**

Git、pyright、ruff、pytest 都在该隔离环境中运行；worker 在这些操作期间不持有数据库连接。

### 3.2 服务端独立核验修改范围

`AST_SCOPE_DIFF` 门禁阶段从 base commit 与 worktree 的实际文件做 `file_snapshot` 对比，**不信任 Runner 的 `touched_symbols`**：

- 实际改变的 symbol 必须被 task 的 `write_locks` 覆盖；
- 新增/删除 symbol、文件骨架/import/global/signature/docstring 变化，必须有对应**文件级** `write_locks`；
- 非 Python 文件变化必须有文件级权限；
- 任何越界/伪造 `touched_symbols` 的补丁在 Git apply 后、合入前被拒绝，Git HEAD 不变。

---

## 4. 数据库迁移

```bash
# 正常启动自动执行 genesis + 所有 v4_*.sql（幂等）
python -m uvicorn app.main:app ...

# VN_AUTO_INIT_DB=false 的部署用显式命令
python scripts/migrate_db.py --database-url postgresql+asyncpg://...
```

- `v4_002_patch_submissions.sql` 只新增 `schema_migrations` / `patch_submissions` / 索引 / 不可变触发器；不改既有表。
- 已有 v4 数据库重启即可升级；无需清空数据。
- 回滚：旧代码不会读新表；但为安全，生产回滚前先停止 v2 提交并等待 `QUEUED/RUNNING` 清空。

### 旧接口迁移

`POST /v1/syscalls/submit_patch` 一律返回 **410 Gone**，body 含 v2 端点、状态端点、按 key 查询端点和幂等 header。
同步回退开关 `VN_LEGACY_SUBMIT_MODE` 已于 2026-11-04 删除。

Node Runner（已删除，ADR-017）当时切到 v2。提交方在提交前计算：
`idempotency_key = SHA-256(task_id + lease_token + patch_sha)`；POST 失败/响应丢失时先
`GET /v2/submissions/find?idempotency_key=...`，再重试同一 POST；获得 202 后轮询 `status_url` 直到 `final=true`。超时默认 900s（`VEYNOD_SUBMISSION_TIMEOUT_SECONDS` / `--submission-timeout`）。

---

## 5. 测试

```bash
# 全部集成（真实 Postgres + Git + pytest + TestClient）
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/<fresh-test-db> \
  pytest -q tests/test_integration.py

# 真实 uvicorn 进程重启 + 客户端断线
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/<fresh-test-db> \
  pytest -q tests/test_submission_restart_e2e.py
```

覆盖：

- 同键重复提交 → 同一 `submission_id`，数据库只有一行；
- 同键不同补丁 → 409；
- 客户端在 202 后断开 → 服务端继续处理，重启后恢复；
- 租约在 worker 领取前过期 → `FAILED/lease_expired`，任务回 READY；
- **真实 uvicorn 进程**接受 202 后被杀 → 新进程恢复 QUEUED 行并 COMMITTED；
- 数据库触发器拒绝修改 `git_diff`；
- 旧 `/v1/syscalls/submit_patch` → 410 迁移提示，任务保持 LEASED。

本轮实际结果：`tests/test_integration.py` **17 passed**；`tests/test_submission_restart_e2e.py` **1 passed**；全量非 integration **77 passed**；ruff 通过。

### 5.1 100-task v2 benchmark（新协议客户端）

结果文件：`.run/bench/v3-100/20261031T192550Z-cross-file.json`（通过后删除隔离库）。

| 指标 | 实测 |
| :--- | ---: |
| 任务 / Runner / SSE | 100 / 100 / 0 |
| 总耗时 / setup / load / drain | 71.0s / 7.6s / 62.2s / 1.2s |
| 数据库最终状态 | **COMMITTED 100 / 100** |
| 退出码 | **0** |
| submit accept p50 / p95 / p99 | **1.45s / 8.61s / 10.37s**（202 不等待门禁） |
| 提交终态 p50 / p95 / p99 | **33.0s / 57.1s / 57.8s** |
| worker gate pool wait p50 / p95 | 265.5ms / 407.7ms（8 workers 与 8 槽位匹配） |
| submit HTTP 错误 / 超时 / 重试 | 0 / 0 / 0 |
| API/worker 数据库连接池超时 | 0 / 0 |

对照：同一拓扑把 worker 设为 2 时总耗时 **171s**，说明 `VN_SUBMISSION_WORKERS` 必须与门禁并发/CPU 配额一起调；异步化只消除了 API 等待，不会自动提高 gate 吞吐。

### 5.2 1000 Runner / 1000 task / 1000 SSE（v2，300s claim 窗口）

结果文件：`.run/bench/v4-1000/20261031T200213Z-cross-file.json`；**exit 1**（容量未达标）。

| 指标 | 实测 |
| :--- | ---: |
| setup / load / drain / acceptance | 55.1s / 307.6s / 89.1s / 396.7s |
| 202 接受 / 最终提交 | **1000 / 415** |
| 任务最终状态 | COMMITTED 415 / LEASED 577 / VERIFYING 8 |
| 无终态任务 | **585 / 1000** |
| 失败的 submission | **0** |
| accept 延迟 p50 / p95 / p99 | **29.2s / 54.0s / 88.7s** |
| 最终状态等待 p50 / p95 | 173.9s / 309.1s（仅 335 个客户端观察到终态） |
| worker gate pool wait p50 / p95 | 562ms / 718ms |
| merge total p50 / p95 | 328ms / 467ms |
| SSE opens / frames / bytes / 错误 | 1000 / 1,812,801 / 553MB / 0 |
| server submit.started / finished | 423 / 415（8 个仍在门禁） |

**结论**：v2 + 独立 Worker + MergeCoordinator 正确地把 **1000 个提交全部持久化**，未完成提交处于明确的 QUEUED/LEASED/VERIFYING，且零 submission 失败。但 300s 窗口内只有 415 个 COMMITTED：门禁吞吐/CPU 仍是瓶颈，且 1000 并发 accept 自身 p50 29.2s。要满足“1000 Runner 稳定服务”，需要提高 gate 执行吞吐与 accept 路径容量，而不是继续调整客户端超时。

---

## 6. 尚未解决的风险

1. **VERIFYING 中断不自动重放**：重启发生在门禁已把任务置为 VERIFYING 后，submission 会被标为 `FAILED/interrupted_while_verifying`，任务留给 reaper。这是有意的安全选择，但需要运维把该任务重置后重新提交。
2. **多个 worker 的 gate 并发**：`VN_SUBMISSION_WORKERS` 默认 8，实际吞吐仍受 `VN_GATE_WORKTREE_POOL_SIZE` 和 CPU 限制；1000 Runner 的容量瓶颈没有因协议异步化消失，只是客户端不再因读超时而丢失结果。
3. **幂等键是全局唯一**：不同 Runner 若故意复用同一键会得到 409；客户端应使用来自 task/lease/patch 的确定性键。
4. **旧接口 `sync` 仍是阻塞路径**：只保留迁移窗口；`reject` 是默认。
5. **Worker 进程内调度**：单 uvicorn 进程内的 asyncio worker；多进程部署时每个进程都会竞争 `SKIP LOCKED`，这是安全的，但需要观察总并发和 gate 池争用。
6. **状态端点只允许创建者 Runner 读取**：管理员目前需通过数据库/后续管理查询排障；未增加管理员状态 API。
