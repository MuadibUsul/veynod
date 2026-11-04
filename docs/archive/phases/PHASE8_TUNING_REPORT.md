# PHASE 8 TUNING REPORT — API 平面、Hold-Queue、Gate 与多进程客户端

日期：2026-11-01
范围：针对上一轮根因剖析的 4 项定向改造。

---

## 1. 已落地改造

### 1.1 API 物理平面与后台循环隔离

- `app/config.py` 新增：
  - `VN_API_PLANE=all|submit|claim_sse`
  - `VN_ENABLE_BACKGROUND_LOOPS`
  - `VN_AUTH_CACHE_TTL_SECONDS`（默认 300s）
  - `VN_CLAIM_MAX_WAIT_SECONDS`
- `app/runtime.py::lifespan`：
  - `submit` 平面不启动 EventHub / edge rebuild / outbox / reaper；
  - `claim_sse` 平面启动 EventHub 与 scheduler sync，仅 `VN_ENABLE_BACKGROUND_LOOPS=true` 的实例运行 reaper/outbox/edge；
  - 所有平面仍执行 idempotent `init_db`；
  - 启动时 `warm_token_cache`。
- `app/runtime.py::install_api_plane_guard`：
  - submit 平面只允许 `/v2/syscalls/submit_patch`、`/v2/submissions/*`、健康与 metrics；
  - claim_sse 平面拒绝 submit，避免平面串流。
- `bench_swarm.py`：
  - `--submit-instances N` 把最后 N 个 API 进程设为 submit 平面；
  - 每个平面分配独立 DB pool（submit 更大）；
  - 仅第一个 claim 实例开后台循环。

### 1.2 Auth 缓存

- `app/auth.py`：
  - TTL 从固定 5s 改为 `settings.auth_cache_ttl_seconds`（默认 300s）；
  - 冷缓存首次 miss 用 `warm_token_cache` 批量加载 active token（带 asyncio lock，避免 1,000 请求同时打 DB）；
  - `TOKEN_REVOKED` / `TOKEN_ROTATED` 事件由每个 API 进程的 EventHub 清本地缓存；
  - `/v1/runner/ping` 与 SSE 边界再次检查 `require_bound_tokens`。

### 1.3 服务端 Claim Hold-Queue

- 新增 `app/engine/hold_queue.py::ClaimHoldQueue`：
  - per-workspace `asyncio.Condition` + FIFO 唤醒；
  - 最大 waiter 数保护；
  - `EventHub` 在 `GATE_PASSED/TASK_READY/...` 时唤醒等待者。
- `app/engine/scheduler.py::claim_next_orthogonal_task`：
  - 新增 `wait_seconds`；
  - 无可调度任务/AdmissionDenied 时 `session.close()` 后等待 3–4.5s 切片，不占 DB 连接；
  - 超时才返回/抛 429；
  - `_reserve_admission` 的 permit 快速拒绝路径补齐 leased/submissions/verifying/merge counts。
- `app/mcp_server/syscalls.py::PullTaskBundleRequest` 新增 `wait_seconds`（默认 0，runner 可显式长轮询）。
- 修复长轮询后 workspace 是 detached 实例的问题：claim 成功后重新 `session.get(Workspace, ...)`。
- Claim 成功后 5xx 从“任务 LEASED 但 bundle 500”路径修复（100/100 无 5xx）。

### 1.4 Gate 命令

- `VN_TEST_CMD` 默认改为：
  `pytest -q --tb=short --no-header --no-summary -p no:cov -p no:warnings -p no:doctest -p no:pastebin -p no:junitxml -p no:anyio`
- 状态检查：`UNIT_TEST_SUITE` 从约 4.2s 降至约 3.1s（100 任务样本）。
- 额外实测：本机安装 `ty`（Astral）后，`VN_TYPECHECK_CMD='ty check'` 把 `STATIC_TYPECHECK` 从约 3.5s 降到约 0.3-0.7s；但它是 bench 可选 env，不是 Kernel 默认，生产使用前需评估类型检查语义差异。

### 1.5 多进程 Bench 客户端

- `bench_swarm.py` 新增 `--client-shards N`：
  - `multiprocessing` 把 runner/SSE 分到独立 OS 进程；
  - 每进程独立 `worker_client`、`submit_client`、`sse_client`；
  - `event_hooks={'request': [_stamp_wire_sent_at]}` 在真正写出前打 `X-Bench-Sent-At`；
  - `Manager.Queue` 汇总各 shard 的计数/延迟/任务集合；
  - parent 仍负责 seeding、DB probe、server metrics、cutoff。
- 100-runner 8 shards 结果：100/100 COMMITTED，SSE 100/100、0 重连/0 错误，client accept P99 **1,346ms**（仍高于 1,000ms，但已从 4–8s 级降到 1.3s）。

---

## 2. 100-runner 结果对比

| 配置 | 最终 | client accept P99 | server handler P99 | SSE |
|---|---:|---:|---:|---:|
| 单进程，旧版 | 100/100 | 8,597ms | 942ms | 0 |
| 平面 + 长轮询 + Auth 缓存，单进程 | 100/100 | 4,472ms | 2,082ms | 0 |
| 平面 + 长轮询 + Auth 缓存 + ty | 99/100 | 4,148ms | 2,212ms | 0 |
| **8 client shards + 平面 + ty** | **100/100** | **1,346ms** | **708ms** | **0 错误** |

100 级已经接近 target：server handler 708ms，SSE 完美；只剩 client P99 约 1.35s。

---

## 3. 1,000-runner 结果（真实）

### 3.1 单客户端（`phase8-1000-final2`）

- **1000/1000 COMMITTED**，0 non-terminal，总 1,256.7s（drained）；
- server accept handler P99 **687.3ms**；
- SSE 1000/1000 打开，**0 重连/0 错误**；
- claim/ submit transport error 仍为 24.1% / 28.7%，client accept P99 **54.4s**；
- acceptance **false**；
- 说明：服务端已达标，单客户端进程仍是瓶颈。

### 3.2 4/8 client shards

- 4 shards 启动后，1000 文件仓库的 `pytest` 单任务变为 9–14s（12 核 CPU 与 4 个客户端进程、8 个 API、8 个 gate 竞争）；
- 运行约 10 分钟只完成 74/1000，主动停止；没有把它当作通过结果；
- 结论：**多进程客户端减少了客户端自锁，但在 12 核机器上与 gate 抢 CPU，不能同时把 gate 吞吐和客户端吞吐都拉满**。

---

## 4. 当前结论

- 4 项改造都已落到代码，默认 `VN_API_PLANE=all`、`wait_seconds=0` 保持向后兼容；
- 100 级从 “P99 4–8s” 降到 **1.35s**，服务端 handler 708ms，SSE 无错误；
- 1,000 级单客户端 **1000/1000** 但 P99 54s，未通过；
- 1,000 级多客户端因 12 核 CPU 与 gate 竞争未能完成 30 分钟窗口，未通过；
- **仍不能宣称 1,000 Runner 生产验收通过。**

## 5. 下一步

1. 客户端分片数量与 gate worker 数量做 CPU 预算（例如 2 shards + 4 gate workers，或把 gate 放到独立机器/Cell）；
2. 把 `ty`/dmypy 的增量类型检查做成正式 gate 选项，并验证语义等价；
3. 降低 merge 串行成本（当前 1,000 文件仓库的 merge/artifact sync 是第二个吞吐瓶颈）；
4. 在 Linux + Docker gate + 独立客户端主机上复测，当前 Windows local-gate 的 subprocess/CPU 数据不能直接外推生产。
