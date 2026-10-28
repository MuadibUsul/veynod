# 联邦 F-00：缺口分析（GAP_ANALYSIS）

> 基线：HEAD `97ea9eb`，应用 3.13.1，schema 3.48.0。现状细节和证据见 [CURRENT_BASELINE.md](CURRENT_BASELINE.md)。
> 标记沿用 CURRENT_BASELINE：**【已有】/【部分】/【缺失】/【假设】**。“落地任务”指联邦 24 任务里的编号。

## 1. 本次审查新发现的问题（N 系列）

这些问题不在愿景 v0.3 的 G 列表里，但会影响联邦能否安全落地。

| ID | 等级 | 问题 | 位置 | 建议处理 | 落地任务 |
|---|---|---|---|---|---|
| N-01 | P0 | 应用以 PostgreSQL **superuser** 连接，RLS 只在 `SET LOCAL ROLE` 之后生效；仓库里没有 `GRANT agentgraph_app TO <login>`，换成非超级用户连接会全部 503 | `docker-compose.prod.yml:50`、`app/tenant.py`、`sql/migrations/v3_2_production.sql` §12 | 联邦上线前改成非超级用户登录角色加显式 membership，并增加启动自检和非超级用户集成测试（ADR-010） | F-01 前置 / F-06 |
| N-02 | P0 | 306 处平台会话不设租户上下文，以 superuser 运行；跨根隔离在这些路径上只靠应用层 | 47 个文件，例如 `app/services/sharing.py` `shared_document()` | 联邦跨根读写只能走一个专门的、有审计的通道（`federation` 服务），**禁止**再新开平台会话去 `JOIN` 对方数据 | F-01 / F-04 / F-07 |
| N-03 | P1 | 工作区 key 和 root key 的 `agent_hint` 可以把任意字符串设成 `agent_signature` | `app/auth.py` `_resolve_principal()` | 联邦审批记录绑定 `user_id` 或 `token_cid`，不绑定签名字符串 | F-03 |
| N-04 | P1 | MCP `send_message` 不在通用 `idempotency_key` 工具列表里；消息表也没有业务唯一键，重试会产生重复消息 | `app/mcp_server/server.py` 约第 98 行的列表、`send_message()`；`app/services/messaging.py` | 联邦消息必须带 `message_id` 唯一约束；顺手把本地 `send_message` 也加入幂等列表（是否本次做，见 ADR-012） | F-04 |
| N-05 | P1 | 租户 Ed25519 私钥和假名密钥以 base64 明文存在 `tenant_signing_keys`，只靠 REVOKE 阻止应用角色读取，而 superuser 平台会话能读到 | `app/services/attestation.py` `_new_key()`、`_current()`；`v3_28_phase3_r32.sql` | 联邦签名密钥改用信封加密（KMS 或环境主密钥），或者交给外部密钥托管（ADR-013） | F-02 |
| N-06 | P2 | 公开的 `/revoked` 不需认证，每次请求都对全租户 token 逐行算 HMAC，开销是 O(n)；`/jwks.json` 和 `/revoked` 都能用来探测租户 alias 是否存在（404 与 200 的差别）；租户第一次被访问时，`_current()` 会在这个 **不需认证的 GET** 里生成并写入签名密钥 | `app/api/wellknown_routes.py` `tenant_jwks()` / `tenant_revoked()`；`attestation.revoked()`、`_current()` | 预先计算并索引假名，或改成按 `kid` + 签名过的撤销列表；密钥在租户创建或 `rotate()` 时生成，不在 GET 里生成；对这两个端点单独限流 | F-02 |
| N-07 | P1 | CI 只能手动触发（`workflow_dispatch`），PR 不会自动跑测试；联邦 24 个任务的“测试通过”缺少客观门禁 | `.github/workflows/test.yml` | 增加 `pull_request` 触发，至少跑 backend 和 protocol 两个 job（需要 owner 同意，因为会消耗 Actions 额度） | F-01 前 |
| N-08 | P2 | `outbound_allow_private_networks` 的代码默认值是 True，只靠部署配置关掉 | `app/config.py` | 改成默认 False，开发环境通过 `.env` 打开（会改变开发默认行为，需要确认） | F-06 |
| N-09 | P2 | README 标题仍写 v3.6.7，实际版本 3.13.1；愿景 v0.3 引用的基线也已经过时（schema 3.40 → 3.48） | `README.md:1` | 联邦文档一律以 `app_version` 和 `CURRENT_SCHEMA_VERSION` 为准 | — |
| N-11 | P1 | 每次启动都重跑 `sql/init.sql`（含 `DROP/CREATE TRIGGER … ON collab_tasks`，需要 AccessExclusiveLock）；advisory lock 只挡 DDL 之间的并发。本地测试时第二个实例启动，与存活实例的写入形成 **死锁**（PG 日志 00:21:37 UTC），存活实例的写事务被回滚 | `app/database.py` `apply_schema()`；`sql/init.sql:384-385` | schema 已是最新版本时跳过 `init.sql` 和 `sql/functions/*.sql`，或者只在迁移中执行触发器 DDL；增加“滚动重启时写入不失败”的测试 | F-01 |
| N-12 | P1 | v3.40 的 `blobs`、`document_blobs`、`blob_uploads`、`cleanup_grants`、`share_links` 没有 `tenant_id`、**没有启用 RLS**，应用角色有完整 CRUD；main 上现有回归测试 `test_tenants::test_every_owned_row_carries_a_derived_tenant` 因此失败 | `sql/migrations/v3_40_blobs_share_links.sql`；`app/services/blobs.py`、`share_links.py` | 补一份迁移：加 `tenant_id`、租户派生触发器、强制 RLS 和 `tenant_isolation` 策略，与 `v3_48` 一致（公开分享链接按 `token_hash` 查询的路径要走平台会话或专用函数） | F-01 前置 |
| N-13 | P1 | main 上全量测试 17 个失败，其中 13 个是因为 3.12.0 有意变更后测试没有更新（DONE 只能由 OWNER 批准、COMMANDER 可铸造、MCP 工具清单），另外根页面的 `ROOT_PRIMITIVES` 漏列了新工具 | 见 CURRENT_BASELINE §7 | 先修复测试和 `ROOT_PRIMITIVES`，让测试套件全绿，再开 F-01；与 N-07 一起处理 | F-01 前置 |
| N-10 | P2 | SSE 撤销最多晚 30 秒才断流 | `app/api/stream_routes.py` `_stream_events()` | 联邦流在 grant 撤销时主动推送关闭（通过事件），不等下一次轮询 | F-03 / F-04 |

