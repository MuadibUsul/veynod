# 联邦 F-00：仓库真实基线（CURRENT_BASELINE）

> 任务：`fedv2-f00-baseline`（联邦 01/24）。只做审查，没有改动任何业务代码。
> 上游：`doc://agentgraph/federation-vision` v3（下称“愿景 v0.3”）§23.1。
> 配套：[GAP_ANALYSIS.md](GAP_ANALYSIS.md)、[ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md)。

## 0. 审查方式与标记

- 审查对象：`MuadibUsul/veynod` 的 `main`，HEAD `97ea9eb20b68e34cf6011ae1191b842a8caa75e6`
  （2026-10-27 18:29 -0400，“Record deployed 3.13.1 image”）。
- 方法：静态读代码和文档，再在本地 PostgreSQL 16 + pgvector 上运行 `ruff` 和完整 `pytest`（结果见 §7）。
  没有连接生产环境，也没有对生产做任何操作。
- 每一条都写明 **文件路径 + 符号 + 测试证据**。三种标记：
  - **【已有】**：代码里有实现，并且有测试覆盖，或者能在代码里直接看到；
  - **【缺失】**：代码里找不到；
  - **【假设】**：根据代码推断出来的，没有运行验证，需要后续任务确认。

## 1. 版本与愿景 v0.3 审查基线的差异

| 项 | 愿景 v0.3 记录 | 本次实测 | 证据 |
|---|---|---|---|
| 仓库 | `MuadibUsul/AgentGraph` | `MuadibUsul/veynod`（产品已改名；Python 包、数据库、容器名仍沿用 `agentgraph`） | `README.md` 第 7 行 |
| Git | 树 `c1c8df8…` | HEAD `97ea9eb` | `git rev-parse HEAD` |
| 应用版本 | v3.6.7 | **3.13.1** | `app/config.py:23` `Settings.app_version`；`app/services/releases.py` `RELEASE_NOTES[0]` |
| Schema | 3.40.0 | **3.48.0** | `app/database.py:239` `CURRENT_SCHEMA_VERSION`；`scripts/deploy_tlines.sh` `EXPECTED_SCHEMA=3.48.0` |
| 生产镜像 | — | `agentgraph:3.13.1-veynod-f7a8e84` | `docker-compose.prod.yml:44` |
| README 标题 | v3.6.7 | 仍写 **v3.6.7**，与实际版本不一致 | `README.md:1` |

愿景 v0.3 之后新增、且与联邦相关的迁移（3.41 → 3.48）：

| 版本 | 迁移 | 对联邦的意义 |
|---|---|---|
| 3.41.0 | `v3_41_task_invariants.sql` | 任务验收锁定、`lease_epoch` 作为 fencing token；可以作为“冻结验收快照”（愿景 §7.5）的落点 |
| 3.42.0 | `v3_42_task_project_repo.sql` | 任务带 project/repo 归属 |
| 3.43.0 | `v3_43_lease_renew.sql` | 显式续租窗口与次数上限 |
| 3.44.0 | `v3_44_super_admin.sql` | SUPER_ADMIN 角色 |
| 3.45.0 / 3.46.0 | `v3_45_conversations.sql` / `v3_46_group_conversations.sql` | **工作区内**的 1:1 和群组会话、永久消息、已发送/已送达/已读回执，是 F-04 和 F-05 最主要的可复用基础 |
| 3.47.0 | `v3_47_message_escalations.sql` | 消息升级给人 |
| 3.48.0 | `v3_48_commander_grants.sql` | 有范围、有期限、可撤销、每次请求都会校验的授权对象（`CommanderGrant`），可以作为联邦 capability grant（ADR-002）的模板 |

## 2. Schema 迁移链与初始化

- **【已有】** 迁移链：`app/database.py` 中的 `_INCREMENTAL_MIGRATIONS`，从 3.5.0 到 3.48.0 共 44 条，按顺序执行。
  在此之前固定执行 `sql/init.sql`、`v3_hardening.sql`(3.1)、`v3_2_production.sql`(3.2)、
  `v3_2_1_mcp_improvements.sql`、`v3_3_agent_feedback.sql`、`v3_4_security_hardening.sql`；
  最后重新加载 `sql/functions/*.sql`。`sql/migrations/` 下共有 50 个文件。
