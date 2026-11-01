# PHASE0_AUDIT_REPORT — Veynod Phase 0 审计报告

> 日期：2026-10-31
> 范围：`E:\Codes\veynod`（AgentGraph + veynod_kernel + veynod_platform + scripts + .run）
> 性质：只读架构审计 + 可执行验证；未修改原有文档/源码（用户指令的 Kernel 运行副本替换除外）
> 结论先行：**Phase 0 审计完成；架构就绪门槛未通过；不得直接进入 Phase 2+ 大规模重构。**

---

## 1. 当前架构真实状态

### 1.1 真实拓扑

```text
AgentGraph :8000（产品控制面，3.24.0）
  -> code_engine HTTP + X-Admin-Key
  -> Kernel :8001（headless 代码引擎，4.0.0，最新副本）
       -> PostgreSQL :55432 / veynod_dev
       -> Gate Worker :8030（8 workers + MergeCoordinator）
  -> PostgreSQL agentgraph（48 表）
```

- AgentGraph 是唯一用户入口；Kernel 无产品登录。
- Kernel 的 6 个 syscall 不带 workspace_id，默认/最老 workspace；一个实例主要服务一个 repo。
- Gate/Merge 在独立 `app.worker`；只启动 API 时 v2 提交会停在 QUEUED。
- AgentGraph 没有持久化 Agent Loop；模型调用在外部 runner。
- 没有 Project/Repository/CodingSession 实体。

### 1.2 现场修复记录

审计开始时 :8001 是 10:51 启动的旧进程：23 routes、无 `/v2`、DB 只有 genesis 8 表。按用户指令：

1. 停止旧进程；
2. 旧目录备份到 `E:\Codes\backup_veynod_kernel_20261031-2230`；
3. 从 `E:\Codes\veynod_kernel` 复制最新副本（源码递归对比 0 差异）；
4. 重启 API：28 routes，`/v2/*` 生效；
5. 启动 Gate Worker（8 workers，:8030）；
6. 应用 `v4_002`–`v4_008`（`schema_migrations` 仅记录 002–005）。

### 1.3 验证结果

| 验证 | 结果 |
|---|---|
| AgentGraph 非集成测试 | 68 passed / 28 skipped / 448 deselected |
| Kernel 非集成测试 | 79 passed |
| Kernel 选定集成测试 | 6 passed |
| Rust 测试 | 14 passed / 1 ignored |
| 2-task production smoke | 通过（8.2s） |
| 100-runner production | 100/100 COMMITTED，126.5s；accept P99 8,015.51ms **失败** |
| 500-runner production | 500/500 COMMITTED，657.7s；**验收失败：accept P99 60,156.82ms；claim transport 3.31%；submit transport/retry 14.24%** |
| 1,000-runner 当前复测 | 未执行；历史未通过 |

---

## 2. 最值得保留的技术资产

1. **Kernel 符号级锁代数 + Rust SchedulerCore**
   `rust/locks.rs`、`scheduler_core.rs`、`app/engine/lock_algebra.py`；14 个 Rust 测试 + 11 个 Python 锁测试；文件/符号/祖先锁语义有真实覆盖。
2. **Kernel claim 的 DB 条件 UPDATE fence**
   Rust 决策后 `WHERE status='READY' AND lease_token_hash IS NULL AND NOT EXISTS(锁重叠)`；stale core 重载重试。
3. **v2 异步提交 + 幂等键**
   `patch_submissions` 绑定 `(task, lease, patch_sha)`；202 + status_url；重启后提交不丢；`test_submission_restart_e2e.py`。
4. **Gate Worker 的 work lease / heartbeat / fencing**
   `status='RUNNING' AND work_lease_id` 才能 finalize；`test_work_leases.py` 覆盖 crash reclaim 和 stale worker 拒绝。
5. **Merge Coordinator 的 per-workspace 选举 + Git CAS + 双向对账**
   `git update-ref <new> <old>`；Git-first/DB-first reconcile 有测试；不同 repo 可并行。
6. **四阶段确定性门禁**
   写集边界、AST 范围、契约哈希、静态检查/测试；AST/契约/锁有负向测试。
7. **AgentGraph 任务/租约/事件基础**
   11 状态、claim/fencing/epoch、触发器 outbox、poll cursor、inbox watermark、webhook 幂等/DEAD；55 项任务/租约测试、8 项事件测试。
8. **AgentGraph 身份/租户/RLS**
   Principal/RBAC、token 哈希、租户 GUC、跨租户 fuzz、RLS DB 测试。
9. **Kernel admission/outbox/edge job**
   429 + Retry-After、admission_permits、event_outbox、durable edge queue；有集成/单元测试。
10. **可复现的压测 harness**
    `bench_swarm.py` 独立 DB/API/worker、真实 HTTP/Git/pytest、客户端 vs DB 对账、严格 exit code。

