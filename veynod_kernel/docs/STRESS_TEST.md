# Veynod Kernel v4.0 — 100 Runner Swarm Stress Test

> **日期：** 2026-10-31  
> **代码：** 第二轮优化基线（Rust 权威调度 + 单 Kernel claim 快路径 + 不可变 AST 快照 + 异步 symbol_edges + opt-in tree merge + worktree pool + EventHub）  
> **脚本：** [`scripts/bench_swarm.py`](../scripts/bench_swarm.py)  
> **环境：** Windows 10 + Python 3.10 + PostgreSQL 16 (Docker) + Rust 1.98 + 12 逻辑 CPU  
> **重要：** 这不是 1,000 Runner soak；这是 100 Runner / 100 task 的单机确定性压测基线。数字只用于量级判断，不是跨机器承诺。

---

## 1. 方法

压测不消耗 Claude/Codex Token：

- 100 个并发 httpx worker，走**真实 HTTP** `/v1/syscalls/pull_task_bundle` 和 `submit_patch`。
- 每个 Agent 的“思考”替换为 `asyncio.sleep(0.05–0.20s)`。
- 每个任务预生成一个只修改目标函数体的 unified diff；门禁真实执行 `pytest`。
- 真实 PostgreSQL 16、真实 bare Git 仓库、真实 Gate worktree pool。
- 每个 worker 另外保持一个 SSE 连接（共 20 个）以覆盖 EventHub 路径。
- Server 配置：
  - `VN_FAST_FORWARD_MERGE=true`
  - `VN_GATE_WORKTREE_POOL_SIZE=8`
  - `VN_SINGLE_KERNEL_CLAIM=true`（默认）
  - `VN_ASYNC_EDGE_REBUILD=true`（默认）
  - `VN_SKIP_TYPECHECK=true`、`VN_SKIP_LINT=true`（隔离 merge/DB/Git；Stage 4 pytest 仍执行）
  - `VN_TEST_CMD="pytest -q --tb=short"`
  - `VN_REAPER_INTERVAL_SECONDS=3600`
- 任务构造：
  - **Scenario A（cross-file）**：100 个源文件 × 1 函数 = 100 个 IMPLEMENT_BODY。
  - **Scenario B（hot-file）**：10 个源文件 × 10 函数 = 100 个 IMPLEMENT_BODY。
  - 每个任务写集是符号级（`file.py::fN`），测试是独立的 `tests/test_*` 文件。
- 第二轮新增响应内计时：
  - `merge_timing`：`gate_pool_wait_ms`、`lock_wait_ms`、`dirty_check_ms`、`git_tree_commit_ms`、`artifact_db_ms`、`edge_enqueue_ms`、`kahn_db_ms`、`core_sync_ms`、`total_ms`。
  - `claim_timing`：`claim_decision_ms`、`bundle_ms`、`total_ms`。
  - `gate_results[].duration_ms`：每个 Stage 的真实耗时。

---

## 2. 第二轮最终结果（当前代码基线）

| 指标 | Scenario A: cross-file | Scenario B: hot-file |
| :--- | ---: | ---: |
| 任务 / Runner / SSE | 100 / 100 / 20 | 100 / 100 / 20 |
| **总耗时** | **54.20 s** | **57.99 s** |
| **COMMITTED** | **100 / 100** | **100 / 100** |
| HTTP errors | 0 | 2（transport 错误，重试后全部完成） |
| **tree_merge 命中** | **99 / 100（99%）** | **99 / 100（99%）** |
| gate_rerun | 1 | 1 |
| claim p50 / p95 / p99 | 29.78 ms / 4.84 s / **5.00 s** | 41.52 ms / 4.50 s / **4.69 s** |
| submit p50 / p95 / p99 | **26.19 s** / 45.36 s / 47.01 s | **29.19 s** / 48.48 s / 49.58 s |
| **gate_pool_wait p50 / p95** | **20.21 s / 37.42 s** | **20.30 s / 38.32 s** |
| Stage 4 pytest p50 / p95 | 2.42 s / 3.60 s | 2.70 s / 3.50 s |
| **merge total p50 / p95** | **218 ms / 376 ms** | **281 ms / 661 ms** |
| merge lock_wait p50 / p95 | 422 ms / 983 ms | 1.34 s / 3.25 s |
| merge git_tree_commit p50 / p95 | 125 ms / 171 ms | 139 ms / 236 ms |
| merge dirty_check p50 / p95 | 0 ms / 0 ms | 31 ms / 47 ms |
| merge artifact_db p50 / p95 | 14 ms / 47 ms | 16 ms / 61 ms |
| merge kahn_db p50 / p95 | 16 ms / 31 ms | 15 ms / 63 ms |
| merge core_sync p50 / p95 | 16 ms / 48 ms | 16 ms / 47 ms |
| merge edge_enqueue p50 | 0 ms（后台队列） | 0 ms（后台队列） |
| config.lock 错误 | **0** | **0** |
| QueuePool 错误 / Timeout | **0 / 0** | **0 / 0** |

