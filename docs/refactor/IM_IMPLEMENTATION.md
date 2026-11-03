# `veynod_im.md` 落地记录

方案见 `docs/veynod_im.md`。按它的四步作战顺序执行，全部四步均已落地；契约哈希守卫那部分
（方案 §7）在本轮之前已由 Phase 1/2 完成，这里只补它缺的投影与对账。

## 与方案的四步对应

| 方案步骤 | 落地情况 |
|---|---|
| Step 1 `/v1/ws/nerve` 双勾毫秒级派发 | 完成。`contracts/nerve_protocol.py`、`app/services/nerve_hub.py`、`app/api/ws_nerve_routes.py`、`cli/bin/veynod-messenger.mjs`、`runner.mjs` 的单次 poke 入口、`npx veynod listen --nerve`、迁移 `v3_81`。 |
| Step 2 收口 `collab_tasks` 状态写入 | 完成。`task_lifecycle.transition_task_fenced` / `end_task_lease_fenced` 带 epoch CAS；`diagrams.py` 两处直写已改走它。 |
| Step 3 剥离 `main.py` 后台循环 | 完成。`ENABLE_INLINE_BACKGROUND_LOOPS` 开关 + `app/worker.py` 独立进程入口。 |
| Step 4 Diagram ↔ Kernel 融合 | 完成。见 `phase-2/RESULT.md`；另加对账器补投丢失事件。 |

## 与方案的偏离（都写了理由）

1. **不新增 `dispatch_state` 之外的 Lease 表**。方案给 `leases` 加 `lease_epoch`/`worker_pid`，
   实际 `leases.fencing_token` 本来就承担纪元，`worker_pid` 只用于网关侧审计，因此只在
   `poke_deliveries` 记录。`agent_presence` 的 `host_id`/`agent_id` 拆分照做了。
2. **`agent_presence` 没有 `updated_at`**（方案里假设有）。刷新用 `seen_at`，离线判定同时用
   内存 `last_seen_ms`，由进程内 reaper 每 3 s 关掉超过 12 s 没帧的 socket。
3. **不替换 SSE**，先并行。`_offer_work` 仍写 `TASK_PLACED` 事件，nerve 只加一层
   "到达即确认"，因此没有 nerve 连接的宿主完全不受影响（`skipped` 计数）。方案说"废弃"，
   现在是"优先 nerve、SSE 兜底"。
4. **`auth.refresh` 用轮换而不是滑动续期**。`token_tree.rotate_secret` 保持同一 token id 与
   15 分钟旧密文宽限，语义上等同于方案想要的"不断连换发"，且复用既有审计。
5. **`NOTIFY` 没接**。方案 §3 的 `veynod_nerve_dispatch` 触发器已写进 `v3_81`，但
   `LISTEN` 协程这次没做：调度仍是 5 s 轮询 + 双勾硬超时，先把可靠性补齐。见"未做"。

## 验证

| 命令 | 结果 |
|---|---|
| `pytest tests/test_nerve_protocol.py` | 10 passed（纯内存：两勾、200 ms、5000 ms、忙拒、顶号、心跳、帧解析） |
| `pytest tests/test_nerve_dispatch.py` | 7 passed（真实 WS + DB：握手、身份错配 403、双勾成功、200 ms 静默超时并把任务放回、只有第一勾则强杀、顶号关闭旧连接、通道内换 token） |
| `pytest tests/test_diagrams.py` | 21 passed（fenced 化之后） |

200 ms 超时的用例断言的是"任务回到 READY 且不再等 90 s 存在感窗口"，不是日志字样。

## 未做

* `LISTEN veynod_nerve_dispatch` 协程：触发器与 payload 已就位，缺消费者。
* `npx veynod listen --nerve` 已经是新入口，但 `service install` 仍默认装 SSE 版
  （要 `--nerve` 得改 `listenArguments`，会改变既有服务的启动参数）。
* 生产 compose 没有 `agentgraph-worker` 服务项；`ENABLE_INLINE_BACKGROUND_LOOPS=0` 需要
  运维手工把 `python -m app.worker` 跑起来。
* 第二勾的触发点在"平台已接受该放置"（`runner.mjs` 的 `IPC_ORIENTATION_READY`），
  早于真正越过模型 orientation。5 秒窗口因此更严，不会漏掉卡住的进程，但也把
  "接受后立刻崩"算作成功——随后 `work()` 的失败仍会 `report(failed)`。
