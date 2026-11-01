# 容量验收工具：`scripts/bench_swarm.py`（v2）

> 日期：2026-10-31  
> 目标：让"1000 并发 Runner"的结论由**数据库最终状态**决定，而不是由压测客户端是否停止决定。  
> 结论先行：旧脚本在 1000/1000/1000 的实测中**退出码为 0**，但数据库最终只有 **316 COMMITTED / 654 VERIFYING / 30 LEASED**；新工具在同等压力下会以非零退出码报告失败，并给出每一项未完成原因。  
> **注意：** 本文 §5 的历史 1000 Runner 数据采集于同步 `/v1/syscalls/submit_patch` 时代。补丁提交协议现已升级为 v2（202 + 持久化 worker + 状态查询）；`scripts/bench_swarm.py` 也已切到 v2，并会自动启动独立 `python -m app.worker` Gate Worker；见 [docs/ASYNC_SUBMISSIONS.md](ASYNC_SUBMISSIONS.md) 与 [docs/GATE_WORKER.md](GATE_WORKER.md)。

---

## 1. 旧脚本为什么会"退出码 0 但任务未完成"

旧 `bench_swarm.py` 的退出条件只有"worker 连续 4 次拉不到任务"。在 1000 Runner / 1000 task / 1000 SSE 下：

1. 每个 worker 的 `submit_patch` 客户端超时是 180s；服务端门禁仍在继续执行。
2. 客户端超时后 worker 继续 claim 其他任务；当所有 worker 都连续 4 次空拉时，脚本直接查询一次 `/v1/admin/tasks` 并打印 JSON。
3. 此时大量任务仍在 `VERIFYING`，服务端继续合并；脚本没有等待、没有对终态做断言，`main()` 也没有返回值，所以进程退出码恒为 0。

### 1.1 实测复现（隔离数据库，未触碰 `veynod_test`）

```bash
# 新建一次性数据库 veynod_legacy_test_20261031_114546
# 旧脚本内部 DROP SCHEMA 只作用于这个新库
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_legacy_test_20261031_114546 \
python scripts/bench_swarm.py \
  --label legacy-1000 --files 1000 --functions 1 \
  --runners 1000 --sse-streams 1000 --port 8020
```

结果（旧脚本 stdout，已整理）：

| 指标 | 实测 |
| :--- | ---: |
| 进程退出码 | **0** |
| duration_s | 380.12 |
| setup_s | 136.01 |
| 客户端 `statuses`（HTTP 200 响应） | COMMITTED **171** |
| 数据库最终状态（脚本结束后立即查询） | COMMITTED **316** / VERIFYING **654** / LEASED **30** |
| 非终态任务 | **684 / 1000** |
| `http_errors`（单一计数桶） | **1360** |
| server log `QueuePool limit` | **10** |
| server log `TimeoutError` | **20** |
| config.lock 错误 | 0 |

结论：旧脚本不但退出码为 0，而且客户端只看到 171 次 COMMITTED，数据库实际已有 316 次；剩余 684 个任务没有明确成功/失败。**旧数据不能作为生产容量证据。**

### 1.2 用新工具回放这份旧输出

```bash
python scripts/bench_swarm.py --evaluate-json .run/legacy-repro.out --profile dev
# EXIT=1；acceptance.reasons:
#   all_tasks_terminal  (non_terminal=684, statuses=316/654/30)
#   committed_tasks     (committed=316, expected=1000)
```

这说明：**旧输出本身已经包含足够的信息**判定失败；新工具只是不再忽略它。

---

## 2. 新工具的输出与退出契约

### 2.1 退出码

| 退出码 | 含义 |
| ---: | :--- |
| 0 | 选定 profile 下验收通过：任务数/终态/COMMITTED/错误与重试预算/SSE 预算/连接池超时全部满足 |
| 1 | 验收失败：任务数不等于预期、存在非终态任务、COMMITTED 不足、客户端与数据库不一致、错误/重试/SSE/连接池超预算、生产 profile 未满足 |
| 2 | 初始化或工具自身错误（数据库、服务器、种子、参数） |
| 130 | Ctrl+C 中断 |

**退出码不是唯一信息**：即使退出码为 1，也会输出 JSON 的 `acceptance.checks`、`acceptance.reasons`、`database.final_statuses`、`client` 与 `client_vs_database`。

### 2.2 客户端与数据库分离