- **【已有】** 并发保护：`apply_schema()` 先取会话级 `pg_advisory_lock(hashtext('agentgraph_schema_v3'))`，
  `conn.close()` 时释放。证据：`tests/test_failure_modes.py::test_schema_application_is_idempotent`。
- **【已有】** 启动顺序：`app/main.py` 的 `lifespan()` 中，当 `auto_init_db` 为真时，先 `apply_schema()`，
  再 `backfill_all_workspaces()`，然后才开始接请求。
- **【假设】** 迁移文件与 `schema_migrations` 的写入标记是两条独立语句，不在同一事务里。
  进程如果在两者之间崩溃，下次启动会重跑整份迁移，所以每份迁移必须幂等。现有迁移大量使用
  `IF NOT EXISTS`，但没有逐份验证过。
- **【假设】** `backfill_all_workspaces()` 和 `record_release()` 在 advisory lock 之外执行。多副本同时启动时，
  它们依赖自身的幂等性，没有锁保护。
- **【缺失】** 迁移回滚脚本。生产采用“备份 + 回滚镜像 + 显式恢复数据库”（见 §3），符合愿景 §23.2
  “前向修复优先”的原则。

## 3. 发布流程

- **【已有】** 部署：`scripts/deploy_tlines.sh`，子命令 `deploy` / `deploy-loaded` / `verify` / `rollback <stamp>` / `rollback-image <stamp>`。
  部署前备份数据库，镜像打 `agentgraph:rollback-<stamp>` 标签；校验失败只回滚镜像，数据库要显式执行 `rollback` 才恢复。
  脚本会校验 `EXPECTED_SCHEMA`。
- **【已有】** 发布记录：`app/services/releases.py` 中的 `record_release()`。新版本启动时记录一次并广播
  `platform.released`；每次部署后提交 “Record deployed X image”，同时更新 `docker-compose.prod.yml` 和 `docs/PROJECT_ARCHITECTURE.md`（例如 HEAD 这次提交）。
- **【已有】** CLI 发布：`.github/workflows/publish-cli.yml`，推送 `cli-v*` 标签时用 npm Trusted Publishing 发布；
  `cli/package.json` 版本为 0.2.2，`license: UNLICENSED`。
- **【缺失】** 自动 CI。`.github/workflows/test.yml` 只能 **`workflow_dispatch` 手动触发**，push 和 PR 都不会自动跑测试。
  联邦 PR 的“测试通过”证据必须由执行方自己跑出来，或者先把 CI 改成自动触发（见 GAP N-07）。

## 4. 逐模块现状

“联邦可复用度”的含义：**直接复用** = 只需加联邦适配层；**改造复用** = 要扩展语义；**不可复用** = 语义不同，只能参考。

