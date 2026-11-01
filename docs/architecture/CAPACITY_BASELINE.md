# CAPACITY_BASELINE — 容量基线（Phase 0 复测）

> 重要：本文中的历史数字来自 `.run` 原始 JSON/日志；本轮复测来自 `veynod_kernel/scripts/bench_swarm.py` 的隔离实例。
> **不得把历史结果直接当当前 SLA。** 所有数字都必须在当前源码、迁移、部署和相同负载模型下重新验收。
> 日期：2026-10-31（本地 UTC-4）。

---

## 1. 测试环境

| 项 | 值 |
|---|---|
| OS | Windows 10 x64（10.0.22000） |
| CPU | 12 logical CPUs |
| RAM | 约 31 GB |
| Python | 3.10.6（Kernel / bench）；AgentGraph venv 3.12 依赖齐全 |
| PostgreSQL | 16.15，`127.0.0.1:55432`（Docker `veynod-test-pg`） |
| Rust | 1.98.1；`veynod_core` 0.1.0；release DLL 已构建 |
| Gate | `local` 沙箱；pyright 1.1.414；ruff 0.15.20/0.17.1；Git 2.54；Node 24.15 |
| API/Worker | bench 为每轮创建独立数据库、独立 uvicorn API 实例、独立 `app.worker` |
| 线上实例 | AgentGraph :8000（3.24.0，Docker）；Kernel :8001（4.0.0，最新副本）；worker :8030（8 workers） |

**未使用 Docker gate sandbox。** 所有容量数字反映 `local` 执行；Docker/K8s 隔离会改变 CPU/I/O/启动时间，不能直接外推。

---

## 2. 指标定义（必须区分）

| 指标 | 含义 | 不是 |
|---|---|---|
| Connected Agents | SSE/HTTP 连接数 | 不等于活跃 Agent |
| Active Agent Runs | 真正在执行 Agent Loop 的 Run | 当前平台没有该指标 |
| Concurrent Executions | 正在执行工具/代码的任务 | 不等于连接数 |
| Concurrent Repository Mutations | 正在修改同一 Repository 的任务 | 不等于任务创建数 |
| Concurrent Gate Jobs | 正在验证的 submission | 不等于提交受理数 |
| Concurrent Merges | 正在执行 Git CAS 的 intent | 不等于 committed 数 |
| `runners`（bench） | 并发客户端 worker 数 | 不等于 Agent 数 |
| `sse-streams`（bench） | 建立的 SSE 连接数 | 不等于活跃 Agent |

**不得把 202 Accepted、SSE Connected、任务创建数包装成完整交付能力。**

---

## 3. 验收门槛（当前 harness）

`veynod_kernel/scripts/bench_swarm.py` 默认/生产 profile：

| 检查 | 门槛 |
|---|---:|
| 任务数匹配 | exact |
| 所有任务 terminal | 0 non-terminal |
| 所有任务 COMMITTED | `max_failed=0` |
| 已受理 submission terminal | 0 non-terminal |
| client COMMITTED 但 DB 未 COMMITTED | 0 |
| HTTP error rate | < 1% |
| transport/timeout rate | < 0.5% |
| submit failure rate | < 0.2% |
| retry rate | < 5% |
| **submit accept P99** | **≤ 1,000 ms** |
| DB pool timeouts | 0 |
| SSE open ratio | ≥ 99% |
| SSE error rate | < 1% |
| production profile | typecheck + lint enabled，server metrics 可用 |

> 注意：P99 门槛是当前 harness 的硬门槛。历史上一些“通过”的 100-runner 结果早于该检查，因此不能算当前通过。

---

## 4. 本轮复测

### 4.1 2-task production smoke

命令：

```bash
cd E:/Codes/veynod/veynod_kernel
python scripts/bench_swarm.py --label phase0-smoke --profile production \
  --files 2 --functions 1 --runners 2 --sse-streams 0 \
  --gate-workers 2 --gate-pool-size 2 --max-duration-s 120 --max-accept-p99-ms 1000
```

结果（`.run/bench/20261101T021948Z-phase0-smoke.json`；运行结束时也曾写入 `latest.json`）：

- 2/2 COMMITTED；
- acceptance passed = true，exit 0；
- total 8.219s，load 7.344s，drain 0.297s；
- 所有检查通过，包括 submit accept P99。

