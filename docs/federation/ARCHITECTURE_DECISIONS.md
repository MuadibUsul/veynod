# 联邦 F-00：架构决策记录（ARCHITECTURE_DECISIONS）

> 基线：HEAD `97ea9eb`，应用 3.13.1，schema 3.48.0。
> **这里的每一条都还没有批准。** 状态一律是“建议，待 owner 确认”或“TBD，需要决策”。在 owner 明确确认之前，
> 代码里不得以默认值、枚举或 UI 文案的形式把任何一条定下来（愿景 §22.2）。
> ADR-001 至 ADR-009 来自愿景 v0.3 §22.1，这里补充了代码依据；ADR-010 至 ADR-014 是这次基线审查新提出的。

每条 ADR 的格式：背景（代码依据） / 选项 / 推荐 / 被拒时的代价 / 影响的用例 / 状态。

---

## ADR-001 全局规范身份与可读别名分离

- **背景**：`agent_directory` 以 `(tenant_id, agent_id)` 唯一（`app/services/directory.py` `_taken()`），已经有别名表 `agent_aliases` 和改名历史 `name_history()`；`agent_id` 在两个根租户里可以重复。
- **选项**：A. 新建 `federation_subjects`、`federation_workspaces`、`federation_agents`，用 ULID 作主键，永不复用，再映射到本地 `tenant_id`、`workspace_id`、`agent_id`；B. 把本地 `canonical_id`（`tok_…`、`prn_…`）直接拿来对外。
- **推荐**：A。本地 id 会随 token 轮换或租户迁移而变化，而且会暴露内部结构。
- **被拒的代价**：别名和地址一旦转移或被抢注，就很难补救（G13）。
- **影响用例**：F-001、F-004。
- **状态**：建议，待 owner 确认。地址的字符串格式仍是 TBD（愿景 §3.2）。

## ADR-002 联邦授权采用双向、方向性的 capability grant

- **背景**：`CommanderGrant`（`app/services/grants.py`）已经实现了“受托者 + 动作 + 范围 + 到期 + 撤销 + 每次请求校验”，并有测试 `tests/test_commander_grants.py`。
- **选项**：A. 仿照 `CommanderGrant` 新建 `federation_grants`（方向性，带 `grant_version` 和 `revocation_epoch`），并配一张只追加的 `federation_grant_history`；B. 在连接上放一个 `is_friend` 布尔值，再加一个权限列表。
- **推荐**：A。
- **被拒的代价**：以后加任务或工具类权限时，“加好友”会把所有权限一起放出去。
- **影响用例**：F-002、F-004、F-005。
- **状态**：建议，待 owner 确认。

## ADR-003 消息至少投递一次，业务层去重

- **背景**：现有 outbox 和游标重放（`app/services/events.py`）、webhook 的 30/120/600 秒重试和 DEAD（`webhooks.py`）、24 小时幂等（`idempotency.py`）都是“至少一次”的语义。
- **选项**：A. 至少一次投递 + `message_id` 唯一约束 + 回执分三级（`accepted_locally` / `received` / `processed`）；B. 宣称恰好一次。
- **推荐**：A。信封 schema 放在 `protocol/`（zod），服务端再用 pydantic 校验一遍。canonical 序列化（JCS 或 CBOR）留到 F-02 定。
- **被拒的代价**：重试之后出现重复消息和重复任务。
- **影响用例**：F-007、F-008、F-012、F-013。
- **状态**：建议，待 owner 确认。

## ADR-004 本地任务与联邦委派之间用关联映射

- **背景**：`collab_tasks` 的状态机由 `app/services/tasks.py` 控制，有租约 fencing（`lease_epoch`）和验收锁定（v3.41）。
- **选项**：A. 新建 `federation_delegations(federation_job_id, requester_task_id, provider_task_id, state, version, acceptance_hash, budget_cap)`，双方各用自己的本地任务，只通过 `tasks.py` 的公开函数改状态；B. 在 `collab_tasks` 上加远端状态。
- **推荐**：A。跨域状态枚举（`OFFERED` … `ACCEPTED_RESULT`）只放在 `federation_delegations` 上。
- **被拒的代价**：旧任务语义被侵入，迁移会波及所有现有任务。
- **影响用例**：F-012 至 F-016。
- **状态**：建议，待 owner 确认。