- `client.submit.*`：v2 的 HTTP 202 接受、accept 4xx/5xx、超时、传输错误、重试；`client.submission_wait` 单独统计终态轮询错误/超时；`client.client_submit_statuses` 是客户端看到的最终 `task_status` 计数。
- `database.final_statuses`：直接使用 asyncpg 查询 `dag_tasks` 的最终状态，是唯一判据。
- `client_vs_database`：
  - `client_committed_responses`：客户端从 `GET /v2/submissions/{id}` 看到 `COMMITTED` 的次数；
  - `db_committed_tasks`：数据库最终 COMMITTED 的任务数；
  - `db_committed_without_client_success`：服务端在客户端超时/断开后仍完成的任务（late commit）；
  - `client_committed_without_db`：客户端说 COMMITTED 但数据库不是 COMMITTED（必须为 0）；
  - `accepted_non_terminal`：submit 返回 202 接受、但最终任务仍未进入终态的数量（必须为 0）。

### 2.3 JSON 指标

结果文件（默认 `.run/bench/<timestamp>-<label>.json`）和 stdout 摘要都包含：

- `timing`：setup / load（到 cutoff）/ drain / acceptance / cutoff 原因 / total。
- `throughput`：`committed_per_s`、`terminal_per_s`、`submit_200_per_s`、`claim_leased_per_s`、`peak_committed_per_s`。
- `client.latency_ms`：claim / submit / heartbeat / release / SSE TTFB 的 p50/p90/p95/p99/max。
- `queue`：初始/最终/峰值队列深度、每个状态的峰值、1s 采样序列、`peak_checkedout`。
- `server.db_pool`：SQLAlchemy 连接池 `size / checkedout / overflow / capacity`、checkout 等待时间分位数、`timeouts`。
- `server.histograms_ms`：
  - `merge.gate_pool_wait_ms`（门禁 worktree 池等待）；
  - `merge.lock_wait_ms`、`merge.total_ms`、`merge.git_tree_commit_ms` 等；
  - `submit.gate_pool_wait_ms`；
  - `gate.<stage>.duration_ms`。
- `server.counters`：服务端 `submit.started/finished/status.*`、`submit.merge_mode.*`、`claim.*`、`sse.*`。
- `client.sse`：独立统计 `attempts / opens / streams_opened_once / frames / bytes / timeouts / transport_errors / reconnects`。
- `server.log_analysis` 与 `server.log_tail`：QueuePool/timeout/config.lock 计数，作为交叉校验。

> 服务端指标来自新增的 `GET /v1/admin/metrics`（`X-Admin-Key` 保护），可通过 `POST /v1/admin/metrics/reset` 在 load 前清零。该端点是**纯新增**，不改任何既有 syscall/响应 schema；无需数据库迁移。

---

## 3. 隔离数据与安全边界

- 默认使用 `BENCH_ADMIN_DATABASE_URL`（默认 `postgresql://postgres:postgres@127.0.0.1:55432/postgres`）创建一个唯一命名的数据库：`veynod_bench_<scenario>_<label>_<utc>_<rand>`。
- **不执行 `DROP SCHEMA`，不触碰 `veynod_test` 或任何已有库。**
- 每个 Runner 通过 `POST /v1/admin/tokens` 获得独立 token / `worker_hub_id`；压测 worker 在 claim 时发送自己的身份，SSE 也使用独立 token。
- `--heartbeat-interval-s` 控制租约心跳；`pytest tests/test_bench_swarm_e2e.py` 用 1s 思考时间 + 0.5s 心跳证明 `heartbeat.success > 0`。心跳只覆盖 `LEASED` 思考阶段；进入 `VERIFYING` 后服务端会返回 409，工具单独计入 `not_leased`，不算错误。
- `--reuse-database` 仅在显式指定时复用；默认拒绝同名已存在库。
- 清理策略 `--db-policy`：
  - `keep-on-failure`（默认）：失败保留数据库，便于复盘；通过则删除本次创建的库。
  - `always-keep` / `always-drop`。
- 只删除**本次创建**的唯一库；结果文件记录 `database.cleanup_sql`，可人工清理。
- 临时 Git 仓库和 work_dir 在 `--keep-work-dir` 时保留，否则删除；server log 的摘要与尾部写入结果文件。

---

## 4. 运行命令

### 4.1 1000 Runner / 1000 task，0 SSE 对照

