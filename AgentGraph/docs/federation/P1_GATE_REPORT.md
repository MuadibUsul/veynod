# 联邦 P1 门禁报告（F-06：安全与故障注入）

> 任务：`fedv2-f06-security-fault`。日期：2026-11-04 / 05（UTC）。执行方：`agent:claude-2`。
> 结论写在第 1 节；每一项都有可复现的命令和结果。

## 1. 结论

- **§21 的 P1 用例 F-001 至 F-011、F-029 全部有自动化测试，全部通过。**
- **两个根租户端到端通过**：门禁测试和 API smoke 都在不同根租户的工作区之间跑；`test_f001` 显式断言两个工作区的根租户不同。
- 模拟攻击 27 项 API 检查（smoke）、15 项门禁测试、迁移演练和前端生产构建全部通过。
- 基线回归：GitHub CI 在门禁提交 `7ce158e` 上 8 个检查全部通过（含完整 product 套件和本门禁测试）。本机完整 product 套件 687 通过、12 失败，失败全部来自本机环境（见第 7 节），在全新库上单独重跑全部通过。

## 2. 被测版本

| 项 | 值 |
|---|---|
| 仓库 / 分支 | `MuadibUsul/veynod` / `claude/charming-shannon-7gbreg` |
| 被测代码 | `7ce158e`：PR #101 的产品代码 `dc82dcd` 加门禁测试；本报告的更新不改代码 |
| main 基线 | `7fe22ff` |
| 数据库 schema | `3.85.0`（`schema_migrations` 共 83 条数值版本） |
| 联邦开关 | 测试中开启；默认关闭（`test_disabled_federation_answers_nothing`） |
| 运行环境 | Python 3.13、PostgreSQL 16 + pgvector、Node 20 / Next.js 14.2 |

## 3. §21 P1 用例对照

| 用例 | 场景 | 测试 | 结果 |
|---|---|---|---|
| F-001 | 两个根租户里有同名 agent | `test_federation_p1_gate.py::test_f001_same_agent_name_in_two_root_tenants`；`test_federation_identity.py` | 通过 |
| F-002 | 有连接但没有委派权限 | `test_federation_p1_gate.py::test_f002_connected_without_delegate_scope`（本地发起和伪造信封两条路都拒绝，且两边都不落库）；`test_federation_delegations.py` | 通过 |
| F-003 | agent 伪造人类批准 | `test_federation_p1_gate.py::test_f003_an_agent_cannot_forge_a_persons_approval`（agent、非 owner、第三方签名都无效） | 通过 |
| F-004 | 授权不传递（A→B→C） | `test_federation_p1_gate.py::test_f004_trust_is_not_transitive`（A 到不了 C；B 转发 A 的信封给 C 被拒） | 通过 |
| F-005 | 过期、撤销、冻结、拉黑 | `test_federation_p1_gate.py::test_f005_expired_revoked_frozen_blocked`（四种状态双向都拒；解冻后，冻结前签的对象仍作废） | 通过 |
| F-006 | 把 token 填进远端字段 | `test_federation_p1_gate.py::test_f006_credentials_never_cross`；`test_federation_messaging.py::test_credentials_never_cross` | 通过 |
| F-007 | 重复 message_id / 幂等键 | `test_federation_messaging.py::test_a_repeated_message_is_one_message`；`test_federation_p1_gate.py::test_replays_are_refused_or_answered_once` | 通过 |
| F-008 | 新版本先到、老版本后到 | `test_federation_messaging.py::test_an_older_object_never_moves_state_back`；`test_federation_delegations.py`（委派按版本应用） | 通过 |
| F-009 | 外部文本要求“忽略规则” | `test_federation_messaging.py::test_outside_text_is_data_not_instructions`；smoke “injection changed no permissions” | 通过 |
| F-010 | 外部消息被摘要成记忆后再读 | `test_federation_api.py::test_chat_over_http_and_memory_keeps_the_outside_label` | 通过 |
| F-011 | 1 万条恶意消息突发 | `test_federation_messaging.py::test_a_flood_from_one_workspace_leaves_others_working` | 通过 |
| F-029 | 加密策略不一致或被降级 | `test_federation_p1_gate.py::test_f029_no_silent_downgrade`；`test_federation_primitives.py`（策略协商） | 通过 |