---

## 3. 重复和冲突模块

| 重复/冲突 | 现状 | 风险 |
|---|---|---|
| 两套代码任务路径 | AgentGraph diagrams/chunk + Kernel code run | 双主路径、状态不一致 |
| 两套 Runner | AgentGraph CLI runner、Kernel CLI runner | 抢任务/协议分叉 |
| 两套事件 | workspace_events 触发器 outbox、Kernel kernel_events/event_outbox | 无统一投影/correlation |
| 两套任务状态 | collab_tasks、dag_tasks | 产品状态失真 |
| 两套调度 | AgentGraph placement scheduler、Kernel DAG scheduler | 职责混淆 |
| 两套 gate 证据 | quality_gate_runs、gate_results | 合成 PASS |
| 两套 dashboard | AgentGraph Next.js、Kernel `/dashboard` | Kernel debug 未鉴权 |
| v1/v2 提交 | v1 410/sync、v2 202 | MCP 仍绑 v1，默认不可用 |
| 多写入任务状态 | AgentGraph 9+、Kernel 6+ | 非法转换/竞态 |

---

## 4. 需要重构的模块

| 模块 | 原因 | 目标 |
|---|---|---|
| AgentGraph 任务/租约/协作/评审/图表/审计写入 | 状态多写入、无单一 FSM | TaskCommandService + 转换矩阵 |
| Kernel scheduler/mutator/gate/merge 状态写入 | 多写入、部分无锁/无 token 复核 | DagTaskCommandService + 统一守卫 |
| `code_engine.py` | 未跟踪、零测试、合成 gate | 纳入 git、读真实 gate、补契约/失败/取消 |
| `verification_gate.merge_verified` | merge 不复核 task lease/submission | 临界区复核 + 负向测试 |
| `gate_sandbox.local` | 主机执行、无隔离 | Docker/K8s 非 root/网络/资源限制 |
| `submission_service.accept` | TOCTOU | 条件 INSERT/行锁 |
| `merge_coordinator._finish_intent` | 允许 token NULL | 禁止 null token；受控 reconcile |
| 事件/Sse | id/seq 不一致、无 DLQ/correlation | 统一 envelope + 投影 |
| AgentGraph dashboard | 无 Project/Review/Observability | Phase 7 产品化 |
| Kernel migrations | ledger 不完整、无回滚 | 补 ledger/回滚/备份演练 |

---

## 5. 可以直接复用的模块

- Rust 锁/AST/SchedulerCore；
- Kernel claim 的 SQL 条件 fence；
- v2 submission/idempotency/work lease；
- Merge CAS/对账；
- 四阶段 gate 的 AST/契约/测试骨架；
- AgentGraph Principal/RBAC/RLS；
- workspace_events 触发器 + poll cursor + inbox watermark；
- webhook 幂等/重试/DEAD；
- AgentGraph Runner host/placement（作为编排层）；
- 压测 harness；
- 现有 93/19 个测试文件中的任务/租约/事件/门禁/合并测试。

---

## 6. 必须淘汰的旧路径

1. **AgentGraph→Kernel 合成 gate 证据**：不能保留。
2. **Kernel v1 sync submit**：默认 410；迁移窗口后删除。
3. **MCP submit_patch 绑 v1**：改为 v2。
4. **AgentGraph diagram/chunk 作为代码执行主路径**：降级为协作编排。
5. **Kernel debug `/dashboard` 公网暴露**：关闭/内网。
6. **`veynod_platform` 主链路残留**：归档/标注，不再维护。
7. **`SUSPENDED` 不可达状态、`CONFLICT` 未写状态、Rust `run_shell` 未用路径**：实现或删除。
8. **默认 `local` gate 作为生产执行**：淘汰。
9. **默认弱 admin key / 0.0.0.0 文档**：淘汰。

---

## 7. 数据和状态风险

1. AgentGraph 任务状态 9+ 写入方；cancel 写 FAILED，子任务/图表写 CANCELLED；CANCELLED 非 DB terminal。
2. Kernel 状态 6+ 写入方；`SUSPENDED` 不可达；无 CANCELLED；上游 FAILED 不递减下游。
3. merge 不复核 task lease/submission；过期/重排任务可 merge；Git/DB 分叉。
4. 多 submission 同一 lease 可假 COMMITTED。
5. test-time mutation 提交未重验。
6. tree merge 未端到端验证；snapshot 复制可能不匹配。
7. INJECT 可创建任意写锁。
8. AgentGraph gate 证据合成；quality_gate_runs 不可信。
9. 事件协议不一致、无 correlation/DLQ；SSE id≠seq。
10. 迁移 ledger 不完整；Kernel 无回滚/备份演练。
11. 审计表非 append-only；DSAR 可改事件 payload。
12. 无 Project/Repository/CodingSession/Agent Run/Delivery 实体。