**结论：** 最新源码的单机最小链路可用（创建、claim、submit、gate、merge、commit）。这是“能跑通”，不是容量结论。

### 4.2 100-runner production

命令：

```bash
python scripts/bench_swarm.py --label phase0-100 --profile production \
  --files 100 --functions 1 --runners 100 --sse-streams 100 \
  --gate-workers 8 --gate-pool-size 8 --gate-worker-port 8180 \
  --max-duration-s 300 --max-accept-p99-ms 1000
```

结果（`.run/bench/20261101T022813Z-phase0-100.json`；`latest.json` 随后被 500 运行覆盖）：

| 指标 | 值 |
|---|---:|
| 最终 | 100/100 COMMITTED |
| 总耗时 | 126.53s（load 116.44s + drain 1.28s + setup 8.81s） |
| 吞吐 | 0.8495 commit/s；峰值 2.7804 commit/s |
| submit accept P99（client） | **8,015.51 ms** |
| submit accept P99（server） | 931.79 ms |
| gate pool wait P50/P99 | 280.5 / 594.3 ms |
| claim P50/P99 | 250 / 376.4 ms（client latency） |
| DB pool timeouts | 0 |
| HTTP/transport/submit errors | 均在预算内 |
| acceptance | **false，exit 1** |

唯一失败检查：`submit_accept_p99_budget`，观察值 8,015.51ms，门槛 1,000ms。

**解释：**

- 100 个任务全部完成，但受理 P99 仍超过 1s；说明“完成”不等于“满足当前验收”。
- server-side accept P99 931ms 已在门槛边缘；client-side P99 8.0s 说明排队/连接/客户端并发或事件循环存在额外延迟。
- 与历史 `full-100-final` 的 6.84s 失败同量级；历史 `full-100` 的 7.057s “通过”是因为旧产物没有 P99 检查。
- 在 100 并发、8 gate workers、12 CPU 的环境下，当前不能宣称“100 Runner 通过当前验收”。

### 4.3 500-runner production

命令：

```bash
python scripts/bench_swarm.py --label phase0-500 --profile production \
  --files 500 --functions 1 --runners 500 --sse-streams 500 \
  --gate-workers 8 --gate-pool-size 8 --gate-worker-port 8180 \
  --max-duration-s 900 --max-accept-p99-ms 1000 --db-policy always-drop
```

结果（`.run/bench/20261101T023951Z-phase0-500.json`；当前 `latest.json` 指向此运行）：

| 指标 | 值 |
|---|---:|
| 最终 | **500/500 COMMITTED**，0 non-terminal |
| 总耗时 | 657.672s（setup 36.219s + load 619.969s + drain 1.484s） |
| 吞吐 | 0.8046 commit/s；峰值 11.883 commit/s |
| submit accept P99（client） | **60,156.82 ms** |
| submit accept P99（server） | 14,092.93 ms |
| submit accept P50（client/server） | 16,917.65 / 5,322.73 ms |
| claim P50/P99（client） | 16,623.56 / 42,481.73 ms |
| claim P50/P99（server） | 4,487.50 / 11,974.66 ms |
| claim transport error | **143/4,315 = 3.314%**（门槛 0.5%） |
| submit transport error | **83/583 = 14.2367%**（门槛 0.5%） |
| submit retry rate | **14.2367%**（门槛 5%） |
| submit failure rate | **14.2367%**（门槛 0.2%） |
| SSE | 500/500 opened once，748,500 frames，0 errors，242.5 MB |
| DB pool timeouts | 0 |
| acceptance | **false，exit 1** |

失败检查：`claim_transport_error_budget`、`submit_transport_error_budget`、`submit_retry_budget`、`submit_error_budget`、`submit_accept_p99_budget`。

**解释：**

- 500/500 全部完成，但完成过程伴随严重传输错误/重试和 60s 级受理延迟；这不是可接受的生产质量。
- 与历史 `full-500b`（478/500，P99 64.5s，1200s 窗口）相比，本轮完成率更高、总耗时更短，但负载配置/窗口/实现不同，不能直接宣称“提升”；两者都没有通过当前验收。
- server-side accept P99 14.1s 仍远超 1s，说明问题不只是客户端网络；API/事件循环/DB/准入路径都需要剖析。
- 500 并发已使 claim P99 42.5s、submit accept P99 60.2s；1,000 级不能靠单实例硬撑。

