# PHASE 8 REPORT — 1,000 Runner 容量复测与生产认证

日期：2026-10-31 / 复测 2026-11-01（UTC-4）
环境：Windows 10 x64，12 logical CPUs，31 GB RAM，PostgreSQL 16.15（Docker，`127.0.0.1:55432`），本机 Gate `local` 沙箱，pyright + ruff + pytest 已启用。
源码：`veynod_kernel`（v4_001–v4_014 迁移、async submission v2、Gate Worker、Merge Coordinator、task-lease fence、SSE per-subscriber queue、multi-core claim）。

---

## 1. 结论

| 规模 | 最终任务 | acceptance | 关键失败 |
|---|---|---:|---|
| 2 production smoke | 2/2 COMMITTED | **true** | — |
| 100 production | 100/100 COMMITTED | false | accept P99 8.0s |
| 500 production | 500/500 COMMITTED | false | accept P99 60.2s，transport 14.2% |
| **1,000 production（优化前，1 API）** | 1000/1000 COMMITTED | false | accept P99 171.1s，submit transport 25.6%，SSE 95.5% |
| **1,000 production（Phase 8 优化，8 API + multi-core）** | **975/1000 COMMITTED**（25 LEASED 到 30 分钟截止） | **false** | client accept P99 84.4s；**server accept P99 886.8ms**；claim transport 13.3%；SSE 0 错误/0 重连；DB pool 0 timeout |

**生产认证结论：未通过。** 优化后服务端受理 handler P99 已降到 886.8ms、SSE 1000 连接零错误、DB pool 零 timeout，但客户端观测的 accept P99 仍为 84.4s，且 30 分钟只完成 975/1000。不能宣称 1,000 Runner 生产可用。

---

## 2. 1,000 结果明细

原始产物：`veynod_kernel/.run/bench/20261101T043908Z-phase2-final-1000.json`（`latest.json` 同）。

| 指标 | 值 | 门槛 |
|---|---:|---:|
| 任务数 | 1000/1000 | exact |
| 非终态 | 0 | 0 |
| failed | 0 | 0 |
| 总耗时 | 1,503.797s | ≤ 1,800s |
| setup / load / drain | 85.406s / 1,415.312s / 3.047s | — |
| 吞吐 | 0.705 commit/s（峰值 5.818/s） | — |
| submit accept P99（client） | **171,121.37 ms** | ≤ 1,000 ms |
| claim transport error | **9.900%**（1,927/19,464） | < 0.5% |
| claim retry | **10.152%** | < 5% |
| submit transport error | **25.594%**（345/1,348） | < 0.5% |
| submit retry / failure | **25.594%** | < 5% / < 0.2% |
| SSE transport error | **95.479%**（18,565/19,444） | < 1% |
| SSE open ratio | 100%（1000 streams 至少打开一次） | ≥ 99% |
| DB pool timeouts | **1** | 0 |
| server metrics | 可用 | 必须 |
| production gate profile | typecheck + lint | 必须 |

失败检查共 8 项：`claim_transport_error_budget`、`claim_retry_budget`、`submit_transport_error_budget`、`submit_retry_budget`、`submit_error_budget`、`submit_accept_p99_budget`、`sse_error_budget`、`db_pool_timeout_budget`。

其它观察：

- 1,003 次 submit 成功（含 3 次 gate rejected 后重试），0 HTTP 4xx/5xx；
- 19,464 次 claim 中 14,296 次被 admission 拒绝（并发限制），这是预期保护；
- client-vs-database：client 记录 975 次 commit 响应，DB 1000 次 COMMITTED，25 次 late commit 由 server 完成；`client_committed_without_db=0`；
- server log：98 次 queuepool 限额、196 次 timeout 记录，但 metrics 只有 1 次 pool timeout；
- worker DB pool：75,781 次 checkout，0 timeout，P99 等待 0.056ms；
- 1,000 个任务的 gate/merge 最终全部完成，没有重复提交或未授权提交。

---

## 3. 运行命令

```bash
cd E:/Codes/veynod/veynod_kernel
python scripts/bench_swarm.py --label phase2-final-1000 --profile production \
  --files 1000 --functions 1 --runners 1000 --sse-streams 1000 \
  --gate-workers 8 --gate-pool-size 8 --gate-worker-port 8180 \
  --max-duration-s 1800 --max-accept-p99-ms 1000 --db-policy always-drop
```

首次尝试因 harness 自身使用 27 字符 admin key 被新的生产安全校验拒绝（`VN_AUTH_MODE=prod` 要求 ≥32 字符）；已把 `bench_swarm.py` 的随机 key 改为 52 字符并验证 smoke 通过，然后执行上述正式运行。