| 模块 | 现状（路径 + 符号） | 测试证据 | 联邦可复用度与限制 |
|---|---|---|---|
| 认证 `app/auth.py` | 【已有】`_resolve_principal()` 依次匹配工作区 key、root key、agent token（含轮换宽限期内的 `previous_key_hash`）、人类会话；`_agent_principal()` 沿 `lineage` 求 scopes 和命名空间的交集，祖先被撤销或过期就拒绝；`hash_api_key()` 用 SHA-256 存哈希，不存明文 | `tests/test_token_tree.py`、`tests/test_token_lifecycle.py::test_expired_tokens_renew_in_place_and_secrets_hot_swap` | 改造复用。本地主体 **不能** 直接映射成 peer。工作区 key 的 `agent_hint` 允许调用方自报 `agent_signature`（见 GAP N-03） |
| 租户与 RLS `app/tenant.py` | 【已有】`activate_tenant()` / `_apply_tenant_context()` 先 `SET LOCAL ROLE agentgraph_app`，再设 `app.workspace_id`、`app.tenant_scope`、`app.lineage_stamp`；失败一律返回 503 `tenant_context_unavailable`（fail-closed）。`Principal.tenant_scope` 只包含单个租户（`app/auth.py`） | `tests/test_production.py::test_database_rls_isolates_workspaces`、`tests/test_tenants.py::test_cross_tenant_fuzz_leaks_nothing`、`::test_client_supplied_tenant_id_is_rejected` | 联邦新表必须沿用同一套 trigger、policy 和 grant 模式（参考 `v3_48_commander_grants.sql`）。连接角色问题见 §6.1 |
| 租户树 `app/services/tenants.py`、`v3_26_tenant_tree.sql` | 【已有】根租户树、节点生命周期、双方确认删除 | `tests/test_tenant_tree.py`、`tests/test_tenants.py::test_two_party_deletion_grace_restore_and_purge` | “不同根租户”就是愿景 §21 中 E2E 的隔离单位 |
| 目录 `app/services/directory.py` | 【已有】`agent_directory` 以 `(tenant_id, agent_id)` 唯一（`_taken()`、`_resolve()`），有别名表 `agent_aliases` 和 presence；`list_agents()` 按 `workspace_id = :ws` 过滤 | `tests/test_token_naming.py::test_generation_keeps_agent_id_and_old_assignments`、`tests/test_identity_continuity.py`、`tests/test_messaging_acceptance.py::test_a06_address_uniqueness` | 改造复用。`agent_id` 只在租户内唯一，**不是**全局主键（愿景 §3.1） |
| Token 树 `app/services/token_tree.py` | 【已有】`issue_child_token()` 做子集校验（`_check_subset`、`_namespaces_extra`），角色只能保持或收窄（`PERMISSIONS[...] <= ...`），SUPER_ADMIN 只能由 owner 签发，TTL 不超过父级，有配额和预算上限；`renew`/`rotate`；默认 `CHILD_TOKEN_ISSUANCE` 关闭 | `tests/test_token_tree.py`、`tests/test_token_delegation.py`、`tests/test_super_admin.py` | 不可复用为联邦凭证（ADR-005），但子集规则可以照搬到 grant |
| 授权对象 `app/services/grants.py` | 【已有】`CommanderGrant`：受托者、动作、命名空间、任务 key、类型、到期、撤销，每次写操作都按 `X-Commander-Grant-Id` 校验 | `tests/test_commander_grants.py::test_commander_writes_require_a_live_in_scope_grant`、`::test_grants_are_issued_by_the_owner_only` | **最接近** 愿景 §3.3 capability grant 的现有实现，可作为 `federation_grants` 的模板 |
| 任务 `app/services/tasks.py` | 【已有】`claim_task()`、`renew_task()`、`handoff_task()`、`review_task()`、`cancel_task()`、`write_continuation()`；带 `lease_epoch`/fencing、验收锁定、ping-pong 熔断、人类门禁 | `tests/test_leases.py::test_stale_fencing_token_is_refused`、`tests/test_fsm_fencing.py`、`tests/test_task_invariants.py`、`tests/test_security_hardening.py::test_agent_cannot_mark_done_or_forge_human_feedback` | 直接复用。联邦桥只能经这些服务函数改本地任务（愿景 §7.2），不能改状态枚举 |
| 事件 `app/services/events.py`、`app/events.py` | 【已有】`workspace_events` 事务性 outbox（按 shard_key 哈希分 16 个分区，v3.29），`poll_events()` 用游标续读，`subscribe()` 支持前缀订阅 | `tests/test_events.py`、`tests/test_phase3_r32.py::test_event_log_is_partitioned`、`tests/test_hardening.py::test_sse_replay_after_disconnect` | 改造复用。投递单位是本地 workspace |
| 收件箱 `app/services/inbox.py`、`app/api/inbox_stream.py` | 【已有】`summary()`、`ack()`；推送流 `_poke_stream()` 带 `Last-Event-ID` 回放 | `tests/test_poke_realtime.py::test_push_presence_and_receipts` | 直接复用为联邦消息的本地落地通道 |
| 戳一戳 / 唤醒 `app/services/pokes.py`、`app/services/webhooks.py` | 【已有】presence、`record_delivery()` / `record_ack()` 回执、`set_wake_url()`；webhook 重试 30/120/600 秒后标记 DEAD；HMAC 签名用 `outbound.signature()` | `tests/test_poke_delivery.py::test_failing_webhooks_retry_three_times_then_die`、`tests/test_phase3.py::test_webhooks_need_scope_and_carry_the_event` | 改造复用。是 F-04 重试和死信的现成模式 |
| 会话消息 `app/services/messaging.py` | 【已有】`send_message()`（单条不超过 8192 字符，每个发送者每分钟有限流）；已发送/已送达/已读回执；读水位；群组；升级给人 | `tests/test_messaging.py::test_direct_message_receipts`、`tests/test_messaging_acceptance.py` | 改造复用。**限制**：只在工作区内；没有污点标记；MCP `send_message` 不接受 `idempotency_key`（见 GAP N-04） |
| 签名证明 `app/services/attestation.py`、`app/api/wellknown_routes.py` | 【已有】每租户一对 Ed25519 密钥（`tenant_signing_keys`，应用角色无权读）；`attest()` 签发按受众定向、最长 900 秒的 JWS，链上每一环用成对假名表示；`/.well-known/agentgraph/tenants/{alias}/jwks.json` 公布公钥，轮换后旧公钥保留 7 天；`/revoked` 公开实时查询吊销状态 | `tests/test_phase3_r32.py::test_lineage_attestation`；`scripts/verify_lineage.py` | 改造复用。可作为联邦签名的密钥和 JWKS 基础；**不是**双向握手。私钥以原始字节 base64 明文存在库里（`_new_key()`），见 GAP N-05 |
| 共享 `app/services/sharing.py` | 【已有】`share()` 跨根租户直接拒绝（422 `share_cross_root_not_allowed`，提示改用 federation）；`shared_document()` 用无租户上下文的平台会话读取，靠应用层 `_grants()` / `_covers()` 过滤 | `tests/test_tenant_tree.py::test_visibility_sharing_and_admin_access`、`tests/test_share_links.py` | 联邦 **不得** 绕过这个检查（愿景 §10.1） |
| 污点 `app/services/taint.py` | 【已有】覆盖文档（`compute_document_taint`、`propagate_from_document`）、slot、任务、resource，以及 15 分钟读窗口和 `content_origin` 声明；`clear_document_taint()` 只允许人操作 | `tests/test_provenance.py::test_taint_sources_and_read_side`、`tests/test_production.py::test_taint_propagation_and_human_verification`、`tests/test_security_hardening.py::test_viewer_cannot_change_taint` | 改造复用。**消息和会话没有纳入污点拓扑**（愿景 G07、F-009/F-010） |
| 预算与计量 `app/services/budget.py`、`token_budget.py`、`usage.py` | 【已有】写入速率、熔断和退避（`check_write`）；embedding 日配额；按 token 链设预算（`token_budget.check`/`meter`）；工作区月度 token 和成本预算（`usage.assert_budget`） | `tests/test_budget.py`、`tests/test_budget_enforce.py`、`tests/test_call_cost.py` | 只能复用“强制上限”的部分。**没有** `metering_source`、`confidence`、`proof_ref`、`price_snapshot` 这类收据结构（愿景 §8.3） |
| 幂等 `app/services/idempotency.py`、`app/protocol_middleware.py` | 【已有】REST 写请求的 `Idempotency-Key` 头和 MCP 写工具的 `idempotency_key` 都在 24 小时窗口内回放第一次结果 | `tests/test_protocol.py::test_same_key_same_body_replays_without_second_write`、`::test_mcp_write_tools_replay_by_idempotency_key` | 直接复用为传输层幂等；业务唯一键（`federation_job_id`）要另建 |
| 出站 `app/services/outbound.py` | 【已有】见 §6.4 | `tests/test_failure_modes.py::test_private_outbound_target_is_blocked` | 直接复用为联邦出站 |
| 功能开关 `app/capabilities.py` | 【已有】`feature_flags()` 列出各开关及其状态；关闭的功能返回 403 `feature_disabled` | `tests/test_releases.py::test_switched_off_feature_says_so` | `FEDERATION_ENABLED` 等开关应接到这里 |
| 链接 `app/services/links.py` | 【已有】`WorkspaceLink` 是本地对象之间的 typed link | — | **不可复用**为 peer 连接（愿景 §10.3） |
| 跨所有者房间 | 【已有，仅预留】文档、任务、事件上有可空的 `room_id`（AG-CO-09，“Reserved”） | `docs/PRD_PROGRESS.md` Phase 3 R3.1 | 等产品决定 |
| 控制台 `dashboard/app/` | 【已有】单页 `page.tsx`，组件有 `agents`、`audit`、`blackboard`、`documents`、`governance`、`situation`、`support-access`、`task-board` 等 | CI `dashboard` job（`npm run build`） | 【缺失】联邦入口 |
| MCP `app/mcp_server/server.py` | 【已有】约 3690 行，工具清单；写工具的通用 `idempotency_key` 列表在第 98 行附近 | `tests/test_protocol.py` | 与 peer 连接是不同的生命周期（§5） |
| 协议包 `protocol/` | 【已有】`@veynod/protocol` 0.1.0，用 zod 校验任务、交接、审核的入站载荷 | CI `protocol` job | 可以放联邦信封的 schema（ADR-003） |

