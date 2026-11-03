# 独立门禁 Worker：工作租约、隔离执行与 AST 范围核验

> 日期：2026-10-31  
> 目标：API 进程只接受/查询补丁；pytest、pyright、ruff、Git 在独立 Worker 中执行。  
> 迁移：[`sql/v4_003_gate_work_leases.sql`](../sql/v4_003_gate_work_leases.sql)  
> 保留：Rust `SchedulerCore`、写集锁代数、Git `update-ref <new> <old>` CAS 语义。

---

## 1. 进程拆分

```text
Runner
  -> API: POST /v2/syscalls/submit_patch  (短事务, 202)
  -> API: GET  /v2/submissions/{id}

Gate Worker (python -m app.worker)
  -> claim patch_submissions (SKIP LOCKED + work_lease_id)
  -> renew work lease heartbeat
  -> revalidate task lease
  -> Gate: AST scope diff -> contracts -> typecheck/lint -> pytest
  -> persist merge_intent (verified task commit + gate_results)
Per-repository MergeCoordinator
  -> fenced ordered Git update-ref CAS
  -> DB finalize + fenced intent result
```

API lifespan **不再启动** submission worker，也不预热门禁 worktree 池。Gate Worker 是独立进程，可横向启动多个；`SKIP LOCKED` 保证一个 submission 同时只有一个 work lease。

---

## 2. 工作租约与 fencing

`patch_submissions` 新增：

- `work_lease_id UUID`：每次 claim 生成新 UUID；
- `work_owner`：worker 标识；
- `work_lease_expires_at`：TTL，默认 `VN_WORK_LEASE_SECONDS=300`；
- `available_at`：退避重试的最早领取时间；
- `last_worker_error`：最近一次临时错误。

### claim

`QUEUED AND available_at <= NOW()` 或 work lease 已过期的 `RUNNING`：

```sql
WITH next AS (
  SELECT submission_id FROM patch_submissions
  WHERE (status='QUEUED' AND available_at <= NOW())
     OR (status='RUNNING' AND work_lease_expires_at < NOW())
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE patch_submissions p
SET status='RUNNING', attempts=attempts+1,
    work_lease_id=:new_lease, work_owner=:worker,
    work_lease_expires_at=NOW()+make_interval(secs=>:ttl)
FROM next WHERE p.submission_id=next.submission_id
RETURNING p.*;
```

### 续租

每 `VN_WORK_HEARTBEAT_SECONDS=30` 调用：

```sql
UPDATE patch_submissions
SET work_lease_expires_at=NOW()+...
WHERE submission_id=:id AND status='RUNNING' AND work_lease_id=:lease
RETURNING submission_id;
```

返回 0 → 本 worker 已失去租约；此后不再合入。

### 退避/重试

临时内部错误：

```sql
UPDATE ... SET status='QUEUED',
  available_at=NOW()+exponential_backoff,
  work_lease_id=NULL, work_lease_expires_at=NULL
WHERE submission_id AND status='RUNNING' AND work_lease_id=:lease;
```

最多 `VN_WORK_MAX_ATTEMPTS=5`；超过后 submission 明确 `FAILED/work_retries_exhausted`。

### 崩溃与旧 Worker

- Worker 崩溃 → work lease 到期 → 新 Worker claim；任务仍是 `LEASED` 则重跑门禁，已是 `VERIFYING` 则 submission 明确 `FAILED/interrupted_while_verifying`（安全选择，不自动重放合入）。
- 旧 Worker 恢复后：
  - 所有结果 UPDATE 都带 `AND work_lease_id=:lease`；旧租约写入影响 0 行；
  - 门禁在进入 merge 前和 `update-ref` 前再次检查 work lease；失去租约抛 `MergeFenceLost`，不执行 Git CAS；
  - 因此旧 Worker 不能覆盖新 Worker 的 submission 终态。极窄的“检查后 TTL 恰好到期”窗口由 2 秒安全余量 + 心跳降低；仍建议监控 `fenced` 警告。

---

## 3. 数据库连接边界

- Worker claim / 续租 / 重新入队 / 最终结果都是独立短事务。
- 进入 Git/类型检查/lint/pytest 前：
  - submission 状态已 commit；
  - 门禁 transition 已 commit；
  - `AST_SCOPE_DIFF`、`CONTRACT_HASH_GUARD` 所需 DB 读取已 commit；
  - `UNIT_TEST_SUITE` 的测试文件列表读取已 commit。
- 沙箱命令与 Git 子进程执行期间不持有 SQLAlchemy 连接/事务。
- API 只做 accept/status 查询，不运行 pytest/Git。