```bash
python scripts/bench_swarm.py \
  --scenario cross-file --files 1000 --functions 1 \
  --runners 1000 --sse-streams 0 \
  --profile dev \
  --max-duration-s 300 --drain-timeout-s 30 --submit-timeout-s 180 \
  --db-policy keep-on-failure
```

### 4.2 1000 Runner / 1000 task，1000 SSE

```bash
python scripts/bench_swarm.py \
  --scenario cross-file --files 1000 --functions 1 \
  --runners 1000 --sse-streams 1000 \
  --profile dev \
  --max-duration-s 300 --drain-timeout-s 30 --submit-timeout-s 180 \
  --db-policy keep-on-failure
```

### 4.3 同文件热点场景（hot-file）

```bash
# 默认 preset：10 个文件 × 100 个函数 = 1000 task
python scripts/bench_swarm.py \
  --scenario hot-file --files 10 --functions 100 \
  --runners 1000 --sse-streams 1000 \
  --profile dev --max-duration-s 300 --drain-timeout-s 30 --submit-timeout-s 180
```

### 4.4 生产 profile（typecheck + lint 必须开启）

```bash
python scripts/bench_swarm.py \
  --scenario cross-file --files 100 --functions 1 \
  --runners 100 --sse-streams 20 \
  --profile production --max-duration-s 600 --drain-timeout-s 120
```

`--profile production` 会检查 `pyright`/`ruff` 是否存在；若被 `--skip-*` 跳过或服务端指标不可用，验收失败并标记 `production_grade=false`。

### 4.5 旧 JSON 回放（迁移路径）

```bash
# 直接对旧脚本的 stdout JSON 重新执行验收；720 未完成会以退出码 1 失败
python scripts/bench_swarm.py --evaluate-json old-legacy-result.json --profile dev
```

旧 JSON 的 `final_statuses`、`tasks`、`http_errors` 会被映射到新判据；`detail_complete=false` 会在 `acceptance.warnings` 中明确说明缺失的细粒度计数。

### 4.6 协议与迁移

- **无数据库迁移**：`dag_tasks` / `code_artifacts` 表结构不变；新指标是进程内内存计数器。
- **新增管理端点**（纯增量，旧客户端无感知）：
  - `GET /v1/admin/metrics`：返回 pool / claim / submit / merge / gate / SSE 指标；
  - `POST /v1/admin/metrics/reset`：在 load 前清零。
- 6 个 syscall 的请求/响应 schema、Rust 调度核心、Git `update-ref` 条件更新语义均未改变。
- 旧结果 JSON 可回放；新结果文件带 `schema_version: "bench-swarm/2"`。

---

## 5. 实测结果（本轮）

> 本节由实际运行结果填充；所有数字来自隔离数据库，未重置 `veynod_test`。  
> 环境：Windows 10 / Python 3.10.6 / PostgreSQL 16.15 (Docker, 55432) / 12 逻辑 CPU / Rust `veynod_core` 0.1.0。

### 5.1 旧脚本复现

见 §1.1。

### 5.2 新脚本：1000 Runner / 1000 task / 0 SSE

**命令**：`--scenario cross-file --files 1000 --functions 1 --runners 1000 --sse-streams 0 --profile dev --max-duration-s 300 --drain-timeout-s 30 --submit-timeout-s 180`

**退出码：1（失败）。** 结果文件：`.run/bench/20261031T164043Z-cross-file.json`（隔离库保留，`database.dropped=false`）。