---

## 8. 并发和容量瓶颈

1. 100-runner：100/100 COMMITTED，但 accept P99 8.0s > 1s（当前失败）。
2. 500/1,000：本轮 500/500 COMMITTED，但 accept P99 60.2s、claim transport 3.31%、submit transport/retry 14.24%，验收失败；历史 1,000 881/1,000，P99 69.5s，未通过。
3. Gate pool：1,000 时 P50 等待 114–191s；8 workers 在 12 CPU 最优（16 更差无原始产物）。
4. Merge 同 repo 串行/锁等待；跨 repo 并行有限。
5. DB pool：500 曾 17 timeouts。
6. SSE：1.36M frames/421s；连接层有基础但非活跃 Agent。
7. AgentGraph 单 FastAPI 进程；无 1,000 活跃 Agent 压测。
8. 多仓库/Cell 未实现，无法水平扩展单实例。
9. 指标口径必须区分 Connected/Active Run/Execution/Mutation/Gate/Merge。

---

## 9. 安全问题

1. Kernel 无租户；runner token 全局；任意 token 可读任意 task/stream；DAG/dashboard 未鉴权。
2. 默认 local gate 主机执行 Agent 代码；无网络/文件系统隔离。
3. 默认弱 admin key（`change-me`/`dev-admin`）+ 文档 `0.0.0.0` + HTTP 明文。
4. AgentGraph 用户可触发 code run（受 task:create），共享一个 Kernel workspace。
5. AgentGraph RLS 依赖 `activate_tenant`；未激活时 fail-open；compose DB 是 superuser；`RLS_REQUIRE_CONTEXT` 未使用。
6. 无 Kernel token revoke/rotate/list；认证缓存 5s。
7. 无取消；worker 丢失 lease 后 merge 仍可能推进。
8. Docker sandbox 无镜像/非 root/seccomp/网络策略/测试。
9. 无 admin-auth 负向测试、token 撤销测试、多 workspace 隔离测试。
10. CORS 默认 `*`；SSRF 默认允许私网；明文 HTTP。

---

## 10. Phase 1 最小实施范围

1. 冻结 ID/身份链（Product Task/Agent Run/Workflow/DAG/Attempt/Submission/Commit/Delivery）。
2. 统一事件 envelope（event_id/version/correlation/causation/aggregate version）。
3. AgentGraph `TaskCommandService` + Kernel `DagTaskCommandService`；所有状态写入经命令。
4. 取消/重试/超时语义；Kernel 增加 CANCELLED；取消传播到 worker/sandbox/merge。
5. merge 临界区复核 task status + lease token + submission status。
6. 删除合成 gate 证据；AgentGraph 读 Kernel `gate_results`/`merge_intents`。
7. code_engine 纳入 git；补契约/失败/取消测试；CI。
8. 安全默认值：强 admin key、禁用 local gate（生产）、DAG/dashboard 鉴权、token 绑定 workspace。
9. 迁移 ledger 修复（v4_006–008）；Kernel 备份/回滚演练。
10. SSE 使用 seq + has_more；Kernel outbox consumer/DLQ 或明确仅轮询。

**在以上完成前，不启动 Phase 2 的 Agent Runtime 大规模实现，不做数据库合并/微服务拆分。**

---

## 11. 分类台账

| 分类 | 结论 |
|---|---|
| VERIFIED | Kernel 符号锁、v2 异步接受、work lease、merge CAS/对账、AgentGraph 任务/租约/事件、RLS 测试、Rust 14 测试、2-task smoke |
| IMPLEMENTED_NOT_VERIFIED | Docker sandbox、pyright/ruff 真实执行、tree merge、code_engine、1,000 当前复测 |
| PARTIAL | 任务状态机、事件协议、lease/fencing 覆盖、契约验证、依赖影响、快照不可变、取消、多仓库 |
| DOCUMENTED_ONLY | 8-vs-16 gate worker、部分 100-runner 历史数字、Rust 1M 耗时、Platform 当前 smoke |
| MISSING | Kernel 取消、租户隔离、Project/Repository/CodingSession、持久化 Agent Loop、DLQ、correlation IDs、policy/security gate、delivery record |
| UNKNOWN | 生产行为、真实多租户竞争、跨网络故障、1,000 Soak、AgentGraph 全量集成当前状态 |

---

## 12. 最终判定

- **Phase 0 审计交付完成。**
- **Phase 0 架构就绪门槛未通过**：无单一写入权威、无单一状态机、无取消、合成 gate 证据、租户/沙箱安全未达标。
- **Phase 1 未启动**；等待架构审查与授权。
- 本轮没有完成“整个项目重构”，也不应声称完成；严格按计划只做了 Phase 0。
