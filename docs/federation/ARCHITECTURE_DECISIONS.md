# 联邦 F-00：架构决策记录（ARCHITECTURE_DECISIONS）

> 基线：HEAD `97ea9eb`，应用 3.13.1，schema 3.48.0。
> **决策来源**：2026-10-28，owner 在会话中授权执行方（`agent:clauderoom`）自行决定（原话：“你自己决定”）。
> 下面每条的“状态”记录了实际采纳的选项。owner 可以随时推翻其中任何一条，推翻后按愿景 §22.2 的方法重新登记。
> 两类事情 **不在** 授权范围内，仍然要由 owner 执行：生产环境的变更（ADR-010 的连接角色切换、ADR-013 的 KEK 下发），
> 以及在板子上把任务批准为 DONE。
> ADR-001 至 ADR-009 来自愿景 v0.3 §22.1，这里补充了代码依据；ADR-010 至 ADR-014 是这次基线审查新提出的。
> 愿景 §22.2 列出的商业和治理 TBD（收费、结算介质、裁决主体、处罚公开等）**不在** 本次决定范围内，仍然保持 TBD。

每条 ADR 的格式：背景（代码依据） / 选项 / 推荐 / 被拒时的代价 / 影响的用例 / 状态。

---

## ADR-001 全局规范身份与可读别名分离

- **背景**：`agent_directory` 以 `(tenant_id, agent_id)` 唯一（`app/services/directory.py` `_taken()`），已经有别名表 `agent_aliases` 和改名历史 `name_history()`；`agent_id` 在两个根租户里可以重复。
- **选项**：A. 新建 `federation_subjects`、`federation_workspaces`、`federation_agents`，用 ULID 作主键，永不复用，再映射到本地 `tenant_id`、`workspace_id`、`agent_id`；B. 把本地 `canonical_id`（`tok_…`、`prn_…`）直接拿来对外。
- **推荐**：A。本地 id 会随 token 轮换或租户迁移而变化，而且会暴露内部结构。
- **被拒的代价**：别名和地址一旦转移或被抢注，就很难补救（G13）。
- **影响用例**：F-001、F-004。
- **状态**：**已决定（2026-10-28）：采用 A。** 规范 ID 为带前缀的 ULID（`fsub_` / `fws_` / `fag_`），一经分配永不复用。可读别名的字符串格式仍是 TBD（愿景 §3.2），但它不阻塞 F-01：别名单独放在 `federation_addresses` 表，以后可以改。

## ADR-002 联邦授权采用双向、方向性的 capability grant

- **背景**：`CommanderGrant`（`app/services/grants.py`）已经实现了“受托者 + 动作 + 范围 + 到期 + 撤销 + 每次请求校验”，并有测试 `tests/test_commander_grants.py`。
- **选项**：A. 仿照 `CommanderGrant` 新建 `federation_grants`（方向性，带 `grant_version` 和 `revocation_epoch`），并配一张只追加的 `federation_grant_history`；B. 在连接上放一个 `is_friend` 布尔值，再加一个权限列表。
- **推荐**：A。
- **被拒的代价**：以后加任务或工具类权限时，“加好友”会把所有权限一起放出去。
- **影响用例**：F-002、F-004、F-005。
- **状态**：**已决定（2026-10-28）：采用 A。** 首版禁止再委派（`may_redelegate` 恒为 false，愿景 §3.3）。

## ADR-003 消息至少投递一次，业务层去重

- **背景**：现有 outbox 和游标重放（`app/services/events.py`）、webhook 的 30/120/600 秒重试和 DEAD（`webhooks.py`）、24 小时幂等（`idempotency.py`）都是“至少一次”的语义。
- **选项**：A. 至少一次投递 + `message_id` 唯一约束 + 回执分三级（`accepted_locally` / `received` / `processed`）；B. 宣称恰好一次。
- **推荐**：A。信封 schema 放在 `protocol/`（zod），服务端再用 pydantic 校验一遍。canonical 序列化（JCS 或 CBOR）留到 F-02 定。
- **被拒的代价**：重试之后出现重复消息和重复任务。
- **影响用例**：F-007、F-008、F-012、F-013。
- **状态**：**已决定（2026-10-28）：采用 A。** canonical 序列化在 F-02 选定（倾向 RFC 8785 JCS）。

## ADR-004 本地任务与联邦委派之间用关联映射

- **背景**：`collab_tasks` 的状态机由 `app/services/tasks.py` 控制，有租约 fencing（`lease_epoch`）和验收锁定（v3.41）。
- **选项**：A. 新建 `federation_delegations(federation_job_id, requester_task_id, provider_task_id, state, version, acceptance_hash, budget_cap)`，双方各用自己的本地任务，只通过 `tasks.py` 的公开函数改状态；B. 在 `collab_tasks` 上加远端状态。
- **推荐**：A。跨域状态枚举（`OFFERED` … `ACCEPTED_RESULT`）只放在 `federation_delegations` 上。
- **被拒的代价**：旧任务语义被侵入，迁移会波及所有现有任务。
- **影响用例**：F-012 至 F-016。
- **状态**：**已决定（2026-10-28）：采用 A。**