---

## 4. Agent 代码隔离

[`app/services/gate_sandbox.py`](../app/services/gate_sandbox.py)

### Docker（生产推荐）

`VN_GATE_SANDBOX=docker` + `VN_GATE_SANDBOX_IMAGE=<含 pyright/ruff/pytest 的镜像>`：

Compose 部署还需把宿主机仓库目录设为绝对路径 `REPOS_DIR`，把
`VN_GATE_DOCKER_USER` 设为该目录所有者的 UID:GID，把
`VN_GATE_DOCKER_GROUP_GID` 设为宿主 docker 组的数字 GID
（`stat -c %g /var/run/docker.sock`）。Gate Worker 以该用户运行，这样它创建
的 worktree 归同一 UID 所有，子容器才能写入；docker CLI 经该组访问挂载的
socket。Gate Worker 挂载 Docker socket，并把 `/repos` 下的单个 worktree 映射
给无网络、只读根文件系统的子容器；Kernel API 不挂载 Docker socket。

- `--network none`；
- worktree 是唯一挂载（`/workspace`）；
- 只读根文件系统 + `--tmpfs /tmp`；
- `--cpus`、`--memory`、`--pids-limit`；
- `--cap-drop ALL`、`no-new-privileges`；
- 不传入 `DATABASE_URL`、`VN_ADMIN_API_KEY`、runner token。

### local（开发/CI）

`VN_GATE_SANDBOX=local`：

- 环境变量 allowlist；明确剔除 `DATABASE_URL` 和所有 token/admin/secret；
- 每次命令独立 `HOME`/`TEMP`；
- POSIX `RLIMIT_CPU/AS/NPROC/FSIZE/CORE`；
- 墙钟超时 + 进程树终止；
- **不是完整安全沙箱**；生产必须 Docker。

测试 `test_gate_sandbox_hides_database_and_admin_credentials` 在真实 pytest 子进程中断言 `DATABASE_URL` / `VN_ADMIN_API_KEY` / `VN_BOOTSTRAP_RUNNER_TOKEN` 不存在。

---

## 5. 服务端独立 AST 范围核验

新门禁阶段 `AST_SCOPE_DIFF`（`GateStage` 新增值，纯增量）：

1. 从 base commit 读原始文件，从 worktree 读补丁后文件；
2. 两边调用 `Slicer.file_snapshot`，比较：
   - 文件骨架哈希；
   - 每个 symbol 的 `source_hash`；
   - symbol 新增/删除。
3. 规则：
   - 实际改变的 symbol 必须在 `task.write_locks` 覆盖范围内；
   - 文件骨架/import/global/signature/docstring 变化、symbol 新增/删除 → 必须有 **文件级** `write_locks`（如 `src/a.py`，不是 `src/a.py::f`）；
   - 非 Python/不可解析文件 → 必须有文件级权限；
   - Runner 的 `touched_symbols` 只作为早期反馈，**不参与最终范围判定**。
4. 失败在 Git apply 后、merge 前返回 `AST_SCOPE_DIFF` 失败；HEAD 不变。

---

## 6. 迁移与运行

```bash
# 迁移（也可由 API/worker 启动时自动执行，幂等）
python scripts/migrate_db.py --database-url postgresql+asyncpg://...

# API（只接受/查询，不运行门禁）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 独立 Gate Worker（可多个进程/实例；同一 metrics 端口只用于本机观测）
python -m app.worker --workers 8 \
  --database-url postgresql+asyncpg://... \
  --metrics-port 8030 \
  --work-lease-seconds 300 \
  --sandbox local
```

生产 Docker sandbox：

```bash
VN_GATE_SANDBOX=docker VN_GATE_SANDBOX_IMAGE=veynod-gate:latest \
  python -m app.worker --workers 8 --database-url ...
```

Worker 指标：`GET http://127.0.0.1:8030/metrics`；重置 `POST /metrics/reset`。API 的 `/v1/admin/metrics` 只反映 API 进程的 claim/SSE/API pool。

---

## 7. 测试

```bash
TEST_DATABASE_URL=postgresql+asyncpg://.../<fresh-test-db> \
  pytest -q tests/test_integration.py tests/test_merge_coordinator.py            tests/test_submission_restart_e2e.py tests/test_work_leases.py
```

覆盖：