### 4.4 1,000-runner

本轮已复测。命令：

```bash
cd E:/Codes/veynod/veynod_kernel
python scripts/bench_swarm.py --label phase2-final-1000 --profile production \
  --files 1000 --functions 1 --runners 1000 --sse-streams 1000 \
  --gate-workers 8 --gate-pool-size 8 --gate-worker-port 8180 \
  --max-duration-s 1800 --max-accept-p99-ms 1000 --db-policy always-drop
```

结果（`.run/bench/20261101T043908Z-phase2-final-1000.json`，`latest.json` 指向此运行）：

| 指标 | 值 |
|---|---:|
| 最终 | **1000/1000 COMMITTED**，0 non-terminal，0 failed |
| 总耗时 | 1,503.797s（setup 85.406s + load 1,415.312s + drain 3.047s） |
| 吞吐 | 0.705 commit/s；峰值 5.818 commit/s |
| submit accept P99（client） | **171,121.37 ms** |
| claim transport error | **1,927/19,464 = 9.900%**（门槛 0.5%） |
| claim retry rate | **10.152%**（门槛 5%） |
| submit transport error | **345/1,348 = 25.594%**（门槛 0.5%） |
| submit error / retry | **25.594%**（门槛 0.2% / 5%） |
| SSE transport error | **18,565/19,444 = 95.479%**（门槛 1%） |
| SSE open ratio | 1000/1000 = 100%（每个 stream 至少打开一次；18,444 次重连） |
| DB pool timeouts | **1**（门槛 0） |
| server metrics | 可用；server log 有 98 次 queuepool 限额、196 次 timeout 记录 |
| acceptance | **false，exit 1** |

失败检查：`claim_transport_error_budget`、`claim_retry_budget`、`submit_transport_error_budget`、`submit_retry_budget`、`submit_error_budget`、`submit_accept_p99_budget`、`sse_error_budget`、`db_pool_timeout_budget`。

**解释：**

- 这是当前源码/迁移/部署下**首次 1,000 production 全量 COMMITTED**（1000/1000，0 non-terminal），比历史 `full-1000` 的 881/1000 更好；但吞吐只有 0.705 commit/s，总耗时 25 分钟。
- 受理 P99 171s、submit 传输错误 25.6%、SSE 95.5% 超时，说明 API 单实例在 1,000 并发下已经不可用；完成全部任务是因为客户端/服务端持续重试并拉长到 1,415s，不是低延迟处理。
- DB pool 仅 1 次 timeout，但 server 日志出现 queuepool 限额；瓶颈首先在 API 事件循环/连接处理与 SSE fan-out，而不是数据库连接完全耗尽。
- 该结果满足“任务最终完成”的持久性/正确性观察，但**不满足**当前 harness 的生产验收，不能宣称 1,000 Runner 生产可用。

### 4.5 Phase 8 优化后复测（2026-11-01）

`EventHub` 改为 per-subscriber queue、admission 快速拒绝、claim 冲突检查合并、8 API + `VN_MULTI_CORE_CLAIM=true` 后：

- 100 runners：100/100 COMMITTED，SSE 0 error，server accept P99 102ms，client accept P99 5,995ms；
- 1,000 runners（`phase8-1000-multicore`）：**975/1000 COMMITTED**，25 LEASED 到 30 分钟截止；**SSE 1,000/1,000 打开、0 重连、0 error**；DB pool 0 timeout；**server accept handler P99 886.8ms**；client accept P99 84.4s；claim P50/P99 250/4,281ms；
- 结论仍是 **acceptance false**：client P99/transport 超预算，且非终态 25。服务端 handler 已接近 1s，但客户端单进程 bench 的 pre-middleware 排队使端到端 P99 失真/超标。

---

## 5. 历史原始证据（`.run`）

以下为 2026-10-31 已有原始 JSON/日志，**不是本轮复测**。

