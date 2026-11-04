# PHASE1D_REPORT — Token 生命周期与租约回收

> 日期：2026-10-31
> 范围：Phase 1D 第一批：Kernel runner token 的 list / revoke / rotate，以及吊销/轮换时释放租约
> 状态：本地实现 + 测试通过；未提交 git；AgentGraph 运行镜像未重建

---

## 1. 本次完成

### 1.1 Admin token API

新增：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/v1/admin/tokens` | 列出 token（无 secret）：token_id、worker_hub_id、label、workspace_id、active、时间 |
| POST | `/v1/admin/tokens/{token_id}/revoke` | `active=false`；立即使缓存失效；释放该 runner 的 LEASED 任务 |
| POST | `/v1/admin/tokens/{token_id}/rotate` | 生成新 secret、替换 token_hash/worker_hub_id；旧 secret 立即失效；释放旧 runner 的 LEASED 任务 |

- 所有接口要求 `X-Admin-Key`。
- `clear_token_cache()`：吊销/轮换后清空 5 秒身份缓存，避免旧 token 继续通过缓存。
- `_release_runner_leases()`：
  - 将该 runner 的 `LEASED` 任务按 retry 规则置为 `READY` 或 `FAILED`；
  - 清空 lease 字段；
  - 写 `TASK_RELEASED`/`TASK_FAILED` 事件；
  - 同步 Rust SchedulerCore。

### 1.2 安全含义

- 泄露的 token 可以立即吊销，不需要改数据库。
- 轮换后旧 secret 立即失效，服务端只存哈希。
- 被吊销 runner 持有的任务不会继续被它 heartbeats/release/submit；任务回到队列或失败。
- 旧 token（workspace_id NULL）仍可列出并吊销；新 token 可绑定 workspace。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| Kernel 非集成 | **95 passed** |
| AgentGraph 非集成 | **75 passed, 28 skipped** |
| contracts | **5 passed** |
| 最终集成回归 | **8 passed**：merge coordinator（3）、token lifecycle、workspace binding、full flow、restart、horizontal |
| 累计相关集成 | **15 个场景**（含 admission/work/edge/cancel 等） |

token lifecycle 集成测试覆盖：

- 创建绑定 workspace 的 token，GET list 可见且 active；
- 用该 token pull 任务；
- revoke 后 active=false，任务回到 READY/FAILED，旧 token ping 401；
- rotate 后旧 secret 401，新 secret 200。

---

## 3. 线上状态

- Kernel API 已重启：**32 条路径**，包含 token list/revoke/rotate。
- `v4_002`–`v4_010` 已应用。
- 现网列出 **2 个 legacy token**，`workspace_id=null`、active=true；它们仍是旧全局语义，生产应重新签发或吊销。
- Gate Worker 正常（:8030）。

---

## 4. 尚未完成

1. **取消事件投影**：Kernel `TASK_CANCELLED` 仍未自动更新 AgentGraph 产品任务。
2. **in-flight sandbox 主动终止**：revoke/cancel 只释放租约，不杀正在跑的 pytest。
3. **生产禁止未绑定 token**：legacy token 仍可用；需要开关/迁移。
4. **AgentGraph 镜像重建**：v3_68 和 code_engine 改动未进入运行镜像。
5. **统一 DagTaskCommandService**：claim/release/commit 等仍分散。
6. **无审计事件**：token revoke/rotate 没有独立的 admin audit 事件（目前只有任务释放事件）。
7. **没有 commit**。

---

## 5. 下一步（Phase 1E / Phase 2）

1. token revoke/rotate 写 admin audit 事件；
2. 生产模式拒绝未绑定 token（可配置宽限期）；
3. 取消/失败事件投影到 AgentGraph；
4. in-flight sandbox 终止；
5. AgentGraph 镜像重建 + code_engine 端到端；
6. 开始 Phase 2 Durable Agent Runtime。

---

## 6. 结论

Token 从“只能创建”变成了可列出、可吊销、可轮换，并且吊销/轮换会回收租约；但取消事件投影、运行中 sandbox 终止和 AgentGraph 运行镜像仍未完成。