【缺失】整个仓库没有任何 `federation_*` 表、路由、服务或开关。用 `grep -ri federation` 搜索，代码里只在
`app/services/sharing.py` 的错误提示中出现过。

## 5. `docs/MCP_CONNECTION_LIFECYCLE.md`：Proposed 与 Implemented 的差异

该文档状态为 **Proposed（2026-10-25）**。逐项对照代码：

| 提案内容 | 状态 | 证据 |
|---|---|---|
| `mcp_connections` 表、`agent_tokens.connection_id`、`cli_sessions.client_instance_id` | 【缺失】在 `app/`、`sql/`、`cli/` 中都搜不到 | `grep -rn "mcp_connections\|client_instance_id"` 无结果 |
| 迁移名 `v3_38_mcp_connections.sql` | 【冲突】实际的 3.38 是 `v3_38_chunk_embed_hash.sql`，落地时要换版本号 | `app/database.py` `_INCREMENTAL_MIGRATIONS` |
| 重连时原地轮换密钥，旧密钥有宽限期 | 【已有，等价实现】`renew`/`rotate` 加上 `previous_key_hash`/`previous_key_valid_until` | `app/auth.py` `_resolve_principal()`；`tests/test_token_lifecycle.py::test_expired_tokens_renew_in_place_and_secrets_hot_swap` |
| 轮换后身份保持稳定 | 【已有，等价实现】v3.39 的 `agent_id` + `generation`，轮换不改变目录身份 | `sql/migrations/v3_39_token_naming.sql`；`tests/test_token_naming.py`、`tests/test_identity_continuity.py::test_assignee_survives_a_rotation_and_the_new_generation_claims` |
| 凭证默认 90 天 TTL，reaper 撤销过期凭证，连接满 60 天转 dormant、90 天撤销 | 【部分】token 有 `expires_at`，过期后鉴权直接拒绝（`_expired()`），但 CLI token 没有默认 90 天 TTL，也没有 dormant/connection 状态 | `app/auth.py` `_agent_principal()`；`app/services/token_tree.py` |
| 控制台按“连接”分组、`/v1/human/connections` | 【缺失】 | — |
| OAuth 动态客户端映射为连接 | 【部分】MCP OAuth 已有（v3.36 `oauth_clients`/`oauth_requests`），但没有映射到 connection | `app/services/oauth.py`；`tests/test_oauth.py` |