| 指标 | 实测 |
| :--- | ---: |
| setup / load / drain / acceptance | 48.2s / 301.2s / 140.7s / 441.9s |
| 数据库最终状态 | **COMMITTED 644 / VERIFYING 322 / LEASED 34** |
| 非终态任务 | **356 / 1000** |
| 客户端收到 COMMITTED | **280** |
| 数据库 COMMITTED | **644** |
| 服务端晚于客户端超时完成（late commit） | **364** |
| 客户端 submit 超时 / 传输错误 | **686 / 909** |
| submit 尝试 / 重试 | 1875 / 769（含超时后的安全连接重试与重复尝试） |
| submit HTTP 4xx / 5xx | **0 / 0**（失败几乎全部是客户端读超时/连接错误） |
| claim 尝试 / 租到 / 空 | 4840 / 1106 / 3343 |
| claim 超时 / 传输错误 / 重试 | 17 / 374 / 391 |
| release 尝试 / 成功 / 传输错误 | 140 / 106 / 34 |
| heartbeat 尝试 | 0（思考 0.05–0.20s < 30s 心跳间隔；心跳由 e2e 单独证明） |
| 队列峰值 READY / LEASED / VERIFYING | 1000 / 964 / 812 |
| 吞吐（最终 COMMITTED / 验收窗口） | **1.46 /s**；停止 claim 时 0.97 /s；峰值 5.68 /s |
| 连接池 checkout 数 / 超时 | 7126 / **0** |
| 连接池等待 p50 / p95 / p99 / max | **0.012ms / 0.018ms / 66.6ms / 13.8s** |
| 连接池峰值占用 | **60 / 60**（size 20 + overflow 40） |
| 门禁 worktree 池等待 p50 / p95 / p99 / max | **191.5s / 356.1s / 369.6s / 373.2s** |
| merge lock_wait p50 / p95 | **2.45s / 13.4s** |
| merge total p50 / p95 | **358ms / 1.20s** |
| Stage 4 pytest p50 / p95 | **3.36s / 4.43s** |
| server `submit.started` / `finished` / `submit_incomplete` | **966 / 644 / 322** |
| server log（load / shutdown） | 0 QueuePool / 0 TimeoutError / 0 traceback（两阶段都干净） |

失败原因：`all_tasks_terminal`、`committed_tasks`、`claim_transport_error_budget`、`claim_retry_budget`、`release_transport_error_budget`、`submit_transport_error_budget`、`submit_retry_budget`、`submit_error_budget`、`server_submit_incomplete`。  
`client_database_commit_agreement=true`：客户端声称 COMMITTED 的 280 个任务全部在数据库 COMMITTED；另有 364 个任务在客户端超时/释放后才被服务端提交。

> 同配置在本机早前一次运行得到 804 COMMITTED / 196 非终态（`.run/bench/20261031T160848Z-cross-file.json`）。两次都失败，但完成量差异约 25%，说明门禁 worktree 排队与 Git/pytest 子进程对机器时序很敏感；**不能把任何单次运行当作 SLA**，应看多次运行的分布。

**结论**：即使 0 条 SSE，1000 Runner 在 300s claim 窗口 + 30s drain 内也无法让全部任务进入明确终态；门禁 worktree 池等待 p50 ~191s 是主瓶颈。

### 5.3 新脚本：1000 Runner / 1000 task / 1000 SSE

**命令**：`--scenario cross-file --files 1000 --functions 1 --runners 1000 --sse-streams 1000 --profile dev --max-duration-s 300 --drain-timeout-s 30 --submit-timeout-s 180 --sse-warmup-s 30`

**退出码：1（失败）。** 结果文件：`.run/bench/20261031T163037Z-cross-file.json`（隔离库保留）。

| 指标 | 实测 |
| :--- | ---: |
| setup / load / drain / acceptance | 56.1s / 316.4s / 49.0s / 365.3s |
| 数据库最终状态 | **COMMITTED 368 / VERIFYING 632** |
| 非终态任务 | **632 / 1000** |
| 客户端收到 COMMITTED | **220** |
| 数据库 COMMITTED | **368** |
| 服务端 late commit（客户端超时后才完成） | **148** |
| submit 尝试 / HTTP 200 / 读超时 / 传输错误 / 重试 | 1092 / 220 / **842** / 30 / 91 |
| claim 尝试 / 租到 / 空 / 超时 / 传输错误 / 重试 | 5251 / 1001 / 2883 / 132 / **1235** / 1367 |
| heartbeat 尝试 | 0（思考时间 0.05–0.20s < 30s 心跳间隔；心跳本身由 e2e 测试单独证明） |
| SSE 开启 / 帧 / 字节 | **1000 / 1,358,425 / 378.7MB** |
| SSE 错误 / 超时 / 重连 | **0 / 0 / 0** |
| SSE TTFB p50 / p95 / p99 | **6.26s / 7.09s / 7.34s** |
| 队列峰值 READY / LEASED / VERIFYING | 1000 / 785 / 986 |
| 吞吐（最终 COMMITTED / 验收窗口） | **1.007 /s**；峰值 7.48 /s |
| 连接池 checkout / 超时 | 3735 / **0** |
| 连接池等待 p50 / p95 / p99 / max | **0.012ms / 0.019ms / 129.0ms / 8.99s** |
| 连接池峰值占用 | **60 / 60** |
| 门禁 worktree 池等待 p50 / p95 / p99 / max | **114.2s / 222.8s / 233.6s / 236.4s** |
| merge lock_wait p50 / p95 | **10.7s / 48.6s** |
| merge total p50 / p95 | **500ms / 2.72s** |
| Stage 4 pytest p50 / p95 | **3.38s / 3.93s** |
| server `submit.started` / `finished` / `submit_incomplete` | **1000 / 368 / 632**（该轮运行代码尚未把 incomplete 作为独立 reason 输出；最终代码会输出 `server_submit_incomplete`） |
| server SSE total_opened / total_closed | **1000 / 1000** |
| 服务端日志 | load 阶段 0 QueuePool / 0 TimeoutError；最终日志尾部有 3 个 GitError `checkout/reset`，发生在停止 server 时、中断了在途 gate；当前版本已把 load 与 shutdown 日志分开记录 |

