# 1000 Runner 容量验收与瓶颈记录（阶段 6）

> 环境：Windows 10 / 12 逻辑 CPU；PostgreSQL 16（容器，`max_connections=300`）；
> 8 个 gate worker；生产 profile（typecheck + lint + pytest 全部启用）。
> 所有测试使用新建隔离库和临时 Git 仓库。

## 1. 本轮优化

| 优化 | 原因/效果 |
| :--- | :--- |
| `git cat-file --batch` 常驻读取池（每仓库 4 条） | 原来每个 claim 启动一个 `git show`/`cat-file` 子进程；现在复用管道，`git.batch_read_ms` p50 27ms，200 个对象一次读取 |
| `VN_DB_POOL_SIZE` / `VN_DB_MAX_OVERFLOW` | 默认 20+40 对多 API 过大；按进程配置，测试 PostgreSQL `max_connections=300` |
| `admission_claim_slots`（默认 32） | 4 个槽位让 1000 并发 claim 排队；32 个槽位把 `http.pre_middleware_ms` p50 从 ~1s 降到 ~90ms（100 Runner） |
| `admission_permits` 改 UNLOGGED | 许可是可重建软状态，去掉每次 claim 的额外 WAL fsync |
| permit 绑定合并进 claim 事务 | 每次 claim 从 3 次 commit 降到 1 次持久 commit |
| 活跃写集部分 GIN 索引 + 三个独立 `NOT EXISTS` | 避免 OR 导致的全表扫描；`claim.update_field_ms` 从 ~520ms 降到 ~23ms（100 Runner，非拥塞时） |
| `scheduler_authorities` 续租缓存 + per-workspace asyncio lock | 避免续租突发排队；authority 热路径不再每次写 DB |
| 认证 token 5s 进程内缓存 | 去掉 claim/submit 每个请求的 token DB 查询；撤销最多延迟 5s |
| 独立 API 实例（`--api-instances`） | 每个实例独立 uvicorn 进程/端口，避免 Windows 上 uvicorn `--workers` 强制 SelectorEventLoop 导致 subprocess 不可用；共享 DB，权威实例唯一 |

迁移：`v4_006_admission_permits.sql`、`v4_007_unlogged_admission_permits.sql`、
`v4_008_active_lock_indexes.sql`。

## 2. 分层实测

所有运行：`--profile production`，gate worker 8，typecheck + lint + pytest 开启。

| 场景 | 任务/ Runner/SSE | 结果 | accept P99 | claim P99 | DB pool timeout | SSE |
| :--- | :--- | :--- | ---: | ---: | ---: | :--- |
| 完整合并 | 100/100/100 | **100 COMMITTED**，0 非终态 | **6.84s** | 6.57s | 0 | 100/100，0 错误 |
| 完整合并 | 500/500/500 | 478 COMMITTED，22 READY* | 64.5s | 61.2s | 0（max_connections=300） | 500/500，0 错误 |
| 仅领取+SSE | 1000/1000/1000 | 589 次成功领取，1000 READY（claim 模式主动持有/释放） | n/a | 73.8s | 0 | **1000/1000 建立**，1,176,250 帧，0 错误 |
| 完整合并 | 1000/1000/1000 | **881 COMMITTED，119 非终态**（104 LEASED/12 VERIFYING/3 READY） | **69.5s** | 89.4s | 0 | 1000/1000，0 连接错误，2,824,357 帧 |

### 门禁 Worker 数量 A/B（500 任务、生产 profile）

- 8 workers：pytest p50 约 3.5s，门禁队列持续增长，端到端约 0.35–0.9 commit/s。
- 临时扩到 16 workers（同一隔离库、同一 API）：pytest p50 升到 **6.5s**，
  总吞吐没有提升（约 0.37/s）。16 个 pytest/pyright 争用 12 逻辑 CPU 与
  Git worktree/config 锁，**8 workers 是本机更优点**；要提升需要更多 CPU、
  常驻 pytest 或仓库分片，而不是继续加 worker。

\* 500 运行中 22 个 READY 是本轮压测器 `--max-empty-claims` 提前停止 Runner 造成；
该停止逻辑已移除，不是服务端丢任务。

### 100 Runner 的细分（生产 profile）

- `gate.STATIC_TYPECHECK` p50 约 3.1s，`gate.UNIT_TEST_SUITE` p50 约 3.5s；
  8 个 worker 的理论门禁吞吐约 1.1 条/s。