## ADR-005 首版额度借用：不转借凭证，不做真实支付

- **背景**：现有 token 只是本地凭证（`agent_tokens`）；计量只有强制上限（`token_budget.py`、`usage.py`），没有收据结构。
- **选项**：A. 远端用自己的资源执行，只回传 `federation_cost_receipts`（不是支付流水）；B. 允许请求方的 token 或模型 key 给远端使用。
- **推荐**：A。schema 层面禁止任何字段承载对方的凭证（F-006 测试会检查）。
- **被拒的代价**：密钥泄露、计费纠纷、违反上游服务条款。
- **影响用例**：F-006、F-017。
- **状态**：**已决定（2026-10-28）：采用 A。**

## ADR-006 先在同一平台落地，独立部署只保留传输抽象

- **背景**：现在所有服务都假设同库、同 RLS；没有任何实例间传输代码。
- **选项**：A. P1/P2 在同一部署的不同根租户之间实现，但 peer 入口只接受签名信封（不读对方的库），传输层通过 `Transport` 接口注入；B. 一开始就做实例间传输。
- **推荐**：A。在 F-11 里用两个进程、两个数据库验证。验证之前，不对外宣称支持跨部署。
- **被拒的代价**：选 A 但不做抽象，以后要重写协议；选 B 则工作量翻倍。
- **影响用例**：F-021、F-022。
- **状态**：**已决定（2026-10-28）：采用 A。**

## ADR-007 信任证据不自动公开私人消息

- **背景**：已经有运营隐私规则（`app/services/operator_privacy.py`，运营只看计数、不看内容）和权利请求（`rights.py`）。
- **选项**：A. 声誉只取履约事件的摘要和哈希；B. 从消息正文提取。
- **推荐**：A。
- **影响用例**：F-026。
- **状态**：**已决定（2026-10-28）：采用 A。**

## ADR-008 企业离线模式不依赖公共目录和公钥服务

- **背景**：已经有 `OUTBOUND_ALLOWED_HOSTS` 和 `feature_flags()`；但 `outbound_allow_private_networks` 的代码默认值是 True（N-08）。
- **推荐**：`FEDERATION_ENABLED=false` 时，任何代码路径都不发起公网请求；用测试拦截出站 socket 来证明（F-022）。
- **状态**：**已决定（2026-10-28）：采用。** 由 F-06 的测试来证明。

## ADR-009 服务器能否读联邦正文，以及密钥管理方向

- **背景**：现有会话消息以明文存在 `messages` 表（`app/services/messaging.py`），搜索、权利请求、运营隐私都依赖服务器能读到正文。
- **选项**：A. 服务器可读（最快，复用现有能力）；B. 端到端加密（平台只见密文，搜索、反滥用和取证都要重新设计）；C. 按连接协商，不允许静默退回明文。
- **推荐**：P1 内部 Alpha 先用 A，但 `federation_messages` 预留 `payload_encoding` 和 `key_ref` 字段，并在协议里协商加密方式，这样以后切到 C 不用重建表。这只是技术推荐，**产品选择必须由 owner 决定**。
- **被拒的代价**：以后再加端到端加密要大面积返工（愿景 §9.5）。
- **影响用例**：F-029。
- **状态**：**已决定（2026-10-28）：P1 内部 Alpha 采用 A（服务器可读），同时按 C 预留字段。** `federation_messages` 带 `payload_encoding`（P1 只允许 `plain`）和 `key_ref`（P1 恒为空）；连接上记录 `encryption_policy`（P1 只允许 `server_readable`）。协议握手时双方声明支持的策略；一旦连接被设成更强的策略，就拒绝降级（对应 F-029）。**复审条件**：开放 Public Opt-in（`FEDERATION_PUBLIC_DISCOVERY_ENABLED`）之前，owner 必须重新确认这一条。

---

## ADR-010 联邦上线前，数据库连接改为非超级用户

- **背景**：N-01。连接角色是 superuser（`docker-compose.prod.yml:50`）；`agentgraph_app` 是 NOLOGIN，而且没有授予任何登录角色（`v3_2_production.sql` §12）；306 处平台会话绕过 RLS（N-02）。
- **选项**：A. 新建登录角色 `agentgraph_login`（NOSUPERUSER、NOBYPASSRLS），执行 `GRANT agentgraph_app TO agentgraph_login`；平台会话改为显式的 `agentgraph_platform` 角色（BYPASSRLS，只给需要的表），迁移仍用 owner 角色执行；启动时自检 `rolsuper = false`。B. 保持现状，联邦代码只靠应用层隔离。
- **推荐**：A，作为 F-01 的前置子任务。这是部署层改动：要改生产 `.env`，并在维护窗口内执行，**必须由 owner 执行或批准**。
- **被拒的代价**：联邦把跨根流量引进来之后，任何一处漏写的 `WHERE` 都会变成跨租户泄露。
- **影响用例**：F-001、F-018、F-021。
- **状态**：**已决定（2026-10-28）：采用 A。** 代码部分（登录角色的创建脚本、`GRANT agentgraph_app TO <login>`、启动自检、非超级用户集成测试）放在 F-01 里做；启动自检默认只告警，配置 `DB_REQUIRE_NON_SUPERUSER=true` 才拒绝启动。**生产切换（改 `.env` 并重启）由 owner 执行。**

