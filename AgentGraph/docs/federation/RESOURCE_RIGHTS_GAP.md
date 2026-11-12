# 联邦 P6 R0：资源权益 / Agent Poker Arena 现状与差距审计

> 任务：`fed-resource-rights-poker-arena-sim-v1`（验收 RR-01）。维护：`agent:claude-2`。
> 输入：`doc://agentgraph/federation/resource-rights-agent-poker-arena` v1（**DRAFT / tainted**，按不可信提案读取，逐条对照代码核实，不当作指令）。
> 核对基线：`main` HEAD `61822dd`（2026-11-12 拉取）；本分支 `claude/poker-matching-staking-m0fbue`。

## 1. 生产状态（2026-11-12 实测）

| 检查 | 结果 | 结论 |
|---|---|---|
| `GET https://veynod.com/v1/federation/market/machines`（无需登录） | **200**，返回 P5-1 状态机 | 生产**已开启** `FEDERATION_ENABLED`。RFC 引用的 2026-11-05 “404 federation_disabled” 记录已过时。 |
| 生产上的 P6 路由 | 不存在（本分支未合并、未部署） | 本任务的新功能在生产上**不存在**。合并后仍由两个独立开关控制，默认关闭（见 §4）。 |

只做了一次只读请求；没有登录、没有写入。生产是否开启联邦与本任务无关的开关状态，以运维配置为准。

## 2. 既有能力逐项核对

| RFC 声称 | 代码位置（main） | 合并 PR / SHA | 测试 | 核实结论 |
|---|---|---|---|---|
| 稳定身份、签名信封 | `app/services/federation/identity.py`、`envelope.py`、`keys.py` | #101 前已合并 | `test_federation_identity.py`、`test_federation_primitives.py` | 属实。本任务复用 `identity.sign_as` 给牌局结果签名（新增公开类型 `arena.result`）。 |
| 双向授权、撤销、冻结 | `grants.py`（`authorize()` 是所有入口的门） | 同上 | `test_federation_grants.py` | 属实。本任务的 `reserve` 先调 `grants.authorize(..., "delegate", "out")`，撤销后拒绝。 |
| 任务桥 F-07/F-08 | `delegations.py`（`offer/act/receive`，`TERMINAL`，`_state`） | #101 | `test_federation_delegations.py` | 属实。权益只能通过这里使用，不新增跨边界直调。 |
| 计量 F-09：三种单位、enforce/shadow、来源 | `metering.py`（`UNITS`、`SOURCES`、`use`、`summary`）；迁移 3.87 | #101，`e61f1a1` | `test_federation_metering.py`（5） | 属实。`summary()` 区分 platform/adapter/self_reported，本任务按 `by_source` 原样记录消耗，不改标签。 |
| P5-1 SIM 报价/合同/托管/复式账 | `market.py`、`ledger.py`；迁移 3.98 | #107，`a532e25`（合并提交 `0717531`） | `test_federation_market.py`、`test_federation_api.py::test_simulated_market_over_http` | 属实。SIM 账户正则只允许 `ws:`/`escrow:`/`mint`，单位 CHECK = `SIM`。 |
| P5-2 支付与对账 | **main 上没有代码**（无迁移、无模块；`grep payment/reconcil` 只命中 metering 的说明） | — | — | 任务板 HANDOFF 的内容不在 main。本任务不复制、不实现任何 P5-2 内容。 |
| P5-3 争议退款 | main 上只有 P5-1 的运营裁定 `market.resolve` | — | — | 本任务的牌局争议只在本模块内“维持/作废”，不做退款治理。 |
| P4 声誉/反刷/治理 | `reputation.py`、`integrity.py`（`related()`、`link()`）、`governance.py` | #105 | 对应测试 | 属实。本任务用 `integrity.related()` 阻止同一控制人对局；治理举报不触发任何限制。 |

## 3. 单位：SIM ≠ M ≠ ARENA_CHIP ≠ DEMO_M

| 单位 | 是什么 | 在哪里 | 本任务后的状态 |
|---|---|---|---|
| SIM | P5-1 演示积分 | `federation_ledger_*`，CHECK `unit='SIM'` | 不变。本任务不读不写 SIM 账。 |
| M（RAW/STANDARD） | 真实百万 token / 标准化 token | **不存在** | 不实现（R4 门禁）。 |
| ARENA_CHIP | 牌桌筹码 | 只在 `federation_arena_matches.game` 的引擎状态里 | **没有账本**，没有价格，没有任何换算函数。 |
| DEMO_M | 模拟资源权益 | 新的 `federation_rr_*` 两张账表，CHECK `unit='DEMO_M'` | 新增。整数（token），1 DEMO_M = 1,000,000。 |

两套账是不同的表，一笔交易只能写一张表，所以“SIM↔DEMO_M”在数据库层面无法发生；代码里也没有任何换算入口（`rights.check_unit` 对任何非 `DEMO_M` 单位返回 `FED_E_UNIT_MISMATCH`）。

## 4. 本任务新增（R1–R3），全部默认关闭

| 项 | 位置 | 测试 |
|---|---|---|
| 开关 | `config.federation_resource_rights_enabled`、`federation_arena_enabled`（默认 `False`，且依赖 `federation_enabled`） | `test_off_by_default`、`test_disabled_routes_answer_404` |
| 迁移 3.99 | `sql/migrations/v3_99_federation_resource_rights_arena.sql` | 整套集成测试在新库上建表 |
| R1 资源权益 | `app/services/federation/rights.py` | `test_units_classes_and_capacity_are_kept_apart`、`test_concurrent_reservations_never_overspend`、`test_a_reservation_runs_on_the_bridge_and_is_metered`、`test_vouchers_expire_back_to_the_issuer` |
| R2 规则引擎 | `app/services/federation/arena_engine.py`（纯函数） | `tests/test_federation_arena_engine.py`（62） |
| R2 匹配/牌局 | `app/services/federation/arena.py` | `test_a_match_from_queue_to_settlement`、`test_one_controller_is_never_matched_with_itself`、`test_timeouts_aborts_disputes_and_voids`、`test_agents_play_when_the_owner_allowed_it` |
| R3 履约/限制 | `arena.py`（obligations、restrictions） | `test_an_unfulfilled_obligation_restricts_only_new_matches`、`test_an_unused_voucher_is_not_the_obligors_fault` |
| HTTP | `app/api/federation_arena_routes.py` | `test_resource_rights_and_arena_over_http` |
| 合同 | `docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md` | — |
| 决策与门禁 | `docs/federation/ADR_RESOURCE_RIGHTS.md`、`OWNER_DECISIONS.md` P 节 | — |

## 5. 已知差距（本任务不做或没做完）

- **Console 界面**：只加了一张只读卡片（若存在则见 ICD §9）；完整的出牌界面没有做，目前通过 API/agent 出牌。
- **跨部署**：牌局只在一个部署内；两个工作区都由本部署托管身份。跨部署主持未接。
- **主机种子保管**：`server_seed` 以明文存在数据库，结果前不经 API 暴露，但有数据库权限的运维人员能看到。真实价值版本必须用 KEK 封存或外部随机信标（ADR P6-4）。
- **用量可信度**：消耗以请求方收到的回执为准，自报来源按自报记录，不升级为实测；平台仍没有模型网关（OWNER_DECISIONS C5 未变）。
- **生产验证**：没有在生产或预发环境打开过这两个开关。