结论：这份提案处理的是 **本地 MCP 客户端到 Veynod** 的连接生命周期，和联邦 peer 连接（愿景 §5）是两回事。
F-03 不能复用 `mcp_connections`，也不能把 CLI 重连当成 peer 续连。

## 6. 安全复审（愿景 §23.1 第 4 条）

### 6.1 RLS 实际使用的数据库角色

- **【已有】** 请求会话 `SET LOCAL ROLE agentgraph_app`；该角色由 `v3_2_production.sql` §12 创建，属性为 `NOLOGIN`，
  有表的 CRUD 权限；激活失败返回 503（fail-closed）。`app.bypass_rls` 分支已删除（`v3_4_security_hardening.sql`）。
- **【已有，残余风险仍在】** 连接角色就是 `DATABASE_URL` 里的用户。`docker-compose.prod.yml:50` 使用 `POSTGRES_USER`
  （默认 `agentgraph`）；在官方 pgvector 镜像里，这个用户是 **superuser**。仓库里没有 `GRANT agentgraph_app TO <login>`，
  如果换成非超级用户连接，`SET ROLE` 会失败，进而导致全部请求 503。这一点和 `docs/SECURITY_AUDIT_2026-10-20.md` M2
  标注的残余风险一致，至今未修复。
- **【已有，风险面】** 共有 306 处 `async with async_session_factory()` 打开新会话（分布在 47 个文件中）。会话只有在调用
  `set_session_tenant()` / `activate_tenant()` 之后才会切换角色，否则以连接角色（superuser）运行，绕过 RLS，
  只能靠应用层 `WHERE` 过滤。例如 `sharing.shared_document()`、`attestation.revoked()`、reaper、目录回填。
- **【缺失】** 以非超级用户运行的集成测试。`tests/` 里没有任何 `rolsuper` 或 `pg_has_role` 断言；
  只有 `app/services/exports.py:931` 在导入时判断是否为 superuser。