### 2.1 第一轮历史（优化前，保留对比）

| 运行 | 总耗时 | submit p50 | 结果 |
| :--- | ---: | ---: | :--- |
| cross-file（文件级 merge 脏检查 + 无 pool/edge 优化） | 191.80 s | 89.19 s | 93 COMMITTED / 7 VERIFYING / 9 HTTP |
| cross-file（symbol 级脏检查 + claim/merge 优化，未修 symbol-edge 碰撞） | 193.14 s | 76.82 s | 99 COMMITTED / 1 VERIFYING / 2 HTTP |
| cross-file（第一轮最终：修 symbol-edge 同名碰撞） | 62.44 s | 40.46 s | 100 COMMITTED / 0 HTTP |
| hot-file（第一轮最终） | 55.26 s | 27.29 s | 100 COMMITTED / 0 HTTP |

第二轮相对第一轮：
- cross-file 总耗时 **62.44 s → 54.20 s（-13%）**，submit p50 **40.46 s → 26.19 s（-35%）**。
- hot-file 总耗时 **55.26 s → 57.99 s**（系统负载波动；merge 本身大幅下降，gate pool 仍是主瓶颈）。
- merge 总耗时 **0.55–0.62 s → 0.218–0.281 s**（约 2.2–2.8×）。
- claim p99 **9.25 s → 5.00 s**（cross-file）、**6.94 s → 4.69 s**（hot-file）。

### 2.2 Claim A/B：单 Kernel 快路径 vs per-workspace advisory lock

同一 100 Runner / cross-file / 20 SSE：

| 指标 | `VN_SINGLE_KERNEL_CLAIM=false`（advisory lock） | `true`（默认，无 claim advisory lock） |
| :--- | ---: | ---: |
| 总耗时 | 63.67 s | **54.20 s** |
| claim p50 | 47.19 ms | **29.78 ms** |
| claim p95 / p99 | 6.24 s / **6.91 s** | 4.84 s / **5.00 s** |
| claim_decision p50 | 16 ms | 15 ms |
| claim_decision p95 | **3.17 s** | **358 ms** |
| claim_decision p99 | **3.26 s** | **437 ms** |
| bundle p95 / p99 | 172 ms / 250 ms | 986 ms / 1.16 s |
| submit p50 | 26.63 s | **26.19 s** |
| tree_merge | 99 / 100 | 99 / 100 |
| config.lock / QueuePool / Timeout | 0 / 0 / 0 | 0 / 0 / 0 |

结论：去掉 claim advisory lock 后，**claim 决策尾部从 3.17 s 降到 358 ms（约 8.9×）**；但 HTTP claim p99 仍有 5 s，因为 `bundle_ms`（读 Git 文件 + AST 切片）和连接排队成为新的大头。这也证明 Claim 3→1 单 SQL 即便正确实现，单独也不足以把 P99 打到 150 ms。

---

## 3. 结论

### 3.1 本轮已验证有效的优化

1. **不可变 Git/ AST 快照缓存 + 锁外预计算**  
   - Rust `file_snapshot(source, file_path)` 单次解析返回：整文件骨架哈希、每个符号的源码哈希、签名哈希、骨架、docstring、引用名。
   - `(commit_sha, file_path)` 内容与 `FileASTSnapshot` 进程内 LRU；Git blob 不可变，缓存永久有效（上限 2,000–4,000 条）。
   - 门禁在 Stage 4 之后、进入 `_merge_lock` 之前预计算新提交的所有变更文件快照；锁内 `dirty_check` 变成内存比较。
   - 结果：cross-file `dirty_check p50 = 0 ms`，hot-file `31 ms`；锁内不再执行 `git diff` / `git show` / tree-sitter 重解析。

2. **`symbol_edges` 后置到后台队列**  
   - merge 事务只保留 artifact 同步、`mark_done`/Kahn 解锁、HEAD 更新。
   - `symbol_edges` 由单后台协程按提交顺序批量重建；`edge_enqueue_ms ≈ 0`。
   - `symbol_edges` 只被 `escalate_contract_defect` 和 `/edges/rebuild` 读取，正常 6 syscall 热路径不读；最终一致可接受。