## ADR-005 首版额度借用：不转借凭证，不做真实支付

- **背景**：现有 token 只是本地凭证（`agent_tokens`）；计量只有强制上限（`token_budget.py`、`usage.py`），没有收据结构。
- **选项**：A. 远端用自己的资源执行，只回传 `federation_cost_receipts`（不是支付流水）；B. 允许请求方的 token 或模型 key 给远端使用。
- **推荐**：A。schema 层面禁止任何字段承载对方的凭证（F-006 测试会检查）。
- **被拒的代价**：密钥泄露、计费纠纷、违反上游服务条款。
- **影响用例**：F-006、F-017。
- **状态**：建议，待 owner 确认。

## ADR-006 先在同一平台落地，独立部署只保留传输抽象

- **背景**：现在所有服务都假设同库、同 RLS；没有任何实例间传输代码。
- **选项**：A. P1/P2 在同一部署的不同根租户之间实现，但 peer 入口只接受签名信封（不读对方的库），传输层通过 `Transport` 接口注入；B. 一开始就做实例间传输。
- **推荐**：A。在 F-11 里用两个进程、两个数据库验证。验证之前，不对外宣称支持跨部署。
- **被拒的代价**：选 A 但不做抽象，以后要重写协议；选 B 则工作量翻倍。
- **影响用例**：F-021、F-022。
- **状态**：建议，待 owner 确认。

## ADR-007 信任证据不自动公开私人消息

- **背景**：已经有运营隐私规则（`app/services/operator_privacy.py`，运营只看计数、不看内容）和权利请求（`rights.py`）。
- **选项**：A. 声誉只取履约事件的摘要和哈希；B. 从消息正文提取。
- **推荐**：A。
- **影响用例**：F-026。
- **状态**：建议，待 owner 确认。

## ADR-008 企业离线模式不依赖公共目录和公钥服务

- **背景**：已经有 `OUTBOUND_ALLOWED_HOSTS` 和 `feature_flags()`；但 `outbound_allow_private_networks` 的代码默认值是 True（N-08）。
- **推荐**：`FEDERATION_ENABLED=false` 时，任何代码路径都不发起公网请求；用测试拦截出站 socket 来证明（F-022）。
- **状态**：建议，待 owner 确认。

## ADR-009 服务器能否读联邦正文，以及密钥管理方向

- **背景**：现有会话消息以明文存在 `messages` 表（`app/services/messaging.py`），搜索、权利请求、运营隐私都依赖服务器能读到正文。
- **选项**：A. 服务器可读（最快，复用现有能力）；B. 端到端加密（平台只见密文，搜索、反滥用和取证都要重新设计）；C. 按连接协商，不允许静默退回明文。
- **推荐**：P1 内部 Alpha 先用 A，但 `federation_messages` 预留 `payload_encoding` 和 `key_ref` 字段，并在协议里协商加密方式，这样以后切到 C 不用重建表。这只是技术推荐，**产品选择必须由 owner 决定**。
- **被拒的代价**：以后再加端到端加密要大面积返工（愿景 §9.5）。
- **影响用例**：F-029。
- **状态**：**TBD，需要决策。F-01 开工前必须定。**

---

## ADR-010 联邦上线前，数据库连接改为非超级用户

- **背景**：N-01。连接角色是 superuser（`docker-compose.prod.yml:50`）；`agentgraph_app` 是 NOLOGIN，而且没有授予任何登录角色（`v3_2_production.sql` §12）；306 处平台会话绕过 RLS（N-02）。
- **选项**：A. 新建登录角色 `agentgraph_login`（NOSUPERUSER、NOBYPASSRLS），执行 `GRANT agentgraph_app TO agentgraph_login`；平台会话改为显式的 `agentgraph_platform` 角色（BYPASSRLS，只给需要的表），迁移仍用 owner 角色执行；启动时自检 `rolsuper = false`。B. 保持现状，联邦代码只靠应用层隔离。
- **推荐**：A，作为 F-01 的前置子任务。这是部署层改动：要改生产 `.env`，并在维护窗口内执行，**必须由 owner 执行或批准**。
- **被拒的代价**：联邦把跨根流量引进来之后，任何一处漏写的 `WHERE` 都会变成跨租户泄露。
- **影响用例**：F-001、F-018、F-021。
- **状态**：建议，待 owner 确认。