### 6.2 权限升级

- **【已有】** 子 token 只能拿到父 token 授权的子集：scopes、读写命名空间、TTL、`max_descendants`、预算（`token_tree.py`），
  角色只能保持或收窄，SUPER_ADMIN 只能由 owner 签发。人类门禁：`ESCALATED_TO_HUMAN`/`AWAITING_HUMAN`
  要求 `is_human`，agent 不能 handoff 成 DONE。证据：`tests/test_token_delegation.py::test_bad_deltas_are_refused`、
  `tests/test_security_hardening.py::test_human_states_require_a_human`、`::test_agent_cannot_review_its_own_handoff`。
- **【已有，风险】** 工作区 key 与 root key 直接获得 COMMANDER 角色和全部命名空间，而且 `agent_hint` 可以把任意字符串
  设成 `agent_signature`（`app/auth.py` `_resolve_principal()`）。审计行另外记录了 `actor_auth_kind` 和 `actor_token_id`
  （安全审计 M5），但 `agent_signature` 这个显示名仍可伪造。联邦审批 **必须** 绑定到 `user_id` 或 `token_cid`，
  不能绑定签名字符串（对应 F-003）。

### 6.3 SSE 重鉴权

- **【已有】** `/v1/stream`（`app/api/stream_routes.py` `_stream_events()`）和 `/v1/inbox/stream`
  （`app/api/inbox_stream.py` `_poke_stream()`）每 `sse_revalidate_seconds`（默认 30 秒）调用一次 `_key_revalidator()`。
  凭证被撤销、过期、换了工作区或租户正在删除时，推送 `closed: credential_revoked` 并断开；角色或命名空间变化对后续事件生效；
  连接最长存活 `sse_max_lifetime_seconds`（3600 秒）；有全局和每主体的连接数上限。证据：`tests/test_security_hardening.py::test_stream_closes_after_token_revocation`。
- **【假设】** 撤销后最多还有 30 秒仍按旧授权推送事件。愿景 §16.1 的“撤销后新请求立即被拒”只对请求成立，对流不成立。
- **【已有，说明】** `admin_stream()` 没有重鉴权器（ADMIN_TOKEN 是静态环境变量），只受存活时间上限约束。

### 6.4 Webhook SSRF

- **【已有】** `app/services/outbound.py` `_resolve_pinned()`：只允许 http/https，生产环境强制 https，支持 `OUTBOUND_ALLOWED_HOSTS`；
  DNS 只解析一次并把 IP 固定下来（防 DNS rebinding），用 SNI 和 Host 头连到固定的 IP；`_is_private()` 拒绝非 global 地址、
  2002::/16、64:ff9b::/96；`send_json()` 设置 `follow_redirects=False`、响应体上限、超时。webhook、wake URL、外部动作在
  创建和每次投递时都会重新校验（`webhooks.py`、`pokes.set_wake_url()`、`external_actions.py`）。
  证据：`tests/test_failure_modes.py::test_private_outbound_target_is_blocked`。
- **【已有，配置风险】** `app/config.py` 中 `outbound_allow_private_networks` 的 **代码默认值是 True**。只靠
  `docker-compose.prod.yml:64` 和 `.env.example` 把它设成 false。用别的方式部署而漏了这项配置，SSRF 防护就会失效。

### 6.5 Token 保存

- **【已有】** 所有 agent、workspace、root key 和人类会话都只存 SHA-256 哈希（`hash_api_key()`）。这些 key 是高熵随机串，
  不加盐的 SHA-256 足够。轮换后旧哈希在 `previous_key_valid_until` 之前仍然有效。CLI 用终端公钥封装后再下发
  （`tests/test_growth.py::test_sealed_key_opens_only_with_the_cli_private_key`）。
- **【已有，风险】** 租户签名私钥（`tenant_signing_keys.private_key`）和假名密钥以 base64 **明文** 存在库里，只靠
  `REVOKE ALL ... FROM agentgraph_app`（`v3_28_phase3_r32.sql`）阻止应用角色读取。但平台会话本身就是 superuser，可以读到。
  联邦签名密钥如果沿用这种存法，数据库备份泄露就等于身份泄露（见 ADR-013）。
- **【缺失】** 文档和消息写入时的密钥识别和清洗（F-006）。