| 运行 | 配置 | 最终 DB 状态 | 验收 | 关键数字 |
|---|---|---|---:|---|
| `full-1000/latest.json` | production，1000/1000/1000，8 gate workers，8 API，1800s | **881 COMMITTED / 104 LEASED / 12 VERIFYING / 3 READY** | false, exit 1 | 119 non-terminal；10 client-COMMITTED 但 DB 未 COMMITTED；93 late commits；accept P99 **69,510ms**；claim P99 **89,397ms**；5,270 claim retries；0 pool timeouts；总 2,071s |
| `full-500b/latest.json` | production，500/500/500，8/4 | **478 COMMITTED / 22 READY** | false, exit 1 | accept P99 **64,518ms**；claim P99 **61,230ms**；0 pool timeouts；总 1,417s |
| `full-500/latest.json` | production，500/500/500，8/2 | **408 COMMITTED / 92 LEASED** | false, exit 1 | accept P99 43,821ms；**17 DB pool timeouts**；总 1,436s |
| `full-100-final/latest.json` | production，100/100/100，8/2 | **100 COMMITTED** | false, exit 1 | 唯一失败：accept P99 **6,843.76ms**；总 119.6s |
| `full-100/latest.json` | production，100/100/100，8/2 | **100 COMMITTED** | true, exit 0 | accept P99 **7,057ms**，但产物**没有** P99 检查；总 131.6s |
| `v9-1000/latest.json` | **dev**，1000/1000/1000，8 workers，1200s | **1000 COMMITTED** | false, exit 1 | 0.9698 commit/s；22.7% claim transport errors（4,606/20,247）；14.4% submit transport（168/1,168）；12,772 admission denials；0 SSE errors；总 1,092.6s |
| `claim-1000/latest.json` | production，claim-only，1000/1000/1000，8/4，120s | **1000 READY** | false | 589 successful claims；4,229 admission denials；claim P99 **73,800ms**；1,176,250 SSE frames；0 pool timeouts |
| `v8-400/latest.json` | dev，400/400/400，8 | **400 COMMITTED** | false, exit 1 | 642 claim transport errors；53 submit transport；2,595 admission denials；0 HTTP 4xx/5xx |
| `...cross-file.json` | dev，1000/1000/0 SSE，300s | **644 COMMITTED / 322 VERIFYING / 34 LEASED** | false | gate pool wait P50 **191.5s**；merge lock wait P50 2.45s；364 late commits；总 490s |
| `...cross-file.json` | dev，1000/1000/1000 SSE，300s | **368 COMMITTED / 632 VERIFYING** | false | gate pool wait P50 **114.2s**；lock wait P50 10.7s；1.36M SSE frames；总 421s |
| `...hot-file.json` | dev，100/100/100，10 files × 10 funcs | **100 COMMITTED** | true, exit 0 | 总 69.4s；20,000 SSE frames；99 tree merges / 1 gate rerun |
| `production-smoke/latest.json` | production，2/2/0 | **2 COMMITTED** | true, exit 0 | 总 6.8s；真实 pyright + ruff + pytest |

### 5.1 关键事实

1. **当前 harness 的 P99 检查下，18 个含该检查的产物全部失败**；所有“通过”的产物要么早于该检查，要么是 dev/hot-file/smoke。
2. **没有当前生产 profile 的 100/500/1,000 通过记录。**
3. 1,000 production 的最好记录是 881/1,000，仍有 119 个非终态。
4. 1,000 dev 能 1000/1000 COMMITTED，但 22.7% claim 传输错误、14.4% submit 传输错误，不能算稳定。
5. Gate pool 是明确瓶颈：1,000 时 P50 等待 114–191s；8 workers 在 12 CPU 上是最优配置（文档称 16 更差，但该 A/B 没有原始产物）。
6. SSE 连接层有基础：1,000 SSE 建立、0 SSE 错误；但 1,000 SSE ≠ 1,000 活跃 Agent。

### 5.2 文档与产物差异

- `STRESS_TEST.md`/README 的 100-Runner 54.2s/58.0s、claim P99 5.0s/4.7s、submit P50 26.2s/29.2s：`.run` 中没有匹配原始产物（DOCUMENTED_ONLY）。
- `CAPACITY_1000.md` 的 8-vs-16 gate worker A/B：没有归档 JSON（DOCUMENTED_ONLY）。
- `HORIZONTAL_SCALE.md` 的 v5-1000 “2,595 HTTP-429” 与 `v5-1000` 产物（2,338 attempts / 1,257 4xx）不一致；2,595 出现在 `v8-400` 的 admission_denied（PARTIAL）。
- `BENCH_ACCEPTANCE.md` 的 78 non-integration 与当前 79 收集数有同日漂移。