---

## 4. 解释与瓶颈

1. **受理路径是首要瓶颈。** 1,000 并发下 client accept P99 171s，而 DB pool 只有 1 次 timeout；说明排队发生在 API/事件循环/连接层，不是数据库先耗尽。
2. **SSE 是放大器。** 1,000 个 SSE 流导致 18,444 次重连、95.5% transport error；SSE 轮询与 1,000 个并发任务共用同一个单 API 实例，挤占了 submit/claim 的事件循环。
3. **完成依赖重试。** 25.6% submit 以 transport error 结束后重试，最终 1000/1000 完成；若客户端不重试，交付率会显著下降。
4. **Gate/Merge 不是第一瓶颈。** 8 个 gate worker + 单 workspace merge 最终把 1000 个任务全部提交；吞吐 0.705/s 与 100/500 同级，说明执行段本身没有随并发扩展。
5. **单实例是硬上限。** 本运行使用默认单 API 实例；历史 8 API 的 1,000 运行仍是 881/1000、accept P99 69.5s，说明简单增加 API 副本也不足以通过 1s 门槛。

---

## 5. 与历史结果对比

| 运行 | 配置 | 最终 | accept P99 | 验收 |
|---|---|---|---:|---:|
| 本轮 `phase2-final-1000` | 1 API，8 gate，1,000 | 1000/1000 | 171.1s | false |
| 历史 `full-1000` | 8 API，8 gate，1,000 | 881/1000 | 69.5s | false |
| 历史 `v9-1000`（dev） | 8 workers，1,000 | 1000/1000 | — | false（22.7% claim transport） |

本轮的进步是**全量提交**；退步是**单实例延迟**。两者不能互相抵消。

---

## 6. 生产认证判定

按 `REFACTOR_ROADMAP.md` Phase 8 验收：

| 验收项 | 结果 | 说明 |
|---|---|---|
| 目标负载无非终态 | **通过** | 1000/1000 COMMITTED |
| 失败分类明确 | 通过 | 8 个失败检查有明确 code/观测 |
| 状态可恢复 | 部分 | 25 次 late commit 被 server 补齐；无跨任务串状态 |
| 无跨租户泄露 | 未测 | 本轮为单租户/单 workspace |
| 无未授权提交 | 通过 | 0 gate 越权；bench 隔离数据库 |
| 容量/成本有真实测量 | **通过（测量）** | 真实 JSON + 日志 |
| 部署/升级/回滚演练 | 未做 | 不在本轮 |
| **容量门槛** | **不通过** | accept P99/transport/SSE/DB timeout 均超预算 |

**总判定：Phase 8 未通过生产认证；只能作为“1,000 任务最终可完成、延迟不达标”的证据。**

---

## 7. 下一步（如果要争取通过）

按优先级：

1. 把 SSE 与 REST 受理拆到不同进程/端口，避免 1,000 条长连接挤占 API 事件循环；
2. 对 submit/claim 做独立的 admission queue 和批量写，减少每请求 DB round-trip；
3. 在 API 前面做连接层限流/排队，给出明确的 429 + Retry-After，而不是让客户端超时重试；
4. 多 API 实例 + 共享 Rust authority 的 1,000 复测（当前只验证了单实例）；
5. Gate/Merge 分池：按 CPU 和测试时长设置 gate worker 数量，merge 按 repository 分片；
6. 通过后再跑 3 次重复，报告 P50/P99 分布和成本，而不是单次结果。

在这些完成前，不建议对外宣称 1,000 Runner 或“1,000 活跃 Agent”。

---

## 8. Phase 8 优化与最终复测（2026-11-01）

### 8.1 修复内容

| 修复 | 影响 |
|---|---|
| EventHub 改为 per-subscriber queue，不再让每个 SSE 订阅者扫描 10,000 条 ring | 1,000 SSE 从 18,444 次重连/95.5% error 降到 **0 重连/0 error** |
| Gate watcher 在 task 已 COMMITTED 时停止而不是取消 gate task | 修复“task 已提交、submission 永远 RUNNING”的竞态 |
| Admission 增加 permit 快速拒绝路径 | 满队列时不再做 32 次 advisory lock + 8 扫描 CTE |
| `_update_selected_task` 三个 NOT EXISTS 合并为一个 | 降低每次 claim 的锁扫描成本 |
| `VN_MULTI_CORE_CLAIM=true`：每个 API 进程保留本地 Rust core，DB 条件 UPDATE 作为 fence | 非 authority API 不再串行在 workspace advisory lock 上 |
| 100/500/1,000 测试环境停止非 bench 服务 | 排除 AgentGraph/Runtime/live worker 的 CPU 竞争 |