## 2. 愿景 G01–G18 与源码对照

| ID | 缺口 | 现有基础（路径 + 符号） | 状态 | 还缺什么 | 落地任务 |
|---|---|---|---|---|---|
| G01 | 主权身份、账号丢失、所有权转移 | `agent_tokens.agent_id` + `generation`（v3.39）；租户树、双方确认删除（`tenants.py`）；`tenant_signing_keys` | 【部分】 | 全局不可复用的 `federation_subject_id` / `workspace_id` / `agent_id`；墓碑；所有权转移和恢复流程（政策 TBD） | F-01 / F-02 |
| G02 | 互信与“可以做什么”未分离 | `CommanderGrant`（`grants.py`：受托者、动作、范围、到期、撤销、每次请求校验）；token 子集规则（`token_tree.py`） | 【部分】 | 方向性 `federation_grants` + `grant_version` + 撤销栅栏；跨工作区的受托者 | F-03 |
| G03 | 聊天和任务本身就是数据出口 | `content_origin`、污点读侧 `handling`（`taint.py`）；消息长度上限 8192 | 【部分】 | 数据分级字段、出站门禁、发送前确认 | F-04 / F-06 |
| G04 | 撤销与执行中副作用的边界 | `cancel_task()`、租约 fencing（`tasks.py`）、外部动作必须人批（`external_actions.py`） | 【部分】 | `cancel_requested_at` / `cancel_ack_at` / `work_may_continue`；跨域 `STOP_NOT_CONFIRMED` 状态 | F-08 |
| G05 | 分布式消息与任务语义 | outbox + 游标重放（`events.py`）；webhook 重试和 DEAD（`webhooks.py`）；24 小时幂等（`idempotency.py`）；消息回执（`messaging.py`） | 【部分】 | 跨工作区的 outbox/inbox、业务唯一键、死信、对账；消息幂等（N-04） | F-04 / F-07 |
| G06 | 额度借用不是现成的模型配额 | `token_budget.py`、`usage.py`、`budget.py` 的强制上限 | 【部分】 | `federation_cost_receipts`（`metering_source`、`confidence`、`proof_ref`、`price_snapshot_id`、`disputed`） | F-09 |
| G07 | 远端消息污染记忆和执行 | 文档、slot、任务、resource 的污点和 15 分钟读窗口（`taint.py`） | 【部分】 | 消息、委派输入、结果、摘要都要纳入污点拓扑；**会话消息目前完全没有污点** | F-04 / F-06 |
| G08 | 平台内互联不等于独立实例联邦 | 无 | 【缺失】 | 传输接口抽象；部署无关的身份 | F-02 设计 / F-11 |
| G09 | Skill 签名不等于安全 | `blobs.py` 存制品，`document_versions.content_hash` | 【缺失】 | 制品不可变性、权限清单、依赖锁定、撤回 | P3-1 至 P3-4 |
| G10 | 声誉刷单与串谋 | `situation.py` 的异常规则（重试风暴、空转循环等） | 【缺失】 | 履约证明、去权重、反女巫 | P4-1 / P4-3 |
| G11 | “确认作恶”缺正当程序 | `incidents.py`（平台事故）、`alerts.py` | 【缺失】 | 举报案件状态机、证据保全、申诉 | F-10 / P4-4 |
| G12 | 冷启动的供需匹配 | `demo_page.py`、`templates/` | 【缺失】 | 精选供给和演示闭环（产品工作） | P4-2 |
| G13 | 冒充、抢注、劫持 | 目录别名表 `agent_aliases`、`name_history()`；alias 唯一（`test_a06_address_uniqueness`） | 【部分】 | 全局 ID 与别名分离、签名挑战、墓碑 | F-01 / F-02 |
| G14 | 事故传播半径 | 写入熔断和退避（`budget.check_write`）、ping-pong 熔断、SSE 连接上限 | 【部分】 | 按 peer、工作区、全局的联邦熔断和紧急停发开关 | F-06 |
| G15 | 第三方模型和工具的使用权 | 无 | 【缺失】 | 交易前置检查（法务，不是代码） | P5-4 |
| G16 | 监控、删除与审计相互冲突 | 权利请求的定位、导出、擦除（`rights.py`，AG-TC-07）；`redaction_log`；运营隐私（`operator_privacy.py`） | 【部分】 | 联邦数据的保留期分层；证据摘要和正文分开存 | F-01 / F-10 |
| G17 | 供给“可用”没有客观定义 | presence 四态在分支 `claude/cool-meitner-6sqiw5` 上，**未合并**（任务 `blueprint-L2-presence-wake` 处于 REVIEWING） | 【缺失（main）】 | 能力自检、健康探针、最近一次验证时间 | P4-1 |
| G18 | 身份、任务、结算边界耦合 | 无 | 【缺失】 | 委派、计量、结算三层表结构分开（ADR-004、ADR-005） | F-01 设计 |