- 恶意越界：Runner 谎报 `touched_symbols`，实际修改未租约 symbol → `AST_SCOPE_DIFF` 拒绝，HEAD 不变；
- 模块级修改（新增 import/函数）仅有 symbol 锁 → 拒绝；
- 门禁超时（`VN_GATE_TIMEOUT_SECONDS=1` + sleep 5）→ `UNIT_TEST_SUITE` exit 124；
- Worker 崩溃/租约过期 → 第二个 worker 重领，attempts+1；
- 重复领取 → 两个 worker 并发 claim，只有一个成功；
- 旧 worker 用旧 lease 写结果 → 0 行（fenced）；
- 完整门禁结果持久化 → 5 个阶段写入 `gate_results`；
- 真实 uvicorn 进程重启 + 客户端断线 → 独立 worker 继续并 COMMITTED。

本轮实际：`tests/test_integration.py` 17 passed；`tests/test_merge_coordinator.py` 2 passed；`tests/test_submission_restart_e2e.py` 1 passed；`tests/test_work_leases.py` 2 passed；`tests/test_submission_protocol_unit.py` 4 passed；非 integration 共 77 passed。

---

## 8. 实测

### 100 task / 100 Runner / 0 SSE

结果：`.run/bench/v3-100/*.json`

- **100/100 COMMITTED**，exit 0；
- setup 7.6s / load 62.2s / drain 1.2s；
- accept p50 1.45s / p95 8.61s；
- 终态 p50 33.0s / p95 57.1s；
- worker gate pool wait p50 265.5ms / p95 407.7ms；
- API pool wait p99 468ms；worker pool wait p99 0.025ms；pool timeout 0；
- 5 个门禁阶段各 100 次，`AST_SCOPE_DIFF` 100% 通过。

### 1000 Runner / 1000 task / 1000 SSE

结果文件：`.run/bench/v3-1000/20261031T193335Z-cross-file.json`；**exit 1**（容量未达标）。

| 指标 | 实测 |
| :--- | ---: |
| setup / load / drain / acceptance | 57.9s / 320.5s / 62.5s / 383.0s |
| 202 接受 / 最终 COMMITTED | **1000 / 394** |
| 任务最终 | COMMITTED 394 / LEASED 598 / VERIFYING 8 |
| 非终态任务 | **606 / 1000** |
| 失败的 submission | **0** |
| accept p50 / p95 / p99 | 33.1s / 61.9s / 79.1s |
| 客户端终态 p50 / p95 | 187.3s / 284.0s（仅 307 个客户端看到终态） |
| worker gate pool wait p50 / p95 | **593.5ms / 858ms** |
| merge lock_wait p50 / p95 | **0ms / 495ms** |
| merge total p50 / p95 | **359ms / 609ms** |
| API pool wait p50 / p99 / max；timeout | 0.012ms / 3.63s / 15.2s；0 |
| worker pool wait p50 / p99 / max；timeout | 0.009ms / 0.029ms / 68.7ms；0 |
| SSE opens / frames / bytes / 错误 | 1000 / 1,771,767 / 539MB / **0** |
| 门禁阶段 | 5 个阶段各 394 次，全部 passed |

**结论**：独立 Worker 正确持久化并处理了 **1000/1000** 个提交，失败 submission 为 0；但 300s 窗口内只完成 394 个，剩余 606 个仍是明确的 QUEUED/LEASED/VERIFYING。门禁吞吐（8 个 worker）和 1000 并发 accept 仍是容量瓶颈，而不是 Gemini/客户端等待。

---

## 9. 尚未解决的风险

1. **1000 并发 accept 仍慢**：API accept p50 在 1000+SSE 下达到数十秒；这是 DB/CPU/SSE 竞争，不是门禁等待。
2. **门禁吞吐仍受 CPU/worktree 槽位限制**：1000 Runner 实测 300s 仅 394 COMMITTED；需要常驻测试 worker/批量测试/更多 CPU。
3. **local sandbox 不是安全边界**：只能保证凭证不泄漏、时间/资源限制和隔离 HOME/TMP；生产必须 Docker。
4. **VERIFYING 中断不自动重放**：旧 worker 在任务进入 VERIFYING 后失去租约，submission 明确 FAILED，任务等待 reaper；需运维重置。
5. **极窄 fencing 竞态**：merge 前检查与 Git CAS 之间仍有毫秒级窗口；当前用 2s 安全余量 + 心跳，若要求零窗口需要把 Git 操作放进可撤销事务或外部队列租约。
6. **Worker metrics 端口**：每个 Worker 进程一个本机 HTTP metrics 端口；多实例部署需要端口分配/集中采集。
7. **API Rust core 同步**：API 通过 kernel_events 同步外部 worker 的 committed/ready 状态（0.1s 轮询）并在终态查询时立即同步；高吞吐下应改为专用事件总线。