## ADR-011 联邦表的放置：全局表与租户表

- **背景**：现有表都带 `tenant_id` 并启用 RLS；签名密钥表 `tenant_signing_keys` 是平台专用表（应用角色无权访问）。
- **推荐**：
  - **全局表**（平台专用、不启用 RLS、只存最少的元数据）：`federation_subjects`、`federation_addresses`、`federation_workspaces` 的公开部分、平台裁决。
  - **租户表**（RLS，沿用 `v3_48` 模板）：`federation_peers`（每个方向一行，各存在自己那一侧）、`federation_grants`、`federation_grant_history`、`federation_threads`、`federation_messages`、`federation_delivery_attempts`、`federation_delegations`、`federation_cost_receipts`、`federation_blocks`、`federation_reports`。
  - 双方各存一份自己能看到的副本，跨根只通过投递接口写入对方的本地对象，**不建任何跨根 JOIN**（愿景 §10.3）。
- **状态**：**已决定（2026-10-28）：采用上面的划分。**

## ADR-012 联邦消息是否复用现有会话

- **背景**：`conversations` / `conversation_members` / `messages` / `message_deliveries`（v3.45–3.46）已经有回执和读水位，但只在工作区内使用，没有污点，`send_message` 也不幂等（N-04）。
- **选项**：A. 单独建 `federation_threads` 和 `federation_messages`，到达对方之后再以“外部来源”身份写进对方本地的 `conversations`（成员 `member_ref` 标为 `fed:<federation_agent_id>`）；B. 直接给 `conversations` 加跨工作区成员。
- **推荐**：A。B 会让本地会话表承载跨根成员，RLS 无法正确表达。
  顺带一个小改动：把本地 `send_message` 加入 MCP 通用幂等工具列表（`app/mcp_server/server.py` 约第 98 行）。这会改变现有工具的契约（增加一个可选参数），**需要单独确认**。
- **状态**：**已决定（2026-10-28）：采用 A。** 本地 `send_message` 加入幂等工具列表放在 F-04 里做；这只是增加一个可选参数，旧调用方不受影响。

## ADR-013 联邦签名密钥的保管

- **背景**：N-05。租户 Ed25519 私钥以 base64 明文存在库里。
- **选项**：A. 信封加密：私钥用 `FEDERATION_KEK`（环境变量或 KMS）加密后再入库，应用只在内存里解开；B. 外部 KMS 或 HSM 签名；C. 沿用现状。
- **推荐**：P1 用 A，并预留 B 的接口。KEK 怎么轮换，在 F-02 里写清楚。
- **状态**：**已决定（2026-10-28）：采用 A，并预留 B 的接口。** 实现放在 F-02；`FEDERATION_KEK` 的生成和下发由 owner 执行。

## ADR-014 测试门禁

- **背景**：N-07。CI 只能手动触发。
- **推荐**：在 `.github/workflows/test.yml` 中增加 `pull_request` 触发。联邦每个任务的 PR 都要附上 HEAD SHA、schema 版本、pytest 汇总和前端构建结果（愿景 §21“证据要求”）。
- **状态**：**已决定（2026-10-28）：采用。** 放在“F-01 前置”PR 里：`test.yml` 增加 `pull_request` 和推送到 `main` 两种触发。

---

## 决策登记

| ADR | 决定 | 落地任务 | 需要 owner 执行的部分 |
|---|---|---|---|
| ADR-001 | A：带前缀的 ULID 规范 ID，别名格式仍 TBD | F-01 / F-02 | — |
| ADR-002 | A：方向性 grant + `grant_version`，禁止再委派 | F-03 | — |
| ADR-003 | A：至少一次投递 + 业务去重 | F-04 | — |
| ADR-004 | A：`federation_delegations` 关联映射 | F-07 | — |
| ADR-005 | A：不转借凭证，只回传成本收据 | F-07 / F-09 | — |
| ADR-006 | A：同平台先行，保留传输抽象 | F-02 / F-11 | — |
| ADR-007 | A：只用履约摘要 | P4 | — |
| ADR-008 | 采用 | F-06 | — |
| ADR-009 | P1 服务器可读，按 C 预留字段，禁止降级 | F-01 / F-04 | Public Opt-in 前重新确认 |
| ADR-010 | A：非超级用户连接 | F-01 | 生产 `.env` 切换与重启 |
| ADR-011 | 全局表 / 租户表划分 | F-01 | — |
| ADR-012 | A：单独建联邦消息表，到达后写入本地会话 | F-04 | — |
| ADR-013 | A：信封加密，预留 KMS 接口 | F-02 | 生成并下发 `FEDERATION_KEK` |
| ADR-014 | 采用：PR 自动跑测试 | F-01 前置 | — |