- `merge.total_ms` p50 266ms，`merge.lock_wait_ms` p50 0；合并不是门禁前的瓶颈。
- API 接受延迟服务端 `http.submit_ms` p50 约 0.3s、p99 约 1.5s；
  客户端观测 `submit_accept` P99 仍为 6.8s，差额是 API 进程/请求队列。

### 500 Runner

- 端到端约 **0.35 commit/s**（1200s 窗口，478 条）。
- 1000/1000 SSE 对照说明连接可建立；500 SSE 0 连接错误。
- 主要限制：单仓库 gate + pytest/typecheck 吞吐、API 事件循环排队；
  不是 Git merge（merge p50 亚秒级）。

## 3. 验收条件对照

| 条件 | 结果 |
| :--- | :--- |
| 1000 Runner + 1000 SSE 可持续 | 1000 SSE 全部建立、0 错误；但 1000 完整合并只有 881/1000 COMMITTED，**未达到验收** |
| 无连接池超时 | 100/500 为 0（max_connections=300）；早期 500 运行在 100 连接时有 17 次超时 |
| 无丢失/重复可见合并 | 100 一致；500 一致；1000 出现 **10 个 client COMMITTED / DB 未 COMMITTED** 的不一致，需按 final status 重查修复 |
| 所有已接受提交最终终态/停止输入后排空 | **未通过**：500 有 22 个 READY（旧压测器停止逻辑，已修复）；1000 有 119 个非终态 |
| API accept P99 < 1s | **未通过**：100 为 6.84s，500 为 64.5s，1000 为 69.5s |
| 崩溃/断线可查询可恢复 | 既有 `test_submission_restart_e2e`、`test_work_leases`、`test_horizontal_api` 25 项集成测试通过；非 integration **79 passed** |

## 4. 两种提交速率

- **1000 Agent 每 10 分钟提交一次** = 1.67 提交/s。
  - 实测完整合并：100 Runner 约 0.90/s，500 Runner 约 0.35/s；**达不到 1.67/s**。
  - 门禁理论值（8 worker / 约 7s 单条）约 1.1/s，仍小于 1.67/s。
- **1000 Agent 每分钟提交一次** = 16.7 提交/s。
  - 单仓库 merge-only 实测上限约 4.1/s（前序阶段），当前 gate 约 1/s；
    **差 4–16 倍**。
  - 这不是“1000 个在线连接”能替代的指标；连接数已独立验证。

### 需要的容量方案

1. **门禁扩容**：单仓库至少需要 16–32 个 gate worker 或常驻 pytest 进程；
   当前 12 逻辑 CPU 下 8 个已接近 CPU/IO 性价比拐点（需用 16/32 实测确认）。
2. **仓库分片**：单仓库主干序列化与 gate 共享是硬上限；按仓库/目录分片，
   每片独立 gate pool 与 merge coordinator，才能线性接近 16.7/s。
3. **批量合并**：merge-only 4.1/s，不是当前主瓶颈；只有在门禁吞吐提升后再评估
   small-batch merge，且必须在组合树上运行必要测试。
4. **API 层**：本轮的 8 个独立 API 实例只解决进程内事件循环竞争；accept P99
   仍受队列与 DB 影响，需要更大连接池/更高门禁吞吐，或把 claim bundle 预计算。

## 5. 尚未解决的风险

1. **accept P99 未达标**：服务端 `http.submit_ms` P99 约 1.5s（100 Runner），
   客户端 6.8s；500 Runner 64.5s；1000 Runner 69.5s。单进程/单请求队列仍是问题。
2. **500 的 22 个 READY** 是压测器陈旧停止逻辑，已修复；但该轮不作为通过证据。
3. **生产 profile 的 1000 完整合并已完成：881/1000 COMMITTED、119 非终态**；
   不能用 dev profile 的旧 v9 1000/1000 替代生产容量结论。
4. **PostgreSQL max_connections=300** 是测试容器调整；生产需要连接池/代理
   （PgBouncer）或按实例池预算，不能依赖无限连接。
5. **claim 丢失响应**：客户端超时后任务可能保持 LEASED 直到 lease 到期；
   reaper 30s + lease 600s 可恢复，但不是即时。需要 claim 幂等/按 runner 查询。
6. **1000 SSE** 资源：1000 连接、百万帧级已验证 0 连接错误，但生产仍需
   socket/backlog/文件描述符配额。