### 6.6 Schema 初始化期间的竞态

- **【已有】** DDL 由 advisory lock 串行化，见 §2。但这个锁只串行化“DDL 与 DDL”，**不挡**“DDL 与正常读写”。
- **【已有，本次实测复现】** 每次启动都会重跑 `sql/init.sql`，其中 `DROP TRIGGER IF EXISTS trg_tasks_notify ON collab_tasks; CREATE TRIGGER …`
  （`sql/init.sql:384-385`）会申请 `collab_tasks` 的 AccessExclusiveLock。本地完整测试时，测试夹具在第一个实例还在服务时
  启动了第二个实例。PostgreSQL 日志 `2026-10-28 00:21:37 UTC` 记录了一次死锁：一方是第一个实例的 `INSERT INTO timeline_streams …`
  （等待 `timeline_streams` 的 RowExclusiveLock），另一方是第二个实例正在执行的 `init.sql`（等待 `collab_tasks` 的 AccessExclusiveLock），
  二者循环等待。PostgreSQL 回滚了 INSERT 那一侧的事务。也就是说，**多副本启动或滚动发布时，存活副本上的正常写入可能因为另一副本重跑 schema 而失败**。
  联邦会增加跨实例的写流量，F-01 应该让 `init.sql` 和 `sql/functions/*.sql` 在 schema 已是最新版本时跳过，或者改成只在迁移中执行。
- **【假设】** 滚动发布时，旧版副本可能在新 schema 上继续服务。迁移需要保持向后兼容（只增不删），目前这只是惯例，没有检查。
- **【假设】** 锁之外的回填操作没有并发测试，见 §2。

### 6.7 跨根租户访问

- **【已有】** 每个请求的 `tenant_scope` 只有一个租户；共享只能在同一个根内；客户端提交的 `tenant_id` 会被拒绝；
  有跨租户模糊测试。证据：`tests/test_tenants.py::test_cross_tenant_fuzz_leaks_nothing`、`::test_client_supplied_tenant_id_is_rejected`、
  `tests/test_tenant_tree.py::test_visibility_sharing_and_admin_access`。
- **【已有，风险】** 公开端点 `/.well-known/agentgraph/tenants/{alias}/revoked` 不需要认证。`attestation.revoked()` 每次请求都会取出
  该租户的全部 `agent_tokens`，逐行计算 HMAC，开销随 token 数线性增长，可能被用来放大请求成本（见 GAP N-06）。
  另外，`/jwks.json` 和 `/revoked` 都可以用来确认某个租户 alias 是否存在；租户第一次被访问时，`_current()`
  会在这个不需认证的 GET 里生成并写入签名密钥。
- **【已有，风险】** 平台会话绕过 RLS（§6.1）。这意味着跨根隔离在这些代码路径上 **只靠应用层**。联邦代码天然要跨根，
  必须走专门的、有审计的跨根通道，不能再多开一个平台会话。

## 7. 测试运行结果

环境：本地 PostgreSQL 16.15 + pgvector 0.6.0（apt `postgresql-16-pgvector`），连接用户 `agentgraph`（superuser，与 CI 的
`pgvector/pgvector:pg16` 服务一致），Python 3.13，按 `requirements-dev.txt` 安装依赖；被测代码为 HEAD `97ea9eb`，没有改动。

```text
ruff check app tests                          → All checks passed!
TEST_DATABASE_URL=… pytest -q                 → 17 failed, 395 passed in 1820.83s (0:30:20)
```

没有运行 `npm test --prefix cli`、`npm test --prefix protocol`、`npm run build --prefix dashboard`（本任务只审后端基线），
F-05 动控制台之前要补跑。

17 个失败逐个归类（失败后又单独重跑过有疑问的用例）：

