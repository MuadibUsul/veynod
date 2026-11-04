# PHASE1F_REPORT — In-flight Sandbox 终止

> 日期：2026-10-31
> 范围：Phase 1F 第一批：Gate Worker 取消 watcher + sandbox 进程树终止
> 状态：本地实现 + 测试通过；未提交 git；AgentGraph 镜像未重建

---

## 1. 本次完成

### 1.1 Gate Worker 取消 watcher

`app/services/gate_worker.py`：

- `process()` 不再直接 await gate，而是：
  - `gate_task = create_task(_process_with_gate(...))`
  - `watcher = create_task(_watch_cancellation(...))`
- `_watch_cancellation()`：
  - 每 `VN_CANCEL_POLL_SECONDS`（默认 2s）查询 `dag_tasks.status`；
  - 任务变为 `CANCELLED` → `gate_task.cancel()`；
  - watcher 与 heartbeat 在 finally 中清理。
- `process()` 区分“内部取消”和“worker shutdown”：
  - 内部取消：记录日志并返回，worker 继续服务；
  - shutdown：继续抛 `CancelledError`，由 worker 生命周期处理。
- 若 gate 已完成，watcher 自然退出；取消后不创建新的 merge intent。

### 1.2 Sandbox 取消清理

`app/services/gate_sandbox.py`：

- local 模式：`CancelledError` → `_kill_tree(proc)`（Windows `taskkill /T /F`，POSIX 进程组 SIGKILL）→ 等待 `communicate` → 重新抛出；
- docker 模式：`CancelledError` → `docker kill` → `proc.kill` → 等待 → re-raise；
- `finally` 仍清理临时 HOME / `docker rm -f`。

这样取消不再只是“阻止合并”，而是能终止正在运行的 pytest/pyright 进程树。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| Kernel 非集成 | **97 passed** |
| AgentGraph 非集成 | **76 passed, 28 skipped** |
| contracts | **5 passed** |
| 受影响集成回归 | **5 passed**：full flow、cancel、work leases（2）、restart |

新增测试：

- `tests/test_gate_sandbox_cancel.py`：启动一个写 `started` 后 sleep 5s 再写 `done` 的进程，取消后 `done` 不存在 → 进程树被杀。
- `tests/test_gate_worker_cancel.py`：fake session 返回 CANCELLED，inflight gate task 被 watcher 取消并设置 cancelled 标志。

---

## 3. 尚未完成/限制

1. Watcher 只识别 `CANCELLED`；token revoke/lease 被回收但任务未 CANCELLED 时，gate 仍可能继续到 merge fence 才停。
2. Watcher 轮询默认 2s，取消不是瞬时；最多约 2s 延迟。
3. 没有后台投影；AgentGraph 仍需主动 GET/sync 才同步取消/失败。
4. 生产仍未强制拒绝 legacy 未绑定 token。
5. token revoke/rotate 仍没有独立 admin 审计事件。
6. AgentGraph 运行镜像未重建。
7. 没有 commit。

---

## 4. 下一步（Phase 1G / Phase 2）

1. Watcher 同时检查 lease token/status，token revoke 立即停 gate；
2. 后台/事件投影（Kernel Outbox → AgentGraph）；
3. 生产 `VN_REQUIRE_BOUND_TOKENS=true`；
4. token admin audit 事件；
5. AgentGraph 镜像重建 + code_engine 端到端；
6. Phase 2 Durable Agent Runtime。

---

## 5. 结论

取消现在能终止正在运行的 gate sandbox 进程树，而不只是阻止后续 merge。**但 token revoke 的 in-flight gate、自动投影和运行镜像仍未闭环。**
