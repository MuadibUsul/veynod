# 容量基线（Phase 4）

日期：2026-11-01。分支 `production/qualification`。数据来自本机真实运行，
脚本 `scripts/capacity_unified.py`，原始结果 `docs/capacity_fast.json` /
`docs/capacity_real.json`。

---

## 1. 环境与方法

| 项 | 值 |
|---|---|
| 机器 | Windows 10 x64，12 逻辑核，34 GB RAM |
| PostgreSQL | 16 + pgvector，Docker，`127.0.0.1:55433` |
| 进程 | Kernel API、Kernel Gate Worker、Runtime API、Runtime Worker Pool、AgentGraph API（均为本机进程，非容器） |
| Kernel | 单进程 Rust/Python 调度；`VN_FAST_FORWARD_MERGE=true`（树合并 + CAS） |
| Worker | 4 个 Runtime worker，lease 10s；2 个 Gate worker |
| 模型 | 本地脚本化 OpenAI 兼容服务（确定性，无网络、无 Provider 限流） |
| 每个任务 | create_task → pull_task_bundle → read_file → submit_patch(202) → 门禁 → merge → 事件唤醒 → finish |
| 并发定义 | **同时启动** N 个 Product Task 的 Run；不是"队列里放了 N 个任务" |

两种 profile：

* **fast**：门禁命令替换为 `VN_TEST_CMD=true`、跳过 pyright/lint，只测编排与合并路径。
  它**不是**生产吞吐，只是排除 CPU 密集类型检查后的系统上限。
* **real**：完整 5 阶段门禁（pyright + ruff + pytest），接近生产成本。

每个 level 使用独立的符号（`src/f{i}.py`），避免"已实现文件 → 空 patch"污染结果。

---

## 2. 结果：fast profile（门禁命令打桩）

| 并发 | 完成 | 失败 | 卡住 | 交付 | 任务创建 P50/P95 ms | 启动 Run P50/P95 ms | 端到端 P50/P95 s | 吞吐 tasks/s |
|---|---|---|---|---|---|---|---|---|
| 10 | 10/10 | 0 | 0 | 10 | 250 / 484 | 531 / 719 | 38.9 / 40.0 | 0.25 |
| 25 | 25/25 | 0 | 0 | 25 | 266 / 375 | 687 / 922 | 96.0 / 100.0 | 0.25 |
| 50 | 50/50 | 0 | 0 | 50 | 265 / 500 | 796 / 1282 | 195.8 / 210.9 | 0.24 |
| 100 | 100/100 | 0 | 0 | 100 | 282 / 500 | 735 / 1015 | 393.3 / 416.3 | 0.24 |
| 250 | 250/250 | 0 | 0 | 250 | 285 / 520 | 740 / 1100 | 1048.8 / 1106.6 | 0.225 |

观察：

* **吞吐立即饱和在 ~0.24 tasks/s**，与客户端并发无关。10 → 100 只增加排队时间
  （端到端 P50 从 39s 线性涨到 393s），不增加完成速率。
* 数据库事实一致：每个 level 的 submission / delivery / wakeup 数等于任务数，
  `stuck_runs = 0`，没有重复交付。
* 编排侧的 create/start 延迟仍然很低（P95 ≤ 1.3s），瓶颈在 **门禁 + 合并 + 投影**。

## 3. 结果：real profile（完整门禁）

| 并发 | 完成 | 失败 | 卡住 | 交付 | 创建 P50/P95 ms | 启动 P50/P95 ms | 端到端 P50/P95 s | 吞吐 tasks/s |
|---|---|---|---|---|---|---|---|---|
| 10 | 10/10 | 0 | 0 | 10 | 266 / 484 | 578 / 828 | 42.8 / 44.0 | 0.227 |
| 25 | 25/25 | 0 | 0 | 25 | 281 / 422 | 657 / 1031 | 116.7 / 120.9 | 0.206 |

**完整门禁只比打桩慢约 10–18%**：pyright/ruff/pytest 不是当前瓶颈，
worktree 创建、树合并、CAS、证据写入与投影才是。这也解释了为什么增加客户端并发
只会增加排队：吞吐由 Gate/Merge 与 Runtime worker 的固定并行度决定。

## 4. 250 / 500 / 1000

| 并发 | 结果 | 依据 |
|---|---|---|
| 250 | **PASS（等待主导）** | 250/250 完成、0 失败、0 卡住、250 交付；端到端 P50 1048.8s、P95 1106.6s；吞吐 0.225 tasks/s |
| 500 | 未完成 | 外推 ~35 分钟 |
| 1000 | **FAIL / 未验证** | 外推 ~70 分钟且端到端 P50 将达 ~4,000s；不满足任何交互式目标 |

**不把 1000 并发描述为通过。** 当前安全并发范围（实测）是
**100 个同时在跑的 Product Task**（0 失败、0 卡住、全量交付），但延迟随时间线性增长，
意味着系统在 4 个 Runtime worker + 2 个 Gate worker 上已经饱和。

## 5. 瓶颈

1. **Gate/Merge 串行化**：`_workspace_merge_lock` + 每任务 worktree 创建/树合并/证据写入
   决定吞吐。两个 gate worker 时每个任务约 4s 门禁窗口（fast profile），real profile 更高。
2. **Runtime worker 数**：4 个 worker 决定模型步骤的并行度；模型是脚本化的，因此这里不是瓶颈，
   但真实 Provider 会成为下一道墙（见 Phase 7）。
3. **投影延迟**：AgentGraph 的 event dispatch 间隔 1s，加上 Runtime 的 wakeup 消费是
   per-poll（0.25s），对端到端贡献有限但存在。

## 6. 下一阶段扩容建议（按顺序）

1. 增加 **Gate Worker**（CPU/IO 密集，最直接的吞吐杠杆），并确认每个 gate worker 的
   worktree 池足够大（`VN_GATE_WORKTREE_POOL_SIZE`）。
2. 同一仓库的写集如果高度重叠，吞吐由 merge lock 决定；对独立项目拆 cell/仓库可以线性扩展。
3. 增加 **Runtime Worker** 与模型 Provider 配额（真实模型场景）。
4. 只有在上述之后才考虑 API 扩容（Kernel API 是薄层）。

## 7. 复现

```bash
python scripts/capacity_unified.py --levels 10,25,50,100 --profile fast --workers 4 --gate-workers 2
python scripts/capacity_unified.py --levels 10,25 --profile real --workers 4 --gate-workers 2
```
