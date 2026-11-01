# RISK_REGISTER — Phase 0 风险登记

> 评分：严重度 S1（阻断生产/安全）、S2（严重）、S3（中等）、S4（低）；可能性 H/M/L。
> 状态：OPEN / MITIGATED / ACCEPTED / DEFERRED。
> 证据路径相对于 `E:\Codes\veynod`。

---

## 1. 风险总表

| ID | 风险 | 严重度 | 可能性 | 状态 | 证据 | 影响 | 缓解 | 阶段 |
|---|---|---|---|---|---|---|---|---|
| R-001 | merge 不复核 task lease/status，过期/重排任务仍可推进 Git | S1 | M | OPEN | `merge_coordinator.py:229-266`、`verification_gate.py:414-444`、`scheduler.py:629-631` | Git/DB 分叉、错误代码进入 main | merge 临界区复核 task status + lease token + submission status；失败回滚/阻断 | Phase 1/6 |
| R-002 | AgentGraph gate 证据合成（硬编码 PASS） | S1 | H | OPEN | `code_engine.py:285-302` | 交付证据不可信、审计失败 | 读取 Kernel `gate_results`/`merge_intents` 真实证据；删除合成路径 | Phase 1/3 |
| R-003 | Kernel 租户隔离不完整：token 可绑定 workspace、读面/SSE/DAG 已收紧、可启用 `VN_REQUIRE_BOUND_TOKENS`；但默认仍允许 legacy token，syscall 仍单 workspace | S1 | H | OPEN（部分缓解） | `auth.py`、`runner_routes.py`、`syscalls.py`、`v4_010_token_workspace.sql`、`PHASE1C/G_REPORT.md` | 默认配置下旧 token 仍可跨 workspace | 生产开启 require-bound；重签 token；多 workspace Registry | Phase 1H/4 |
| R-004 | 默认 local gate 在主机执行 Agent 代码 + 默认 admin key + 0.0.0.0 文档 | S1 | H | OPEN | `config.py:294-316`、`.env.example:9-21`、README/架构文档 `0.0.0.0` | 主机 RCE、凭证泄露、租户逃逸 | 生产禁用 local；强制强 admin key；绑定 loopback/内网；Docker/K8s 沙箱 | Phase 1/6 |
| R-005 | 取消闭环：Phase 1B/1F 已有 CANCELLED/API/传播/in-flight sandbox 终止；仍缺取消事件自动投影、token revoke 期间 VERIFYING gate 的主动停止 | S1 | H | OPEN（部分缓解） | `task_commands.py`、`gate_worker.py`、`gate_sandbox.py`、`PHASE1F_REPORT.md` | 取消不是瞬时；部分 in-flight 场景仍可能跑到 merge fence | 扩展 watcher 校验 lease/token；事件投影 | Phase 1G/3 |
| R-006 | AgentGraph 任务状态 9+ 写入方，无单一 FSM/命令层 | S2 | H | OPEN | `tasks.py`、`leases.py`、`collab.py`、`scheduler.py`、`reviews.py`、`diagrams.py`、`audit.py`、`quality.py` | 非法转换、状态回退、审计不可解释 | TaskCommandService + 转换矩阵 + 单写入 | Phase 1 |
| R-007 | Kernel DAG 状态 6+ 写入方；`SUSPENDED` 不可达；无 CANCELLED | S2 | H | OPEN | `models.py:54-63`、`scheduler.py`、`mutator.py`、`verification_gate.py`、`merge_coordinator.py` | 状态歧义、无法取消、测试误导 | 统一状态命令/守卫；删除/实现 SUSPENDED；增加 CANCELLED | Phase 1 |
| R-008 | 上游 FAILED 不递减下游，任务永久 BLOCKED | S2 | M | OPEN | `scheduler.py:619-655` | 队列卡死、非终态任务 | FAILED/SKIP 显式处理下游；BLOCKED reaper；依赖状态传播 | Phase 1/3 |
| R-009 | heartbeat 可复活过期未 reaper 的 lease | S2 | M | OPEN | `scheduler.py:723-745` | 双 worker/旧 worker 继续操作 | heartbeat 检查 `lease_expires_at > NOW()`；fencing 复核 | Phase 1 |
| R-010 | SPLIT/ESCALATE/INJECT 只检查 status，不检查 token/expiry | S2 | M | OPEN | `mutator.py:57-69, 386-405, 496-517` | 旧 runner 操作新 lease；竞态 | 所有变更加 token/expiry/行锁；审计 | Phase 1/5 |
| R-011 | submission accept 先 SELECT 后 INSERT，无行锁（TOCTOU） | S2 | L/M | OPEN | `submission_service.py:177-316` | 过期 lease 接受（gate 会复核，风险降低） | 条件 INSERT/行锁；把 lease 校验纳入同一事务 | Phase 1 |
| R-012 | 多个 submission 同一 lease，第二个可能在 task COMMITTED 后假 COMMITTED | S2 | M | OPEN | `gate_worker.py:235-247`、`v4_002:51-52` | 未验证 patch 被报告成功 | 每个 task/lease 只允许一个 final submission；校验 patch/commit 绑定 | Phase 1/6 |
| R-013 | test-time mutation 在 pytest 后提交，不重跑 AST/contract | S2 | M | OPEN | `verification_gate.py:600-623` | 未验证内容进入 commit | 对验证后的 tree 做哈希绑定；重跑或拒绝测试修改源码 | Phase 5/6 |
| R-014 | tree merge 默认关闭且无端到端测试；snapshot 复制可能不匹配 | S2 | M | OPEN | `verification_gate.py:1049-1188`、`artifact_snapshot.py:252-258` | 静默覆盖/过期快照 | 打开默认或明确禁用；补端到端；按合并内容重建快照 | Phase 5 |
| R-015 | INJECT 可创建任意新写锁，绕过 SPLIT 守恒 | S2 | M | OPEN | `mutator.py:485-561` | 越权扩大写集/冲突 | 授权/子集校验；审计 | Phase 5 |
| R-016 | 事件协议不一致/无 correlation/DLQ；SSE id≠seq | S2 | H | OPEN | `stream_routes.py:149-224`、`events.py:235-253`、`kernel event_bus.py` | 漏事件、投影回退、无法追踪 | 统一 envelope；SSE 用 seq；补 DLQ/retention/correlation | Phase 1/3 |
| R-017 | AgentGraph 状态同步：Phase 1I/1J 已有 Kernel 事件游标（2s 轮询）+ 多 workspace + 任务扫描兜底；仍非 push | S2 | H | OPEN（部分缓解） | `code_engine.py` project_code_events_once；v3_69 cursor；`PHASE1J_REPORT.md` | 多 workspace 创建仍单入口；事件丢失时滞后 | Outbox push/consumer；事件集成测试 | Phase 1K/3 |
| R-018 | code_engine 未跟踪、零测试 | S2 | H | OPEN | `git status`、`tests/` 无引用 | 主链路回归风险、无法审计 | 纳入版本控制；契约/集成测试；CI | Phase 1 |
| R-019 | 无 Project/Repository/CodingSession 实体 | S2 | H | OPEN | `models.py`、dashboard | 产品无法按项目组织、权限/计量粒度不足 | Phase 1 定权，Phase 7 实现 | Phase 1/7 |
| R-020 | 无持久化 Agent Loop/Model Gateway/Checkpoint | S2 | H | OPEN | 无 model client；runner 外部进程 | 不能恢复长任务、不能审计模型/工具调用 | 新建 Agent Runtime；Phase 2 | Phase 2 |
| R-021 | 100-runner 当前 P99 8.0s 验收失败 | S2 | H | OPEN | 本轮 bench JSON | 不能宣称容量达标 | 优化受理/事件循环/池；重复测 | Phase 8 |
| R-022 | 500/1,000 生产 profile 未通过（本轮 500：500/500 但 P99 60.2s、claim/submit transport 3.31%/14.24%；历史 1,000：881/1,000） | S2 | H | OPEN | `.run/bench/phase0-500、full-1000` | 规模上限不确定 | Gate/API/DB/Cell 扩容；Phase 8 验收 | Phase 8 |
| R-023 | 单 Kernel 实例单 workspace 语义，多仓库未实现 | S2 | H | OPEN | `kernel_service_impl.py:93-123` | 无法服务多项目/租户 | Repository Registry/Cell/workspace 维度 | Phase 4 |
| R-024 | Kernel 迁移 ledger 不完整（v4_006–008 未记录） | S3 | H | OPEN | `schema_migrations` 仅 002–005 | 升级审计/回滚困难 | 补 ledger；迁移检查；回滚演练 | Phase 1/4 |
| R-025 | 无迁移回滚/备份演练（Kernel） | S3 | H | OPEN | 无 down migration | 升级失败不可恢复 | 备份/PITR/回滚脚本；演练 | Phase 4/8 |
| R-026 | 审计表非 append-only；DSAR 可改事件 payload | S2 | M | OPEN | `rights.py:707-714, 1216-1225`；`state_audit_diffs` 无 trigger | 合规/取证风险 | 哈希链/append-only；DSAR 走受控流程 | Phase 1/7 |
| R-027 | 默认弱 key/HTTP 明文/ CORS `*` | S2 | H | OPEN | `config.py`、compose | 凭证泄露、浏览器风险 | 生产校验、TLS、CORS 白名单 | Phase 1 |
| R-028 | Token 撤销/轮换/审计 | S3 | M | MITIGATED（Phase 1D/1G） | `admin_routes.py` list/revoke/rotate；清缓存、释放租约、写 TOKEN_REVOKED/TOKEN_ROTATED；未绑定 token 无 workspace 审计 | 泄露后难处置 | 生产重签绑定 token；补全局审计 | Phase 1H |
| R-029 | Kernel `pg_notify` 无 consumer、无 DLQ/retention | S3 | H | OPEN | `outbox.py`、`event_bus.py` | 事件膨胀、延迟 | consumer/DLQ/retention；或明确轮询 | Phase 3 |
| R-030 | AgentGraph SSE 500 重放上限 + 内存队列丢事件 | S3 | M | OPEN | `stream_routes.py`、`app/events.py` | 消费者漏事件 | 用 poll/watermark；补 has_more/续传 | Phase 3 |
| R-031 | 无跨服务契约测试/CI（Kernel/Platform） | S3 | H | OPEN | 无 workflow；code_engine 零测试 | 回归不被发现 | CI + contract tests | Phase 1 |
| R-032 | 文档漂移（表数/工具数/测试数/容量） | S4 | H | OPEN | 多个 docs | 误判现状 | Phase 0 新文档为基线；后续同步 | 持续 |
| R-033 | `veynod_platform` 死代码、残留命名 | S4 | H | OPEN | 无引用 | 维护混淆 | 归档/标注 | Phase 1 |
| R-034 | Kernel debug dashboard/DAG 未鉴权 | S2 | H | MITIGATED（Phase 1C） | `/v1/dag*` 需 admin key；`/dashboard` 默认关闭，开启后仅 loopback | 信息泄露/调试接口滥用 | 保持默认关闭；生产不启用 | Phase 1C |
| R-035 | Docker sandbox 无镜像/无测试/默认 root | S2 | M | OPEN | `gate_sandbox.py:151-243` | 隔离不达标 | 构建镜像、非 root、seccomp、网络策略、测试 | Phase 6 |
| R-036 | Merge `_finish_intent` 允许 token IS NULL | S2 | L/M | OPEN | `merge_coordinator.py:510-514` | 无 fencing 完成 intent | 禁止 null token；reconcile 走专用受控路径 | Phase 1 |
| R-037 | DB-first reconcile 把 task 置 LEASED 但不恢复 token/runner | S3 | M | OPEN | `merge_coordinator.py:628-640` | 任务卡到 reaper | 恢复 token/runner 或 FAILED；测试 | Phase 1 |
| R-038 | startup reconcile 对已 MERGED intent 非幂等 | S3 | M | OPEN | `merge_coordinator.py:576-596` | 重复 artifact/event | 幂等检查；只处理未完成状态 | Phase 1 |
| R-039 | AgentGraph 单 FastAPI 进程承载所有后台循环 | S3 | M | OPEN | `main.py:100-220` | 控制面扩展性/故障隔离差 | 拆 worker/事件循环；水平扩容 | Phase 7/8 |
| R-040 | Kernel ORM 只列 7 表，其他表靠原生 SQL | S4 | H | OPEN | `models.py` vs `sql/` | 类型/迁移漂移 | 统一 schema 管理/生成 | Phase 1 |