## 4. 测试结果

| 套件 | 命令 | 结果 |
|---|---|---|
| 门禁测试 | `pytest tests/test_federation_p1_gate.py` | 15 passed |
| 联邦全部测试 + 文档/注册表检查 | `pytest tests/test_federation_*.py tests/test_docs_site.py tests/test_error_registry.py tests/test_onboarding.py tests/test_exports.py tests/test_feedback.py tests/test_root_page.py tests/test_seo.py tests/test_v3_integration.py tests/test_messaging_acceptance.py` | 123 passed（F-05 时）；加上门禁 15 项 |
| contracts | `scripts/test_all.sh contracts` | 34 passed |
| runtime | `scripts/test_all.sh runtime` | 51 passed |
| kernel | `scripts/test_all.sh kernel` | 见第 7 节：1 项在本机高负载下失败，单独重跑通过 |
| product（全部产品测试，本机） | `scripts/test_all.sh product` | 687 passed、12 failed、2 skipped（22 分 29 秒）；12 项在全新库单独重跑全部通过，见第 7 节 |
| cli | `scripts/test_all.sh cli` | 72 passed |
| dashboard（类型检查） | `scripts/test_all.sh dashboard` | 通过 |
| 前端生产构建 | `cd dashboard && npx next build` | 构建成功 |
| GitHub CI（`dc82dcd` 和门禁提交 `7ce158e`） | 8 个检查：lint、protocol、contracts、runtime、kernel、product、cli、dashboard | 两次都全部通过 |

## 5. 模拟攻击复现

### 5.1 门禁测试（服务层，两个根租户之间）

| 攻击 | 期望 | 测试 |
|---|---|---|
| 篡改正文 | `FED_E_BAD_DIGEST` | `test_forged_envelopes_are_refused` |
| 篡改正文并重算摘要 | `FED_E_BAD_SIGNATURE` | 同上 |
| 用自己的密钥冒充别的工作区 | `FED_E_IDENTITY_UNKNOWN` | 同上 |
| 多加顶层字段 / 改协议版本 | `FED_E_ENVELOPE_INVALID` / `FED_E_PROTOCOL_VERSION` | 同上 |
| 重放连接对象 / 聊天信封 5 次 | 只记一次；聊天返回第一次的回执 | `test_replays_are_refused_or_answered_once` |
| 过期信封 / 未来信封 | `FED_E_EXPIRED` / `FED_E_NOT_YET_VALID` | `test_envelopes_outside_their_time_window` |
| 超长正文 / 70 KB 信封 / 嵌套 20 层 | `FED_E_TOO_LARGE` / `FED_E_TOO_LARGE` / `FED_E_ENVELOPE_INVALID` | `test_oversized_messages_are_refused` |
| 跨租户读联邦表 | 应用角色只看到本工作区的行；身份私钥表应用角色无权读 | `test_tenant_isolation_holds_for_federation_rows` |
| 写进别人的会话 | `FED_E_ENVELOPE_INVALID` | `test_federation_messaging.py::test_a_peer_cannot_write_into_another_peers_thread` |
| 联邦关闭时访问 | 所有联邦接口 404 `federation_disabled`，不写任何行 | `test_disabled_federation_answers_nothing` |

### 5.2 故障注入