## 3. 验收用例 F-001–F-029 与现有测试对照

“可复用测试”指现有测试里可以照着改写的场景；**没有任何一条联邦用例已经存在**。

| 编号 | 级别 | 场景 | 可复用测试 / 机制 | 缺口 | 落地任务 |
|---|---|---|---|---|---|
| F-001 | P1 | 两个根租户里有同名 agent | `test_tenants.py::test_cross_tenant_fuzz_leaks_nothing`；`agent_id` 只在租户内唯一 | 全局 ID 和寻址 | F-01 / F-02 |
| F-002 | P1 | 有连接但没有委派权限 | `test_commander_grants.py::test_commander_writes_require_a_live_in_scope_grant` | 方向性 grant | F-03 |
| F-003 | P1 | agent 伪造人类批准 | `test_security_hardening.py::test_human_states_require_a_human`、`::test_agent_cannot_mark_done_or_forge_human_feedback` | 审批绑定 `user_id`（N-03） | F-03 |
| F-004 | P1 | 授权不传递（A→B→C） | 无 | 授权模型本身 | F-03 |
| F-005 | P1 | 过期、撤销、冻结、拉黑 | `test_security_hardening.py::test_stream_closes_after_token_revocation`；`test_token_tree.py::test_revocation_cascades_releases_and_taints` | `revocation_epoch`；N-10 | F-03 |
| F-006 | P1 | 把 token 填进远端字段 | 无 | 密钥识别和清洗 | F-04 / F-06 |
| F-007 | P1 | 重复的 `message_id` / 幂等键 | `test_protocol.py::test_same_key_same_body_replays_without_second_write` | 消息唯一键（N-04） | F-04 |
| F-008 | P1 | 新版本先到、老版本后到 | `test_leases.py::test_stale_fencing_token_is_refused`（单调 epoch） | 联邦状态的单调版本 | F-04 / F-07 |
| F-009 | P1 | 外部文本要求“忽略规则” | `test_provenance.py::test_taint_sources_and_read_side`（`handling` 标记） | 消息污点 | F-04 / F-06 |
| F-010 | P1 | 外部消息被摘要成记忆后再读 | `taint.py` 的读窗口机制 | 摘要和记忆路径上的污点继承 | F-06 |
| F-011 | P1 | 1 万条恶意消息突发 | `messaging._enforce_rate_limit()`；`budget.check_write` | 按 peer 限流和熔断 | F-04 / F-06 |
| F-012 | P2 | 重复投递委派 | 无 | `federation_job_id` 唯一 | F-07 |
| F-013 | P2 | 接单后断网再恢复 | `test_hardening.py::test_sse_replay_after_disconnect` | 对账 | F-07 / F-08 |
| F-014 | P2 | 验收被拒 | `test_fsm_fencing.py::test_reject_returns_ready_keeps_assignee_and_counts_a_retry` | 跨域 `REVISION_REQUESTED` | F-07 |
| F-015 | P2 | 提交后修改验收标准 | `v3_41_task_invariants.sql`（验收锁定） | 冻结 `acceptance_hash` 跨域保存 | F-07 |
| F-016 | P2 | 运行中取消 | `cancel_task()` | `STOP_NOT_CONFIRMED` | F-08 |
| F-017 | P2 | 在途执行超预算 | `test_budget_enforce.py` | 委派级 `budget_cap` | F-09 |
| F-018 | P2 | 回复里带内部对象指针 | `share()` 拒绝跨根 | 跨根 `doc://` 解引用防护 | F-07 |
| F-019 | P2 | 举报不自动处罚 | 无 | 案件状态机 | F-10 |
| F-020 | P2 | 伪造平台裁决通知 | `test_phase3_r32.py::test_lineage_attestation`（验签） | 平台权威签名 | F-10 |
| F-021 | P2.5 | 两个独立数据库 | 无 | 传输层 | F-11 |
| F-022 | P2.5 | 企业禁用公网 | `OUTBOUND_ALLOWED_HOSTS`；`feature_flags()` | `FEDERATION_ENABLED=false` 时零公网请求的测试 | F-06 / F-11 |
| F-023 | P3 | Skill 撤回 | 无 | — | P3-4 |
| F-024 | P3 | 补丁版本静默提权 | 无 | — | P3-3 |
| F-025 | P4 | 同一控制人互刷 | 无 | — | P4-3 |
| F-026 | P4 | 私聊和履约证明 | `operator_privacy.py` | — | P4-4 |
| F-027 | P5 | 支付事件重复 10 次 | 幂等机制 | — | P5-2 |
| F-028 | P5 | 退款和入账竞态 | 无 | — | P5-3 |
| F-029 | P1 | 加密策略不一致或被降级 | 无 | 取决于 ADR-009 | F-02 / F-04 |