---

## 2. 最高优先级（Phase 1 必须处理）

1. **R-001 merge lease 复核**：正确性阻断。
2. **R-002 合成 gate 证据**：交付可信度阻断。
3. **R-003/R-004 租户/沙箱/admin**：安全阻断。
4. **R-005 取消**：产品闭环阻断。
5. **R-006/R-007 单一状态机**：后续所有迁移的基础。
6. **R-016 事件契约**：投影/恢复的基础。
7. **R-018 code_engine 纳入版本控制 + 测试**：主链路可回归。
8. **R-021 100-runner P99**：容量基线必须可重复。

---

## 3. 风险处理原则

- 先修正确性/安全，再谈容量/性能；
- 先建契约/身份，再拆服务；
- 每个修复必须有测试/负向测试；
- 不允许用全局锁/无限重试/静默异常掩盖问题；
- 不允许删除已验证能力而没有等价替代；
- 每个阶段有回滚方案；
- 不把文档描述当实现事实。

---

## 4. 已缓解/已接受

| ID | 处理 |
|---|---|
| R-041 旧 Kernel 进程（10:51 启动，无 v2） | 已按用户指令停止旧进程、备份旧目录、复制最新源码、重启 API + worker；v2 路由/表已生效。仍保留旧目录备份 `E:\Codes\backup_veynod_kernel_20261031-2230`。 |
| R-042 旧 Kernel 目录替换导致历史 `.run` 丢失 | 旧目录整体备份，历史压测证据仍可访问；新目录自带 `.run`。 |
| R-043 测试环境缺 pgvector | 已记录；AgentGraph 集成测试未跑，不声称通过。 |

---

## 5. 风险登记维护

- 每次 Phase 结束更新状态；
- 新增风险必须有证据、影响、缓解、阶段；
- 已修复风险标记 MITIGATED 并附测试；
- 不能因为“文档写了”就关闭风险；
- 不能把风险接受（ACCEPTED）当作已修复。