失败原因：`all_tasks_terminal`、`committed_tasks`、`claim_transport_error_budget`、`claim_retry_budget`、`submit_transport_error_budget`、`submit_retry_budget`、`submit_error_budget`。

**与 0 SSE 对照**：同一预算下，1000 条 SSE 让 COMMITTED 从 **644 降到 368（-43%）**，submit 读超时从 686 升到 **842**，merge `lock_wait` p50 从 **2.45s 升到 10.7s**。SSE 本身没有连接错误；额外的 1000 个订阅者、1.36M 帧扇出和 379MB 写出放大了 merge/CPU 竞争。

### 5.4 同文件热点场景

**命令**：`--scenario hot-file --files 10 --functions 10 --runners 100 --sse-streams 100 --profile dev --max-duration-s 300 --drain-timeout-s 60 --submit-timeout-s 300`

**退出码：0（通过）。** 结果文件：`.run/bench/20261031T164308Z-hot-file.json`；隔离库按 `always-drop` 删除。

| 指标 | 实测 |
| :--- | ---: |
| setup / load / drain / total | 7.0s / 61.3s / 1.0s / 69.4s |
| 任务 / Runner / SSE | 100 / 100 / 100 |
| 数据库最终状态 | **COMMITTED 100 / 100** |
| 客户端 vs 数据库 | 100 vs 100；late commit 0；server submit incomplete 0 |
| 错误 / 超时 / 重试 | **0 / 0 / 0** |
| submit p50 / p95 / p99 | 31.7s / 54.3s / 54.8s（100/100 HTTP 200） |
| claim p50 / p95 / p99 | 265ms / 3.47s / 4.35s |
| 吞吐 | **1.60 COMMITTED/s**；峰值 7.21/s |
| tree_merge / gate_rerun | 99 / 1 |
| 门禁 worktree 池等待 p50 / p95 | **20.36s / 40.56s** |
| merge lock_wait p50 / p95 | 5.61s / 11.66s |
| merge total p50 / p95 | 282ms / 1.90s |
| Stage 4 pytest p50 / p95 | 2.63s / 3.22s |
| 连接池等待 p50 / p99 / max | 0.013ms / 22.0ms / 1.35s |
| 连接池超时 / QueuePool 日志 | 0 / 0 |
| SSE opens / frames / bytes | **100 开启 / 20,000 帧 / 5.77MB**；0 错误、0 重连 |

**结论**：同文件热点写集（10 文件 × 10 函数）在 100 Runner / 100 SSE 下可以全部进入 COMMITTED；但门禁池等待 p50 仍为 20.4s，说明 gate 执行模型是规模化的主要限制。

### 5.5 生产 profile 冒烟（真实 pyright + ruff）

**命令**：`--scenario cross-file --files 2 --functions 1 --runners 2 --sse-streams 0 --profile production`

**退出码：0（通过）。** 结果文件：`.run/bench/production-smoke/20261031T164612Z-cross-file.json`；隔离库已删除。

| 指标 | 实测 |
| :--- | ---: |
| 任务 / Runner / SSE | 2 / 2 / 0 |
| 数据库最终状态 | **COMMITTED 2 / 2** |
| `production_grade` | **true**（`skip_typecheck=false`, `skip_lint=false`） |
| setup / load / drain / total | 0.48s / 6.09s / 0.22s / 6.80s |
| `gate.STATIC_TYPECHECK` p50（2 个样本） | **2.98s**（真实 pyright） |
| `gate.UNIT_TEST_SUITE` p50 | 1.49s（真实 pytest） |
| 错误 / 超时 / 重试 | 0 / 0 / 0 |

