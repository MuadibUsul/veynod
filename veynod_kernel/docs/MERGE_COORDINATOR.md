# 每仓库受围栏保护的合并协调器

> 日期：2026-10-31  
> 迁移：[`sql/v4_004_merge_coordinator.sql`](../sql/v4_004_merge_coordinator.sql)  
> 目标：门禁可并行；同一仓库的主干推进由唯一 fenced coordinator 有序执行；合并意图与结果持久化；重启自动对账。  
> 保留：Git `update-ref <new> <old>` CAS；Rust `SchedulerCore`；原有 AST 门禁与写集锁。

---

## 1. 结构

```text
Gate Worker A ─┐
Gate Worker B ─┼─> gate stages in parallel (isolated worktrees)
Gate Worker N ─┘
                    │ verified task commit + gate_results
                    ▼
              merge_intents (durable, one per submission)
                    │
                    ▼
       MergeCoordinator (one elected leader per workspace)
                    │
                    ├─ renew workspace fencing lease
                    ├─ check HEAD / dependency revalidation
                    ├─ gate.merge_verified -> Git update-ref CAS
                    ├─ DB finalize (artifacts/Kahn/core/events)
                    └─ write MERGED / REJECTED / CONFLICT / REVALIDATE
```

- Gate worker 不再直接调用 `_merge`；它运行 `verify_patch`，产生 task commit，然后持久化 merge intent 并等待结果。
- Coordinator elect 使用 `merge_coordinators` 表：`workspace_id` 主键、`coordinator_id`、`fencing_token`、`lease_expires_at`。
- 任意数量 worker 进程都可运行 coordinator 任务；同一 workspace 只有一个有效 lease。

---

## 2. merge intent

`merge_intents` 关键字段：

| 字段 | 含义 |
| :--- | :--- |
| `submission_id UNIQUE` | 一个提交只有一个可见合并意图 |
| `base_sha` / `task_commit_sha` | 已验证的基线与任务提交 |
| `changed_files`, `gate_results`, `pool_wait_ms` | 门禁结果快照 |
| `lease_token_hash` | 依赖变化时把任务放回 LEASED 用 |
| `status` | `PENDING/RUNNING/MERGED/REJECTED/CONFLICT/REVALIDATE` |
| `coordinator_token` | 当前 coordinator fencing token |
| `merged_commit_sha`, `merge_mode`, `merge_timing` | 持久化结果 |
| `unlocked_downstream_task_ids` | 下游解锁结果 |
| `error_code/detail`, `revalidations` | 失败/重验证记录 |

Gate worker 的 upsert 以 `submission_id` 幂等；重复消息不会产生第二行或第二次合并。

---

## 3. Fencing 与串行化

- Coordinator 只有拿到 workspace lease 才能 claim intent。
- Claim：

```sql
UPDATE merge_intents
SET status='RUNNING', coordinator_id=..., coordinator_token=...
WHERE merge_id = (
  SELECT merge_id FROM merge_intents
  WHERE workspace_id=:ws AND status='PENDING'
  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
)
RETURNING *;
```

- 结果写入：

```sql
UPDATE merge_intents SET status=... WHERE merge_id=:id
  AND status='RUNNING' AND coordinator_token=:token;
```

- merge 前回调再次续租；失去 lease 抛 `MergeFenceLost`，不执行 Git CAS。
- 旧 coordinator 的迟到结果写入 0 行，不能覆盖新 coordinator 的结果。

---

## 4. 崩溃窗口与启动对账

### 4.1 Git 已更新、DB 未提交

- Coordinator 先 `update-ref`，再写 DB；中途崩溃会留下 RUNNING intent + Git HEAD 已包含 `task_commit_sha`。
- 启动时 `reconcile_workspace`：
  - 读取 Git HEAD；
  - 找到 `task_commit_sha` 已成为 HEAD 祖先的 intent；
  - 调用 `_finalize_merge` 补齐 task COMMITTED、workspace head、artifact sync、Kahn、core/event；
  - intent 置 `MERGED`。

### 4.2 DB 已提交、Git 未更新（相反顺序）

- 启动对账发现 workspace DB head / task result commit 不在 Git main：
  - workspace head 回退为 Git HEAD；
  - task 从 COMMITTED 回退为 VERIFYING，清空 `result_commit_sha`；
  - 对应 intent 置 `REVALIDATE`；
  - 不允许保留“DB 成功但 Git 没合并”的假成功。

### 4.3 重复消息

- `merge_intents.submission_id UNIQUE`；gate worker upsert；coordinator 结果 UPDATE 带状态/fencing。
- 测试断言：重复 202 后 `patch_submissions` 和 `merge_intents` 都只有 1 行。

---

## 5. 主干变化与依赖重验证

Coordinator 在合并前比较 `base_sha` 与当前 HEAD：