3. **单 Kernel claim 快路径**  
   - `VN_SINGLE_KERNEL_CLAIM=true` 时，Rust `SchedulerCore` 的单 Mutex 决策已经保证 100 个候选任务写集两两正交；DB 只做各自独立主键的条件 UPDATE + `NOT EXISTS` 围栏。
   - 保留 `lease_expires_at > NOW()`；不再让 100 个互不冲突的 claim 排同一条 `pg_advisory_xact_lock`。
   - 多进程部署可设 `false` 退回强串行。

4. **符号级 tree-merge 脏检查**（第一轮已证明）  
   - 文件骨架哈希 + read/test/target 每个符号源码哈希；同文件不同符号不再误杀。
   - **99% 命中，1% 重跑**；Batch Merge Train 对命中率仍未必要。

5. **worktree 池 / 常驻 Runner 沙箱**  
   - 100 Runner 下 `config.lock` 错误 **0**；QueuePool 错误 / Timeout **0**。

### 3.2 目标水位对照

| 指标 | 目标 | 实测 | 结论 |
| :--- | :--- | :--- | :--- |
| tree_merge 命中率 | > 80–90% | **99%** | 达标 |
| 门禁重跑率 | < 10–15% | **1%** | 达标 |
| Claim P99 | < 50 ms | **4.7–5.0 s** | **未达标**；advisory lock 已不是主因；bundle + 连接排队占大头 |
| Merge 临界区 | 50–80 ms | **218–281 ms** | **未完全达标**，但已从 550–620 ms 降 2.2–2.8×；Git 子进程占 125–139 ms |
| Submit P50 | 个位数秒 | **26–29 s** | **未达标**；**Gate worktree 池等待 p50 = 20.2 s**，不是 merge |

### 3.3 为什么 Submit P50 还是 26–29 s：排队论修正

你的模型是“100 个协程在 0.55 s 合并锁前排成 50 人队 → 50 × 0.55 ≈ 27.5 s”。第二轮数据推翻了这个归因：

- **merge `lock_wait` p50 只有 422 ms（cross-file）/ 1.34 s（hot-file）**，不是 27 s。
- **merge `total` p50 只有 218–281 ms**，不是 550–620 ms。
- **`gate_pool_wait p50 = 20.2 s`**，与 submit p50 26–29 s 基本吻合。
- Stage 4 pytest p50 2.42–2.70 s；Gate worktree 池只有 **8** 个槽位；100 tasks / 8 slots ≈ 13 波 × 约 2.4 s ≈ **31 s**。
- 本机 **12 逻辑 CPU**；即使把池开到 12–16，100 个真实 `pytest` 进程的下限也约 **100 / 12 × 2.4 s ≈ 20 s**。要把 submit P50 打到 5 s，必须改门禁执行模型（批量/常驻测试 worker、共享 pytest 会话、按 workspace 合并测试运行），而不是继续压缩 merge。

### 3.4 为什么 Claim P99 还是 4.7–5.0 s

- `claim_decision` p95 已从 3.17 s 降到 **358–422 ms**；advisory lock 确实去掉了。
- `bundle_ms` p95 约 **717–986 ms**、p99 **922 ms–1.16 s**；bundle 仍要读 target/test 文件并做 AST 切片。
- cross-file 的 100 个 target + 100 个 test 文件互不相同，`(commit,file)` 缓存只能命中重复读取，无法让 200 个不同 blob 的 `git show` 消失；hot-file 的 10 个源文件能命中，但 100 个 test 文件仍不同。
- HTTP claim p99 4.7–5.0 s 还包含 100 并发请求的连接/排队尾部；服务端 `claim_timing.total_ms` p99 约 2.2–2.7 s。
- 要打到 150 ms，需要**批量 Git 读取**（`git cat-file --batch` 常驻进程）或直接读 bare repo 对象，并扩大 DB 连接池/减少每请求 workspace 查询；单靠 claim 锁不够。

### 3.5 下一阶段优先级（基于本轮数据）

1. **Gate 执行模型（最高收益）**：submit P50 的主瓶颈是 8 个 worktree 槽位 + 每任务独立 pytest。优先做：
   - 常驻 pytest worker / 按 workspace 批量测试；
   - 或提高 `VN_GATE_WORKTREE_POOL_SIZE` 并配 CPU 配额；
   - 在 1,000 Runner 前先定义“每 CPU 的门禁吞吐”容量模型。