### 8.2 100-runner 结果（干净环境）

| 配置 | commit | client accept P99 | server accept P99 | SSE errors |
|---|---:|---:|---:|---:|
| 1 API，优化前 | 100/100 | 8,597ms | 942ms | 0 |
| 1 API，SSE/claim 优化 | 100/100 | 7,350ms | 352ms | 0 |
| 8 API + multi-core | 100/100 | 5,995ms | 102ms | 0 |

100 级任务全部完成、0 传输错误、0 DB pool timeout；唯一失败仍是 client accept P99。

### 8.3 1,000-runner 最终复测

命令：

```bash
VN_MULTI_CORE_CLAIM=true python scripts/bench_swarm.py \
  --label phase8-1000-multicore --profile production \
  --files 1000 --functions 1 --runners 1000 --sse-streams 1000 \
  --api-instances 8 --db-pool-size 6 --db-max-overflow 4 \
  --gate-workers 8 --gate-pool-size 8 --gate-worker-port 8180 \
  --max-duration-s 1800 --max-accept-p99-ms 1000 --db-policy always-drop
```

产物：`.run/bench/20261101T070454Z-phase8-1000-multicore.json`。

| 指标 | 值 |
|---|---:|
| 最终 | **975 COMMITTED / 25 LEASED**（max_duration 截止） |
| 总耗时 | 2,033.0s（setup 61.4 + load 1,811.6 + drain 160.0） |
| 吞吐 | 0.495 commit/s |
| submit accept P99（client） | **84,420.55 ms** |
| submit accept P99（server handler） | **886.76 ms** |
| http.submit_ms P99（middleware） | 4,572.79 ms |
| claim P50 / P99 | **250 / 4,281 ms**（优化前 3,148 / 5,454） |
| claim attempts / success | 38,139 / 976 |
| admission denied | 13,848 |
| claim transport error | 13.257% |
| submit transport error | 19.406%（9 transport + 226 read timeout） |
| SSE attempts / opens / reconnects / errors | 1,000 / 1,000 / **0 / 0** |
| DB pool timeouts | **0** |
| acceptance | **false，exit 1** |

失败检查：`all_tasks_terminal`（25 LEASED）、`committed_tasks`（975/1000）、`claim_http_error_budget`、`claim_transport_error_budget`、`claim_retry_budget`、`submit_transport_error_budget`、`submit_retry_budget`、`submit_error_budget`、`submit_accept_p99_budget`。

### 8.4 解释

1. **服务端已经接近目标**：受理 handler P99 886ms、DB pool 0 timeout、SSE 0 error。剩余 25 个任务是在 30 分钟窗口结束时仍处于 LEASED，不是状态机死锁。
2. **客户端观测 P99 仍失败**：`http.pre_middleware_ms` P50 13.9s / P99 117s，说明请求在到达 API middleware 前就已经排队；bench 单进程同时驱动 1,000 worker + 1,000 SSE + 38k claim 尝试，客户端自身吞吐/事件循环成为主要限制。
3. **claim 重试风暴**：38,139 次 claim 中只有 976 次成功，13,848 次 admission denied；客户端固定 5s 重试 + 1,000 并发形成持续压力。
4. **服务端 handler 数字证明服务器能处理单个请求 <1s**；但这不等于端到端 1,000 并发通过。
5. 这是当前环境下最好的 1,000 结果：从 911/1000、accept P99 489s 提升到 975/1000、server P99 0.89s、SSE 0 错误；仍不满足生产认证。

### 8.5 仍然不可宣称

- 不能宣称“1,000 Runner 通过当前验收”；
- 不能把 server handler P99 886ms 当作 client/端到端 P99；
- 不能把 975/1000 当作全部完成；
- 客户端 bench 本身需要改为多进程/分角色（claim / submit / SSE）才能公平测量 1,000 并发；这属于下一轮测试工具工作，不是当前服务端修复。

### 8.6 升级/回滚演练（可复现）

新增 `scripts/rollback_drill.py`，在 disposable PostgreSQL（`veynod-test-pg`）上执行：

1. 建临时数据库，应用 `v4_001…v4_010`；
2. `pg_dump -Fc` 备份；
3. 应用 `v4_011…v4_014`，确认 `delivery_records` 存在、ledger 有 v4_014；
4. 重建空库并 `pg_restore`；
5. 确认 `delivery_records` 不存在、ledger 回到 v4_010。

实测结果：

```text
rollback drill PASS: baseline=9 upgraded=13 restored=9
```

这验证了“升级前备份 + 恢复备份”的回滚路径；不是零停机部署演练，也不是跨主机仓库迁移。
