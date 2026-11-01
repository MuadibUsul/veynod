# PHASE1G_REPORT — Token/Lease 收紧与 Admin 审计

> 日期：2026-10-31
> 范围：Phase 1G：watcher 校验 lease/token、token revoke 终止 VERIFYING、生产可选强制绑定、admin audit 事件
> 状态：本地实现 + 测试通过；未提交 git；AgentGraph 镜像未重建

---

## 1. 本次完成

### 1.1 Watcher 不再只看 CANCELLED

`app/services/gate_worker.py::_watch_cancellation`：

- 一次查询 `status` + `lease_token_hash`；
- 以下任一条件都取消 in-flight gate：
  - 任务不存在；
  - 状态不是 `LEASED`/`VERIFYING`（含 CANCELLED/FAILED/READY/COMMITTED）；
  - `lease_token_hash` 与当前 submission 的 lease 不一致。
- 效果：取消、token revoke、lease 被重新分配都会停止正在运行的 gate，而不是等到 merge fence。

### 1.2 Token revoke/rotate 终止 VERIFYING

`app/api/admin_routes.py::_release_runner_leases`：

- 覆盖 `LEASED` 和 `VERIFYING`；
- `VERIFYING` → `FAILED`（不允许已撤销 runner 的验证结果继续）；
- 同时把该任务 `QUEUED`/`RUNNING` submission 置为 `FAILED` + `error_code=runner_token_revoked`；
- 清 work lease，避免旧 worker finalize 成功；
- 事件/核心同步沿用原有路径。

### 1.3 生产可选强制绑定 token

新增配置：

```text
VN_REQUIRE_BOUND_TOKENS=false|true
```

- `false`（默认）：legacy 未绑定 token 仍可用；
- `true`：`workspace_id IS NULL` 的 token 在认证时直接拒绝（dev 未知 token 也要求有默认 workspace）。

这允许生产先吊销/重签 legacy token，再开启强制绑定，不需要一次性破坏所有现有 runner。

### 1.4 Admin audit 事件

- `revoke` → `TOKEN_REVOKED`；
- `rotate` → `TOKEN_ROTATED`；
- 事件写入 token 绑定 workspace 的 `kernel_events`/`event_outbox`；
- payload 包含 token_id、worker_hub_id、label、released_tasks；rotate 还包含 old_worker_hub_id。

> 未绑定 legacy token 没有 workspace，无法写 workspace 级审计；需要先绑定或吊销。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| Kernel 非集成 | **98 passed** |
| AgentGraph 非集成 | **76 passed, 28 skipped** |
| contracts | **5 passed** |
| 集成回归 | **6 passed**：token lifecycle、require-bound、full flow、work leases（2）、restart |

新增/更新测试：

- watcher：CANCELLED 任务、lease hash 不匹配两种情况都会取消 gate；
- token lifecycle：revoke 后任务回 READY/FAILED，旧 token 401；audit 事件包含 `TOKEN_REVOKED`；
- require-bound：`VN_REQUIRE_BOUND_TOKENS=true` 时未绑定 token 401，绑定 token 200。

---

## 3. 线上状态

- Kernel API/Worker 已重启；32 条路径，`:8030` 正常，0 pool timeout。
- 未新增迁移。
- `VN_REQUIRE_BOUND_TOKENS` 默认仍为 false；现有 2 个 legacy token 未绑定。
- DAG 未授权 401；dashboard 404。

---

## 4. 尚未完成

1. 后台/事件投影到 AgentGraph（仍按需 GET/sync）。
2. AgentGraph 运行镜像重建（v3_68、code_engine、投影均未进镜像）。
3. 统一 DagTaskCommandService 的其余命令。
4. 取消/失败事件的自动消费。
5. 多 workspace/Repository Registry。
6. 没有 commit。

---

## 5. 下一步（Phase 1H / Phase 3）

1. AgentGraph 后台 projection loop 或事件消费者；
2. AgentGraph 镜像重建 + 真实 code run 端到端（创建/取消/失败/证据）；
3. 统一 DagTaskCommandService；
4. 生产开启 `VN_REQUIRE_BOUND_TOKENS=true` 并吊销 legacy token；
5. 之后进入 Phase 2 Durable Agent Runtime。

---

## 6. 结论

Token revoke/rotate 现在能终止 `LEASED` 和 `VERIFYING` 的任务并失败其 submission；watcher 也会在 lease 丢失/换人时停止 gate；生产可以显式要求绑定 token；revoke/rotate 有 workspace 级审计事件。**自动投影、运行镜像和多租户调度仍未完成。**