2. **Bundle/Git 批量读取**：`git cat-file --batch` 常驻进程或内存对象缓存，目标把 `bundle_ms` p95 从 ~1 s 压到 100 ms 内；这是 Claim P99 的主要剩余项。
3. **Merge 继续压**：已 218–281 ms；Git `merge-tree` + `commit-tree` + `update-ref` 三个 Windows 进程就要 125–139 ms。要进 50–80 ms，需要 libgit2/`git2` 进程内实现或持久 Git 服务；不建议再靠减少 SQL。
4. **Batch Merge Train**：命中率 99% 已达标，暂不引入；只有当 Gate/merge 都优化后、1,000 Runner 吞吐仍不足时再评估。
5. **Claim 3→1 SQL**：不要在未补 `lease_expires_at > NOW()`、重试/退避和单 Kernel 语义前上线；本轮 A/B 已证明锁不是唯一瓶颈。

### 3.6 压测器限制

- 100 Runner，不是 1,000；100 tasks，不是 500/1,000。脚本支持调大 `--runners/--files/--functions`。
- 12 逻辑 CPU；8 个 Gate worktree 槽位；真实 Claude/Codex 的 2–5 s 思考会改变 claim/merge 交错形态。
- `VN_SKIP_TYPECHECK/LINT=true`；Stage 3 的真实 pyright/ruff 成本未计入。
- hot-file 本轮有 2 个客户端 transport 错误，benchmark 自动 release/重试后 100/100 COMMITTED；server 端 `config.lock` / QueuePool / Timeout 均为 0。
- `pool_wait_ms` 仍没有独立 SQLAlchemy pool 指标；已新增 `gate_pool_wait_ms`、`claim_timing`、`merge_timing` 作为替代。
- Scenario A 的 100 个文件都含一个函数 `f0`，用于触发同名 symbol-edge 碰撞；真实仓库命名分布不同。

---

## 4. 复现

```bash
# 测试 Postgres（README 的 55432）
docker run -d --rm --name veynod-test-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=veynod_test \
  -p 55432:5432 postgres:16-alpine

# Rust 扩展
python scripts/build_rust.py

# 两组 100 Runner 基线
python scripts/bench_swarm.py --label cross-file --files 100 --functions 1 --runners 100 --sse-streams 20
python scripts/bench_swarm.py --label hot-file  --files 10  --functions 10 --runners 100 --sse-streams 20

# Claim A/B：退回 per-workspace advisory lock
VN_SINGLE_KERNEL_CLAIM=false python scripts/bench_swarm.py \
  --label cross-file-lock --files 100 --functions 1 --runners 100 --sse-streams 20
```

脚本会重置 `veynod_test` schema、启动 uvicorn、跑真实门禁，最后输出 JSON 指标并扫描 server log 中的 `config.lock` / `QueuePool` / `TimeoutError`。

---

## 5. 原始关键 JSON（第二轮）

```json
{
  "cross-file": {
    "duration_s": 54.2,
    "claim_p50_ms": 29.78,
    "claim_p99_ms": 4995.74,
    "submit_p50_ms": 26188.99,
    "merge_timing_p50_ms": {
      "gate_pool_wait_ms": 20211.0,
      "lock_wait_ms": 421.5,
      "dirty_check_ms": 0.0,
      "git_tree_commit_ms": 125.0,
      "artifact_db_ms": 14.0,
      "edge_enqueue_ms": 0.0,
      "kahn_db_ms": 16.0,
      "core_sync_ms": 16.0,
      "total_ms": 218.0
    },
    "gate_stage_p50_ms": {
      "CONTRACT_HASH_GUARD": 32.0,
      "UNIT_TEST_SUITE": 2421.5
    }
  },
  "hot-file": {
    "duration_s": 57.99,
    "claim_p50_ms": 41.52,
    "claim_p99_ms": 4689.68,
    "submit_p50_ms": 29193.16,
    "merge_timing_p50_ms": {
      "gate_pool_wait_ms": 20304.5,
      "lock_wait_ms": 1335.5,
      "dirty_check_ms": 31.0,
      "git_tree_commit_ms": 139.0,
      "artifact_db_ms": 16.0,
      "edge_enqueue_ms": 0.0,
      "kahn_db_ms": 15.0,
      "core_sync_ms": 16.0,
      "total_ms": 281.0
    },
    "gate_stage_p50_ms": {
      "CONTRACT_HASH_GUARD": 46.0,
      "UNIT_TEST_SUITE": 2702.5
    }
  }
}
```