这条只证明生产 profile 的接线和真实 pyright/ruff 门禁可运行，不代表 1000 Runner 的生产容量。

---

## 6. 测试

```bash
# 纯函数/退出码测试（无需数据库）
pytest -q tests/test_bench_swarm_acceptance.py

# 真实 Postgres + Git + pytest + uvicorn 的端到端测试：
# 1) 2 task/2 Runner/2 SSE 通过并删除隔离库
# 2) 同样的拓扑 --expected-tasks 3，必须退出 1（task_count_matches）
pytest -q tests/test_bench_swarm_e2e.py

# 全量非 integration 测试（当前 78 passed）
pytest -q -m "not integration"
```

本轮实际执行：`tests/test_bench_swarm_acceptance.py` 18 passed；`tests/test_bench_swarm_e2e.py` 1 passed（内部两次真实运行：通过 + `--expected-tasks 3` 失败）；`pytest -q -m "not integration"` 78 passed。

关键测试覆盖：

- 1000 个任务中 280 COMMITTED / 719 VERIFYING / 1 LEASED → 退出 1；
- 任务数不等于预期、存在额外任务、错误率/重试率/连接池超时超预算 → 退出 1；
- 客户端 COMMITTED 与数据库 COMMITTED 不一致 → 退出 1；
- SSE 0 条为合法对照；1000 条时 open ratio/帧/错误率有独立断言；
- 旧 JSON 回放：720 未完成 → 退出 1；全部 COMMITTED → 退出 0；
- 真实端到端通过/失败两条路径。

---

## 7. 尚未解决的风险

1. **1000 并发下的门禁模型仍是主瓶颈**：交付代码的 0 SSE 实测 `gate_pool_wait_ms` p50 **191.5s**（1000 SSE 为 114.2s，随运行波动），远超 300s claim 窗口内的可用吞吐。新工具只是让失败可见，并没有改变门禁吞吐。要稳定服务 1000 Runner，需要常驻 pytest worker / 按 workspace 批测 / 更多 CPU，而不是压测器。
2. **客户端超时后的服务端工作**：新工具会对超时 submit 做数据库轮询与必要的 release，但服务端若在客户端断开后仍执行长事务，其资源占用仍计入容量；`server.submit.started - finished` 可暴露这个差值。
3. **连接池等待只覆盖 SQLAlchemy checkout 等待**：`pg_stat_activity` 中的锁等待、磁盘 I/O 等待不在 `db_pool.wait_ms` 内；后者需另行采样。
4. **SSE 1000 连接受客户端/OS 限制**：Windows 临时端口、httpx 连接池、服务端 backlog 都可能成为边界；工具会记录 open ratio 与错误，但不会自动调高 OS 限制。
5. **生产 profile 的 pyright/ruff 成本**：`--profile production` 会真实执行类型检查与 lint，1000 task 的耗时显著高于 dev；本轮 dev 结果不能当作生产容量。
6. **旧脚本复现的 316/654/30 与用户报告的 280/719/1 不是同一组数字**：同一失败模式，具体分布随机器/时序变化；关键是旧脚本退出码为 0，而新工具会失败。
7. **单次运行不足以定义 SLA**：同一 0 SSE 配置两次运行分别为 804 和 644 COMMITTED；差异来自 gate/Git/pytest 子进程与机器负载时序。验收应使用多次运行分布（建议至少 3 次），本轮只做单次对照。
8. **cutoff 释放本身会产生 transport 错误**：0 SSE 的 140 次 release 中有 34 次传输错误，最终留下 34 个 LEASED；工具单独统计并判失败，但这也说明"停止 claim 后立即 drain"的窗口对 1000 个在途 lease 偏短。
9. **Windows 进程树清理**：工具现在用 `taskkill /T /F` 终止 server 及其 pytest/git 子进程；若 server 已提前崩溃，残留子进程可能不会被该调用清理，需要外部看门狗。
10. **本轮全部是 dev profile**：typecheck/lint 被显式跳过，`production_grade=false`；这些数字不能当作生产容量，只能作为工具验收和瓶颈定位。