- HEAD == base：直接合并。
- HEAD 已包含 task commit：视为 Git-first 崩溃，补齐 DB。
- HEAD 变化：
  - 取 `changed_files_between(base, HEAD)`；
  - 读取 task 的 read/target/test artifact 文件路径；
  - 若任一变化文件命中这些依赖文件，或命中 `conftest.py`/`pytest.ini`/`pyproject.toml`/`setup.cfg`/`tox.ini`，或位于 `tests/`：
    - **不合并**；任务放回 LEASED，intent 置 `REVALIDATE`，gate worker 在**新 HEAD** 上重跑全部门禁；
    - 最多 3 次。
  - 若依赖未受影响且启用 fast-forward/tree merge，才允许树合并；否则同样重验证。
- 因此“补丁能干净合并”不等于测试仍有效；依赖变化会在组合后的新基线上重新执行 AST scope、合同、类型/lint、测试。

---

## 6. 单补丁合并吞吐与批量决策

100 task / 100 Runner / 0 SSE、8 个 gate worker、独立 coordinator：

- 结果：`.run/bench/v4-100/20261031T195418Z-cross-file.json`
- **100/100 COMMITTED**，exit 0；
- 总耗时 81.6s（setup 7.5 / load 72.6 / drain 1.5）；
- `merge.total_ms` p50 235ms / p95 297ms / sum 24.4s；
- 单补丁合并吞吐 ≈ **4.1 merges/s**（100 / 24.4s）；
- 同期端到端 gate 吞吐 ≈ **1.35 tasks/s**；merge 不是瓶颈。

决策：**单补丁合并吞吐高于当前门禁吞吐，未达到需要批量的条件；本阶段不做小批量合并。**  
如果未来 gate 优化到 >4 merges/s，再引入小批量合并并在组合树上运行必要测试。

> 说明：目标定义为“合并协调器不得成为门禁吞吐的瓶颈”，而不是单独追求 merge 数字；当前 1000 Runner 的瓶颈是 gate/pytest 与 accept，而不是 merge。

### 6.1 1000 Runner coordinator 实测

结果：`.run/bench/v4-1000/20261031T200213Z-cross-file.json`；**exit 1**（容量未达标）。

| 指标 | 实测 |
| :--- | ---: |
| 202 接受 / 最终 COMMITTED | **1000 / 415** |
| 任务最终 | COMMITTED 415 / LEASED 577 / VERIFYING 8 |
| 提交最终 | COMMITTED 415 / QUEUED 577 / RUNNING 8 |
| 失败的 submission / merge intent | **0 / 0** |
| merge.total p50 / p95 / sum | **328ms / 467ms / 140.6s** |
| 单补丁 merge 吞吐（415 / 140.6s） | **≈2.95 merges/s** |
| 同期 gate 吞吐 | **≈1.35 tasks/s** |
| gate pool wait p50 / p95 | 562ms / 718ms |
| API/worker pool timeout | 0 / 0 |
| SSE opens / frames / bytes / 错误 | 1000 / 1,812,801 / 553MB / 0 |
| accept p50 / p95 / p99 | 29.2s / 54.0s / 88.7s |

结论：coordinator 串行合并仍未成为瓶颈（2.95 > 1.35 merges/s），故不做小批量合并；1000 容量失败仍归因于 gate/CPU 与 accept。

---

## 7. 测试

```bash
TEST_DATABASE_URL=postgresql+asyncpg://.../<fresh-test-db> \
  pytest -q tests/test_integration.py tests/test_merge_coordinator.py \
           tests/test_submission_restart_e2e.py tests/test_work_leases.py
```

本轮结果：`tests/test_integration.py` 17 passed；`tests/test_merge_coordinator.py` 2 passed；`tests/test_submission_restart_e2e.py` 1 passed；`tests/test_work_leases.py` 2 passed；`tests/test_submission_protocol_unit.py` 4 passed；非 integration 77 passed。

覆盖：

- 外部提交命中 read dependency → gate 在新 HEAD 重跑并拒绝/失败，不会把旧测试结果当成有效；
- 外部提交造成补丁冲突 → 永不提交，main 保留外部提交；
- Git-first 崩溃 → 重启 reconcile 将 intent 置 MERGED、task COMMITTED；
- DB-first 崩溃 → 重启撤销假 COMMITTED、intent REVALIDATE；
- 重复 202 → 一个 submission / 一个 merge intent；
- 旧 coordinator/worker fencing；
- 真实 uvicorn 进程重启 + 客户端断线。

---

## 8. 尚未解决的风险

1. **DB-first 回滚不恢复已同步的 artifact 状态**：task 不会保持假 COMMITTED，但 target/test artifact 可能已标记 COMMITTED；需要后续 artifact 对账/重建。
2. **依赖判定是保守文件级**：read/target/test artifact 文件与测试基础设施变化会触发全量重验证；间接 import 但未登记为 read 的模块变化可能漏判，建议扩大 read_set/依赖图。
3. **Coordinator lease 与 Git CAS 之间仍有毫秒级窗口**：结果以分 fencing 与 Git CAS 保证，极端窗口下仍需人工检查 `merge_intents` 状态。
4. **单 coordinator 吞吐未参与 1000 目标**：4.1 merges/s 足够当前 gate；如果 gate 大幅提速，需要批量合并。
5. **1000 Runner 的容量仍未达标**：瓶颈在 gate/pytest 与 1000 并发 accept，不是 merge coordinator。
6. **local sandbox 不是安全边界**；生产必须 Docker 镜像。