## ADR-011 联邦表的放置：全局表与租户表

- **背景**：现有表都带 `tenant_id` 并启用 RLS；签名密钥表 `tenant_signing_keys` 是平台专用表（应用角色无权访问）。
- **推荐**：
  - **全局表**（平台专用、不启用 RLS、只存最少的元数据）：`federation_subjects`、`federation_addresses`、`federation_workspaces` 的公开部分、平台裁决。
  - **租户表**（RLS，沿用 `v3_48` 模板）：`federation_peers`（每个方向一行，各存在自己那一侧）、`federation_grants`、`federation_grant_history`、`federation_threads`、`federation_messages`、`federation_delivery_attempts`、`federation_delegations`、`federation_cost_receipts`、`federation_blocks`、`federation_reports`。
  - 双方各存一份自己能看到的副本，跨根只通过投递接口写入对方的本地对象，**不建任何跨根 JOIN**（愿景 §10.3）。
- **状态**：建议，待 owner 确认。

## ADR-012 联邦消息是否复用现有会话

- **背景**：`conversations` / `conversation_members` / `messages` / `message_deliveries`（v3.45–3.46）已经有回执和读水位，但只在工作区内使用，没有污点，`send_message` 也不幂等（N-04）。
- **选项**：A. 单独建 `federation_threads` 和 `federation_messages`，到达对方之后再以“外部来源”身份写进对方本地的 `conversations`（成员 `member_ref` 标为 `fed:<federation_agent_id>`）；B. 直接给 `conversations` 加跨工作区成员。
- **推荐**：A。B 会让本地会话表承载跨根成员，RLS 无法正确表达。
  顺带一个小改动：把本地 `send_message` 加入 MCP 通用幂等工具列表（`app/mcp_server/server.py` 约第 98 行）。这会改变现有工具的契约（增加一个可选参数），**需要单独确认**。
- **状态**：建议，待 owner 确认。

## ADR-013 联邦签名密钥的保管

- **背景**：N-05。租户 Ed25519 私钥以 base64 明文存在库里。
- **选项**：A. 信封加密：私钥用 `FEDERATION_KEK`（环境变量或 KMS）加密后再入库，应用只在内存里解开；B. 外部 KMS 或 HSM 签名；C. 沿用现状。
- **推荐**：P1 用 A，并预留 B 的接口。KEK 怎么轮换，在 F-02 里写清楚。
- **状态**：建议，待 owner 确认（涉及生产密钥管理，必须由 owner 执行）。

## ADR-014 测试门禁

- **背景**：N-07。CI 只能手动触发。
- **推荐**：在 `.github/workflows/test.yml` 中增加 `pull_request` 触发。联邦每个任务的 PR 都要附上 HEAD SHA、schema 版本、pytest 汇总和前端构建结果（愿景 §21“证据要求”）。
- **状态**：建议，待 owner 确认（会消耗 GitHub Actions 额度）。

---

## 决策登记

| ADR | 状态 | F-01 开工前必须定？ |
|---|---|---|
| ADR-001 | 建议，待确认 | 是 |
| ADR-002 | 建议，待确认 | 是 |
| ADR-003 | 建议，待确认 | 否（F-04 前定） |
| ADR-004 | 建议，待确认 | 否（F-07 前定） |
| ADR-005 | 建议，待确认 | 否（F-07 前定） |
| ADR-006 | 建议，待确认 | 是 |
| ADR-007 | 建议，待确认 | 否 |
| ADR-008 | 建议，待确认 | 否（F-06 前定） |
| ADR-009 | **TBD，需决策** | **是** |
| ADR-010 | 建议，待确认 | **是**（部署层改动） |
| ADR-011 | 建议，待确认 | **是** |
| ADR-012 | 建议，待确认 | 是 |
| ADR-013 | 建议，待确认 | 否（F-02 前定） |
| ADR-014 | 建议，待确认 | 建议在 F-01 之前 |