---

## 6. 当前容量结论

### 6.1 可以说的

- 最新源码的最小链路（2 tasks）通过；
- 100 个任务在约 126s 内全部 COMMITTED；
- 500 个任务在约 658s 内全部 COMMITTED，但验收未通过（P99/传输错误）；
- **1,000 个任务在当前源码上全部 COMMITTED（1,503.8s），但验收未通过（P99 171s/传输错误）**；
- Gate/Merge 的持久化、lease、CAS、恢复路径有真实测试；
- 100 级路径有真实的代码执行、门禁、Git commit 证据；
- 1,000 SSE 连接层已实测：1,000/1,000 打开，优化后 0 重连、0 error。

### 6.2 不能说的

- 不能说“100 Runner 通过当前验收”（P99 8.0s > 1s）；
- 不能说“500/1,000 Runner 生产可用”；
- 不能说“1,000 活跃 Agent”；
- 不能把 202/Connected/任务创建数当交付能力；
- 不能把历史 `full-100` 的 exit 0 当当前通过；
- 不能把 dev 1,000/1,000 COMMITTED 当生产稳定；
- 不能把 1,000/1,000 COMMITTED（优化前）或 975/1,000 + server accept P99 0.89s（优化后）当生产通过；客户端 P99/传输错误仍超预算。

### 6.3 瓶颈判断

按现有证据，优先级：

1. **提交受理 P99 / 事件循环 / 连接处理**：100 并发已 8s，500 并发 60s，1,000 并发 171s；
2. **Gate pool 吞吐**：1,000 时等待 114–191s；
3. **Merge 串行化/锁等待**：同 repo 高冲突；
4. **DB pool/连接管理**：历史 500 曾 17 timeouts；本轮 500 为 0，但连接等待仍需观测；
5. **SSE fan-out 内存/带宽**：1.36M frames/421s；
6. **AgentGraph 控制面**：单 FastAPI 进程，未压测 1,000 活跃 Agent；
7. **多仓库/多 Cell**：未实现，无法水平扩展单实例。

### 6.4 建议的 Phase 1/6 容量工作

- 先修 P99：profile API 受理路径（事件循环、JSON、DB 写、admission、连接池），分离 client/server 指标；
- Gate Worker 独立扩容/分池；按 CPU/测试时间定容量，不按 Agent 数机械配置；
- 将 SQL/Rust claim、admission、submission 的 p99 分阶段埋点；
- 在 3 次重复运行下报告分布（当前文档建议至少 3 次）；
- 1,000/5,000 必须用 Execution Cell/Repository Sharding，不要单实例硬撑；
- 压测产物记录源码 commit、迁移版本、环境、Docker/local sandbox、CPU/内存。

---

## 7. 复现命令

```bash
cd E:/Codes/veynod/veynod_kernel

# 非集成
python -m pytest -q -m "not integration"

# 选定集成（会 DROP public schema，必须指向 disposable test DB）
TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test' \
python -m pytest -q tests/test_admission.py tests/test_work_leases.py \
  tests/test_merge_coordinator.py tests/test_edge_outbox.py

# Rust
cd rust/veynod_core && cargo test --release

# 容量 smoke
python scripts/bench_swarm.py --label smoke --profile production \
  --files 2 --functions 1 --runners 2 --sse-streams 0 \
  --gate-workers 2 --gate-pool-size 2 --max-duration-s 120

# 100
python scripts/bench_swarm.py --label 100 --profile production \
  --files 100 --functions 1 --runners 100 --sse-streams 100 \
  --gate-workers 8 --gate-pool-size 8 --gate-worker-port 8180 \
  --max-duration-s 300 --max-accept-p99-ms 1000
```

> 注意：bench 会创建/删除隔离数据库；不要指向生产库。`--db-policy always-drop` 可在失败时清理数据库，但保留 JSON。

---

## 8. 结论一句话

**当前是“100/500 能完成、1,000 在 30 分钟内只能完成 975”的工程原型；1,000 的最好记录是 1000/1000 COMMITTED 但 accept P99 171s（优化前），以及服务端 accept P99 0.89s/SSE 0 错误但客户端 P99 84s、975/1000（优化后）。容量结论是“未达标”，不是“已验证 1,000”。**