| 类别 | 数量 | 用例 | 原因 | 判断 |
|---|---|---|---|---|
| A. 测试没跟上 3.12.0 | 10 | `test_event_delivery::test_task_lifecycle_reaches_creator_and_executor`、`test_phase0_fixes::test_review_targets_match_on_rest_and_mcp[approve-DONE-True]`、`test_phase0_fixes::test_review_rounds_and_history`、`test_poke_delivery::test_verdicts_and_subtasks_poke_the_right_people`、`test_production::test_dependency_becomes_ready_when_parent_is_done`、`test_production::test_approved_task_artifact_becomes_verified`、`test_production::test_backfill_verifies_legacy_artifacts_and_clears_downstream_taint`、`test_security_hardening::test_agent_cannot_review_its_own_handoff`、`test_v3_integration::test_task_project_repo_create_read_list_claim_and_legacy_flow`、`test_v3_integration::test_task_lease_handoff_l2_and_dependencies` | 用例用工作区 key 或 AUDITOR 批准 DONE，返回 403 `role_permission_denied`（“only the human OWNER approves DONE”）。3.12.0 的发布说明写明 `task:approve_done` 改为 OWNER 专属 | 产品行为是有意的，**测试没有更新** |
| B. 测试没跟上 3.12.0 | 1 | `test_token_delegation::test_bad_deltas_are_refused` | 期望铸造 COMMANDER agent token 返回 422，实际 201。3.12.0 发布说明写明“COMMANDER becomes a mintable agent role”，写操作另外由 `CommanderGrant` 把关 | 有意的，测试没有更新；**不是**提权漏洞 |
| C. MCP 工具清单漂移 | 2 | `test_root_page::test_root_primitive_list_is_the_full_mcp_surface`、`test_v3_integration::test_mcp_v3_tools` | L3 消息和 SUPER_ADMIN 新增了 `send_message`、`mark_read`、`create_group`、`get_message_receipts`、`update_task` 等工具。根页面的 `ROOT_PRIMITIVES`（`app/root_page.py`，`app/capabilities.py` 会用到）没有列出它们，测试里的期望集合也没有更新 | 根页面和能力描述 **漏列了新工具**（小的产品缺陷），测试期望也要同步 |
| D. **真实的隔离缺口** | 1 | `test_tenants::test_every_owned_row_carries_a_derived_tenant` | `blobs`、`document_blobs`、`blob_uploads`、`cleanup_grants`、`share_links`（v3.40）这 5 张表有 `workspace_id`，但没有 `tenant_id`，**没有启用 RLS**（`relrowsecurity = false`），`agentgraph_app` 却有完整 CRUD。应用层查询都带了 `workspace_id` 过滤（`app/services/blobs.py`、`share_links.py`），所以目前没有发现可利用的泄露，但这 5 张表少了数据库这一层的隔离 | **产品缺陷**，见 GAP N-12 |
| E. 环境或测试隔离问题 | 2 | `test_events::test_seq_is_gap_free_under_concurrent_writes`（`httpx.ReadTimeout`，单独重跑也一样）；`test_phase3_r32::test_scheduled_exports`（单独重跑也失败） | 前者在本机并发写 1000 个文档时超时，本机单次写入约 400ms，比 CI 慢；后者推断是 `export_schedule.run_due()` 每轮只处理 `plan()` 返回的前 20 个租户，而测试库里已经积累了全量测试建的大量租户，新租户排不进去 | 不代表产品回归；需要在 CI 的干净库上复核 |
| F. 需要 owner 判断 | 1 | `test_feedback::test_browser_root_serves_human_page_and_json_stays_available` | 浏览器访问 `/` 返回的落地页里没有 `href="/dashboard/"`（`app/root_page.py` 里有这个链接，但实际返回的是另一张落地页） | 可能是有意改版，也可能是控制台入口丢了 |

结论：**main 上有 13 个测试因为代码已经有意变更而过时（A + B + C 中的测试期望部分）**。又因为 CI 只能手动触发（§3），
这些红灯没有被发现。联邦任务开工前应该先让测试套件恢复全绿（GAP N-13），否则后面任务“测试通过”的证据没有意义。

## 8. 假设清单（后续任务需要验证）

| ID | 假设 | 由谁验证 |
|---|---|---|
| H-01 | 每份迁移都能安全地重复执行（§2） | F-01：迁移前后各跑一遍 `apply_schema()` |
| H-02 | 锁外回填在多副本下是幂等的（§2、§6.6）。注意：“重跑 init.sql 与存活写入死锁”已经实测复现，不再是假设（GAP N-11） | F-06：故障注入 |
| H-03 | 撤销后最多 30 秒仍按旧授权推送 SSE（§6.3） | F-03/F-06：测量撤销到断流的延迟 |
| H-04 | 旧版副本能在新 schema 上安全服务（§6.6） | F-01：迁移只增不删 |
| H-05 | 改成非超级用户连接后，测试仍能通过（§6.1） | ADR-010 批准后在 F-01 中验证 |