## 4. 落地时需要改的最小接口

只列 P1（F-01 到 F-06）必须改的地方；全部改动放在新模块里，由开关控制，默认关闭。

| 层 | 改动 | 不改的 |
|---|---|---|
| 数据库 | 新增 `federation_*` 表（ADR-011 决定哪些是全局表、哪些按租户）；每张租户表沿用 `v3_48` 的 trigger、policy 和 grant 模板 | `collab_tasks` 状态枚举、`agent_tokens`、`tenant_shares` |
| 服务 | 新增 `app/services/federation/`（身份、grant、投递、桥接）；只通过 `tasks.py`、`messaging.py`、`taint.py` 的公开函数改本地对象 | `sharing.share()` 的跨根拒绝逻辑 |
| 路由 | `/v1/federation/*`（人类和本地 agent 用）加单独的 peer 入口（只验签，ADR-006） | `/v1/tasks/*` 不对外暴露 |
| 开关 | 在 `app/capabilities.py` 的 `feature_flags()` 中增加 `FEDERATION_ENABLED`、`FEDERATION_PUBLIC_DISCOVERY_ENABLED`、`FEDERATION_REMOTE_DELEGATION_ENABLED`，默认全关 | 现有开关 |
| 协议 | 在 `protocol/` 中加 `FederationEnvelopeSchema` | 现有导出 |
| 控制台 | 一级入口“联邦”，内容为连接审批和会话 | 现有页面 |

## 5. P1 开工前必须由人决定的事

以下任何一项没有定，F-01 的表结构都可能返工。详情见 [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md)。

另外有三件工程上的前置工作，不需要产品决策，但需要 owner 同意开工：N-12（给 5 张表补 RLS）、N-13（测试套件恢复全绿）、
N-11（启动时不再重跑 `init.sql`）。

1. **ADR-009**：服务器能不能读联邦正文（明文 / 端到端加密 / 混合）。这决定 `federation_messages.payload` 存明文还是密文。
2. **ADR-010**：是否先把数据库连接角色改成非超级用户（N-01）。
3. **ADR-011**：联邦表哪些放全局、哪些按租户。
4. **ADR-012**：联邦消息复用 `conversations`/`messages`，还是单独建 `federation_messages`。
5. **ADR-001 至 ADR-008**：愿景 v0.3 提出的建议，目前都是“建议，未批准”。

## 6. 本审查没有覆盖的范围

- 没有连接生产库，生产里的实际角色属性以安全审计报告为准。
- 没有做性能压测（愿景 §16.1 的 SLO 目前没有依据）。
- 没有审查 `dashboard/` 的前端安全，比如 CSP 与 localStorage。安全审计 M1 已标注 human session 仍存在 localStorage。
- 分支 `claude/cool-meitner-6sqiw5` 上的 presence 和实时黑板没有合并进 main，这里没有把它们算作基线。