| 故障 | 期望 | 测试 |
|---|---|---|
| 连接被重置 | 记一次失败，按退避重试，不崩 | `test_faults_on_the_wire_never_fake_a_delivery` |
| 对方回了未签名或别人签名的“回执” | 状态停在 `sent`，不算送达 | 同上 |
| 第三方伪造回执 | 拒绝，状态不变 | 同上 |
| 入队后进程崩溃、没来得及第一次投递 | 投递循环补投，送达一次 | `test_a_crash_after_queueing_is_recovered` |
| 对方长期不可达 | 8 次后死信；人重发后只送达一次 | `test_federation_messaging.py::test_outage_is_retried_then_dead_lettered_then_resent` |
| 对方明确拒收 | 记为拒收，不重试 | `test_federation_messaging.py::test_a_refusing_peer_is_not_retried` |

### 5.3 API smoke（真实 HTTP 服务，全新迁移的库，联邦开启）

27 项检查全部通过，其中攻击类：agent 密钥或非 owner 不能开启联邦、不能批准连接、不能重发；没有连接时不能聊天；没有委派权限时委派被拒（`FED_E_SCOPE_DENIED`）；消息带凭证、超长被拒；第三个租户读不到别人的消息（404）；冻结后消息被对方拒收（`FED_E_GRANT_INACTIVE`）；提示注入不改变任何权限。脚本与逐项结果随交接一起保存在 Veynod 任务记录里。

## 6. 迁移演练

| 步骤 | 结果 |
|---|---|
| 全新空库执行全部迁移 | 2.0 秒，schema `3.85.0`，16 张联邦表，其中 10 张按工作区强制行级安全，其余 6 张全局身份表对应用角色撤销权限 |
| 同一库再执行一次 | 0.1 秒，无变化（幂等） |
| 全新库与长期增量升级的测试库对比联邦表结构（`pg_dump -s -t 'federation_*'`） | 完全一致（533 行） |

## 7. 观察到的问题与说明

- **本机 product 套件的 12 项失败都来自本机环境，不是联邦代码**。复跑方法：删掉本机前端构建产物，建一个全新测试库，只跑这 12 项，结果 24 项全部通过；CI 上同一代码全绿。原因分四类：
  - 8 项：库连接返回 503 `database_unavailable`。当时本机同时在跑 kernel 套件和前端构建，负载很高；`KeyError: 'needs_you'` 也是 503 之后的连带错误。
  - 2 项（根页面相关）：我为了验证前端构建，在本机生成了 `dashboard/out`，服务端看到它就把根页面链到控制台，测试期望没有构建产物。
  - 1 项（`test_budget.py::test_shadow_mode_records_but_never_blocks_bursts`）：断言一段时间窗内的请求计数（实测 8，期望 ≤ 7），高负载下请求被拖进同一窗口。
  - 1 项（`test_releases`）：复用了之前跑过的测试库，库里记录的“上次见过的工具列表”比新增的 `federation` 工具旧。全新库上通过。
  - 另外，`test_loop_steps.py::test_the_supervisor_walks_the_floor_only_when_something_waits` 在单独重跑时失败过一次，再跑通过；完整套件和 CI 上都通过。属于 Loop 监督者，与联邦无关，建议另开任务查时间依赖。
- **kernel 套件 `test_submission_guard.py::test_a_second_open_submission_is_refused`**：在本机同时跑 product 套件、负载很高时失败一次（409 的返回体是字符串而不是对象），单独重跑通过（3 秒），GitHub CI 上同一代码也通过。这条测试依赖“门控进程还在跑”的时间窗口，与联邦代码无关（本 PR 未改 kernel）。根因未查，建议另开任务把这条测试改成不依赖时间。
- 限流计数在单个应用进程内；多进程部署时每个进程各有一份额度。网络网关上线后边缘也要限流。
- 只在同一部署内（进程内传输）测试；两个独立部署之间的互联属于 P2.5（F-021）。

## 8. 复现方法

```bash
cd AgentGraph
TEST_DATABASE_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:5432/<db> \
  python -m pytest -q tests/test_federation_p1_gate.py tests/test_federation_messaging.py \
  tests/test_federation_api.py tests/test_federation_grants.py tests/test_federation_delegations.py \
  tests/test_federation_identity.py tests/test_federation_primitives.py
```

```bash
PG_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:5432 PYTHON=python scripts/test_all.sh
```
