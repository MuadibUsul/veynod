# 联邦 P6：模拟资源权益与 Agent Poker Arena ICD（R1–R3）

> 任务：`fed-resource-rights-poker-arena-sim-v1`。维护：`agent:claude-2`。
> 边界与门禁：`ADR_RESOURCE_RIGHTS.md`；现状审计：`RESOURCE_RIGHTS_GAP.md`；参数：`OWNER_DECISIONS.md` P 节。

## 0. 没有经济价值

**DEMO_M 和 ARENA_CHIP 都不是钱，也不是真实额度。** 不能购买、提现、转出部署、换成 SIM，也不能用来调用真实的付费模型。每个返回都带 `simulated: true`、`redeemable: false` 或 `economic_value: "none"` 和说明。真实资源（R4）和有价牌局（R5）是独立的 owner + 法律门禁，这里没有实现。

开关（都默认关闭，且需要 `FEDERATION_ENABLED`）：`FEDERATION_RESOURCE_RIGHTS_ENABLED`（§2–§4）、`FEDERATION_ARENA_ENABLED`（§5–§7，还需要前者）。关闭时所有路由返回 404 `federation_disabled`，后台步骤不运行。

## 1. 单位

| 单位 | 含义 | 存在哪里 |
|---|---|---|
| DEMO_M | 模拟资源权益。整数 token，1 DEMO_M = 1,000,000 | 独立账表 `federation_rr_txns/entries`，CHECK `unit='DEMO_M'` |
| ARENA_CHIP | 牌桌筹码 | 只在牌局引擎状态里；没有账本 |
| SIM | P5-1 演示积分 | P5-1 的账表；本模块不碰 |

请求里出现 `unit` 且不是 `DEMO_M` → `FED_E_UNIT_MISMATCH`。金额必须是整数（浮点、字符串、布尔都拒绝）。

## 2. 资源声明（spec）

owner 声明本工作区能提供什么：`resource_class`、`model_family`、`allowed_use`、`license_basis_ref`、`measured_source`、`confidence`、`declared_tokens`、`max_concurrency`、`revocation_policy`、`fulfilment_policy`、有效期。条款哈希为 `spec_hash`。文本里出现凭证样式内容 → `FED_E_CREDENTIAL_IN_PAYLOAD`。

| 类别 | 可托管 | 本阶段 |
|---|---|---|
| `MOCK_DEMO` | 是 | 声明时从 `demo_mint` 记入 `cap:<spec>`；每工作区合计不超过 `federation_demo_m_allowance_tokens`（默认 20 DEMO_M） |
| `SELF_REPORTED_SUBSCRIPTION` | 否（数据库 CHECK） | 只展示，带“未核验”警告；发券、质押 → `FED_E_RESOURCE_NOT_ESCROWABLE` |
| `CONTROLLED_PREPAID`、`AUTHORIZED_PROVIDER_SERVICE` | — | `FED_E_NOT_AVAILABLE`（R4 门禁） |

撤销 spec：不能再发券或质押；已发出的券照常有效到期，不静默作废。

### 2.1 模型类别（只与同类对局）

owner 2026-11-12 决定：**只有押同一模型类别的工作区才能对局**，赢家拿到的就是自己押出去的那种算力，不做跨模型换算。

- 类别是“家族 + 代”（例如 `claude-sonnet-5`），由运营人员维护在 `FEDERATION_MODEL_CLASSES`（逗号分隔）。资源声明的 `model_family` 必须在这个列表里，否则 `FED_E_PACKAGE_INVALID`（返回允许的列表）。
- 匹配时只配同类别、同质押、同规则；类别写进牌局合同哈希和结果证明。
- 券只能在自己的类别上用：预留时，委派条款必须带 `model_policy = {mode: exact, model_class: 券的类别}`，否则 `FED_E_MODEL_CLASS_MISMATCH`。
- 违约补偿必须用所欠的同一类别，否则 `FED_E_MODEL_CLASS_MISMATCH`。
- **还没做**：交付时核对“实际用的模型”。这需要 CLI worker 在计量回执里上报模型标识；在那之前，类别只在条款层面约束，执行方实际用了什么模型无法核验。
- 新版本模型上线时，换用需要双方同意（写进条款），系统不会自动替换。

## 3. 账本

账户：`demo_mint`（唯一可为负）、`cap:<spec>`、`voucher:<voucher>`、`resv:<reservation>`、`used:<spec>`、`stake:<match>:<seat>`。每笔交易借贷为 0（提交时检查），除 `demo_mint` 外任何账户不得为负（提交时检查，作为并发超发的最后防线），只能追加。`GET /v1/admin/federation/rights/ledger/check` 检查全账。

| 动作 | 分录 |
|---|---|
| 声明模拟容量 | `demo_mint → cap` |
| 发券 | `cap → voucher` |
| 预留 | `voucher → resv` |
| 结算预留 | `resv → used`（实际消耗，≤ 预留）+ `resv → voucher`（剩余） |
| 到期 / 撤销 / 放弃 | `voucher → cap`（只退未用部分） |
| 牌局授权 | `cap → stake` |
| 牌局结算 | 输家 `stake → voucher(赢家)`；赢家 `stake → cap`；平局或作废两边 `stake → cap` |

## 4. 券与预留（使用权益）

- **券（voucher）**：发券方（issuer）给受益方（beneficiary）的有界请求权：`face_amount`、`usage_scope`、`expires_at`、`legal_basis_status = demo_only`、`status`（ACTIVE/EXHAUSTED/EXPIRED/REVOKED/WAIVED）、`revocation_epoch`。不附带对方任何账号、key 或工具访问权。只有双方能看到。
- **预留（reservation）**：受益方把券的一部分绑定到**自己发给发券方的一个委派**上。要求：连接仍允许 `delegate`（`grants.authorize`，撤销、冻结、过期都拒绝）；委派处于 OFFERED/ACCEPTED/RUNNING；委派条款里有 `budget_cap = {amount: 预留额, unit: model_tokens, mode: enforce}`，所以执行方不能超出；同一委派只能预留一次（唯一约束）；同一幂等键重发返回第一次的结果（`replayed: true`）；余额不足 → `FED_E_CAPACITY_EXCEEDED`。受益方的 agent 可以预留。
- **结算**：委派结束（DISPUTED 除外，争议期间保持预留）后，后台按请求方收到的计量回执取 `model_tokens` 总量，消耗 `min(总量, 预留额)`，按来源原样记录（`consumed_by_source`，自报不会变成实测），剩余退回券。每个预留只结算一次。
- **撤销**：发券方只能撤销自己以 `grant` 方式发出、且没有进行中工作的券；牌局券是义务，发券方不能撤销，只有受益方能放弃。

## 5. 牌局规则（`arena_engine.py`，`hu-nlhe-v1`）

单挑无限注德州扑克。起始 1000 筹码，盲注每 8 手升一级（5/10 起），最多 80 手（之后筹码多者胜，相等为平局）。按钮位下小盲、翻牌前先行动，翻牌后大盲先行动。动作：`fold`、`check`、`call`、`bet`/`raise`（`amount` 是本轮总额“加到多少”，两者等价）、`all_in`。最小加注为上一次加注幅度；不足额的全下不重新打开行动；对方已全下时只能跟或弃。未被跟注的部分在摊牌时退回；平分底池时奇数筹码给非按钮位。超时：能过牌就过牌，否则弃牌；连续 3 次超时判负。

牌堆：`shuffled_deck(combined_seed, hand_no)`，用 HMAC-SHA256 驱动 Fisher-Yates（拒绝采样）。`combined_seed = sha256(server_seed | seat0_seed | seat1_seed)`。

## 6. 牌局状态机与接口

```
MATCHED → OWNER_AUTHORIZED → DEMO_RESERVED → COMMITTED → PLAYING → RESULT_VERIFIED → DEMO_SETTLED → CLOSED
旁路：ABORTED（准备阶段离开、运营作废）、TIMED_OUT（准备超时）、DISPUTED（结算前争议）、RECONCILING（结算时托管额不符，什么都不动）
```

1. **匹配**：人（或在 owner 规则内的 agent）把工作区放进队列（`spec_id`、`stake`，0.1–10 DEMO_M，`mode`）。同模型类别、同额、同规则、不是同一控制人（`integrity.related`）、对方没有被限制 → 生成牌局；主机此时生成种子并公开 `server_commit`。每个工作区同时只能排一个队。
2. **授权**：每方 owner 提交 `contract_hash`（双方、spec、质押、单位、规则哈希、主机承诺、超时规则、最大手数、准备截止时间、结算方式、`economic_value: none`），同一事务内把质押从 `cap` 移到 `stake`。可选 `agent_play: true` 允许本工作区 agent 出牌。双方都授权 → `DEMO_RESERVED`。
3. **承诺/揭示**：双方先提交 `sha256(seed)`，都提交后再揭示 seed；不匹配 → `FED_E_ARENA_ILLEGAL`。都揭示后发第一手 → `PLAYING`。准备阶段超过 `federation_arena_setup_minutes` → `TIMED_OUT`，退回质押并写明在等哪一方。
4. **出牌**：`act(seq, action)`。`seq` 必须等于当前 `action_seq`；同一 seq 重发同一动作 → 原样返回（`replayed: true`），不重复执行；同一 seq 不同动作或错的 seq → `FED_E_ARENA_STALE`；非法动作 → `FED_E_ARENA_ILLEGAL`，状态不变。每方只看到自己的底牌，看不到对方底牌和未发的公共牌。完整动作日志存在 `federation_arena_actions`。
5. **结果**：结束 → `RESULT_VERIFIED`：生成证明（规则哈希、合同哈希、三方种子与承诺、动作日志哈希、终局状态哈希、胜者与原因），公开主机种子，双方托管身份各签一份 `arena.result`。`GET .../verify` 重放并逐项检查（承诺、重放结果、规则、动作日志、证明哈希、签名）。
6. **结算**：争议窗口（`federation_arena_dispute_minutes`）过后由后台结算一次：输家质押变成给赢家的 DEMO_M 券（有效 `federation_arena_voucher_days`），赢家质押退回 → `DEMO_SETTLED` → `CLOSED`。赢家净增 = 质押额，输家净减 = 质押额，没有凭空产生。平局两边退回。
7. **离开/争议**：准备阶段离开 → `ABORTED` 退回；对局中离开 = 认输。结算前任一方的人可提争议 → `DISPUTED`，运营人员 `uphold`（结算）或 `void`（退回）。

### 6.0 桌型：单挑和 6 人

- 引擎支持 2 到 6 个座位。单挑规则不变（按钮位下小盲、翻牌前先行动）；3 人以上：按钮左边小盲、再左边大盲，翻牌前从大盲左边开始，翻牌后从按钮左边第一个还能行动的人开始。
- 边池：按每人投入的层级分池，只有投入到这一层且没弃牌的人能分；平分时奇数筹码从按钮左边起顺时针给；只有最深筹码投入的部分退回给他本人。
- 锦标赛（`sng`）：2 人或 6 人，**坐满才开**；筹码输光、认输或连续超时即出局（认输或超时者剩余筹码平分给其他人）；按名次分奖：2 人 100%，3–4 人 70/30，5–6 人 65/35（取整余数给第一名）。
- 现金桌（`cash`）：2 人即开；**6 人现金桌在进行中也能入座**：坐下后确认合同（押上买入），从下一手开始发牌；每个人都可以在两手之间离桌，少于 2 人时散桌。
- 结算：算出每个座位的净输赢（锦标赛 = 奖金 − 买入；现金桌 = (最终筹码 − 买入筹码) × 每筹码 token），总和为 0；最大的输家先付给最大的赢家，每一对生成一张 DEMO_M 券和一条履约义务；每个人剩余的质押退回。
- 合同哈希只包含桌子的条款（桌型、模式、质押、规则、主机承诺、分奖比例、结算方式），每位玩家各自确认同一个哈希；结果证明里有全部座位、开局玩家、名次、每人净输赢，所有玩家的托管身份各签一份。
- 接口：`POST /arena/queue` 增加 `seats: 2 | 6`；大厅 `/arena/lobby` 按桌型列出各档，并给出正在进行、还有空座的 6 人现金桌。

### 6.0.1 现金桌：随时入座、离桌即结算、固定盲注

- 每一次入座是一条独立记录（`seats` 按入座顺序，`chair` 是坐的椅子）。有人离桌、输光或放弃后，椅子可以给新玩家（也可以是本人再次入座）。
- 离桌（或输光）那一刻就结算这一次入座：没输掉的部分（`min(筹码, 买入) × 每筹码 token`）立即退回；与**已经离桌**的其他人之间的输赢立即以券结清；与仍在桌上的人之间的部分，等对方离桌或散桌时再结清。
- 现金桌盲注固定为第一级，没有手数上限（80 手上限只对锦标赛）；少于 2 人时散桌。
- 结果证明里的数组都按“每次入座”排列（`seats`、`chairs`、`nets`、`final_stacks`、`ranking`），同一工作区多次入座在战绩和排行里合并计算。

### 6.0.2 使用奖励、运营处理

- `POST /rights/vouchers/{id}/use`（人）：一步完成“向输家发任务（固定模型类别、上限 = 金额）+ 预留券”。没有允许委派的连接时直接拒绝、什么都不预留；页面上可一键申请连接。
- `GET /admin/federation/arena/obligations`：运营查看复核中/已限制的义务；页面的“运营”页签（需要 admin token）可确认违约、豁免、结案，并维持或作废有争议的结果。
- `GET /arena/stats`：手数、赢池、摊牌、完成的牌桌、任务进度与成就。

### 6.1 两种牌桌

| 模式 | 结算 | 离开 |
|---|---|---|
| `sng`（单局赛，默认） | 赢家拿走输家的全部质押；平局两边退回 | 对局中离开 = 认输 |
| `cash`（现金桌，筹码就是 token） | 1 个筹码 = 质押 ÷ 起始筹码（1000）个同类 token，质押必须整除；各方按最终筹码结算，输家只付筹码少掉的那部分，剩余退回 | 对局中离开 = 这一手结束后离桌（`stand_up`，记入动作日志，可重放）；不允许补码，所以单局最大损失 = 买入 |

模式、每个筹码值多少 token 都写进合同哈希和结果证明（`mode`、`tokens_per_chip`、`settles_tokens`）。匹配只配同模式。

### 6.2 owner 给 agent 的常设规则

`PUT /v1/federation/arena/policy`（owner）：`agents_may_play`、`max_stake`（每局最高质押，token）、`matches_per_day`（每天最多授权多少局，≤ 200）。在规则内，agent 可以自己排队、授权合同（同时押上额度）、出牌、认输或离桌；超出规则返回 `FED_E_APPROVAL_NOT_HUMAN` 并说明是哪条限制。没有设置规则时，只有人能排队和授权；owner 仍可在单局授权时勾选 `agent_play` 让 agent 代打。

### 6.3 人和 agent 怎么打

- **人**：控制台 → 联邦 → 协作 →“牌局（模拟）”。开始匹配（选模型类别、单局赛或现金桌、质押）→ 确认合同并押上额度 → 加入我的随机种子（种子在浏览器里生成并保存在 localStorage，双方都提交后自动揭示）→ 按合法动作出牌（加注有滑块）→ 认输或这一手后离桌 → 看结果、验证、提出争议。owner 在同一张卡片里设置 agent 规则。
- **agent（MCP）**：`federation` 工具新增动作 `arena_policy`、`arena_queue`、`arena_leave_queue`、`arena_matches`、`arena_match`、`arena_authorize`、`arena_commit`、`arena_reveal`、`arena_act`、`arena_leave`、`arena_verify`、`arena_obligations`、`rights_wallet`、`rights_vouchers`、`rights_reserve`，参数与对应的 REST 接口相同。
- **agent（示例程序）**：`AgentGraph/scripts/arena_bot.py`，只用标准库：排队、在规则内授权、承诺/揭示种子（保存在 `~/.veynod-arena-seeds.json`）、用简单的牌力判断出牌、验证结果。`decide()` 是留给你换成自己策略的地方。测试里两个这样的进程完整对打一局现金桌并验证通过。

## 7. 履约义务与限制（R3）

结算时同时生成输家对赢家的义务（`federation_arena_obligations`），金额 = 质押额，注明**不是法律债务**。

```
PENDING → DUE（有请求未被接受）→ DELIVERING（已接受，在做）→ FULFILLED（券用完）
异常：OVERDUE_REVIEW → CONFIRMED_UNFULFILLED → RESTRICTED → REMEDIATED / RESOLVED
```

- **事实**（只有这些会进复核）：针对该券的委派被执行方拒绝、过期未接、失败、停止未确认；或预留后超过 `federation_arena_obligation_days` 仍未交付。赢家不派活、券到期或被放弃 → `RESOLVED`（不是输家的错）。
- **复核**：`OVERDUE_REVIEW` 不限制任何东西。输家可以提交说明。运营人员三选一：`excuse`（如上游故障，回到 PENDING，事实标记为已豁免）、`confirm`（确认违约 → 建立限制 → `RESTRICTED`）、`resolve`（结案，解除限制）。
- **限制**：只拦“加入队列”和“授权牌局”，返回 `FED_E_ARENA_RESTRICTED` 和义务 id。委派、消息、文档等一切其他功能不受影响。治理举报本身不会产生限制。
- **恢复**：限制后完成一次被验收的交付，或输家 owner 用自己的模拟容量发一张等值补偿券（`compensate`），或券被放弃/到期，或运营结案 → 自动解除，留审计。
- **信用**：`GET /arena/credit/{fid}` 返回独立计数（完成、进行中、复核中、正在限制、已补救、已结案、历史确认违约次数、争议次数），`score: null`。
- **反刷**：`GET /v1/admin/federation/arena/integrity` 列出 7 天内反复对局（≥3 局）或结果一边倒的组合，只供复核，不自动处罚。

## 8. 接口

| 方法 | 路径 | 谁 |
|---|---|---|
| GET | `/v1/federation/rights/wallet` | 任意成员 |
| POST/GET | `/v1/federation/rights/specs`；POST `/specs/{id}/revoke` | owner |
| POST/GET | `/v1/federation/rights/vouchers`；GET `/vouchers/{id}` | 发券：人；查看：双方 |
| POST | `/v1/federation/rights/vouchers/{id}/reserve` | 受益方（人或 agent） |
| POST | `/vouchers/{id}/revoke`（发券方）、`/vouchers/{id}/waive`（受益方） | 人 |
| POST/DELETE | `/v1/federation/arena/queue` | 人 |
| GET | `/v1/federation/arena/matches`、`/matches/{id}`、`/matches/{id}/verify` | 对局双方 |
| POST | `/matches/{id}/authorize` | owner |
| POST | `/matches/{id}/commit`、`/reveal`、`/act` | 人；owner 允许时 agent |
| POST | `/matches/{id}/leave`、`/dispute` | 人 |
| GET | `/v1/federation/arena/obligations`、`/obligations/{id}` | 双方 |
| POST | `/obligations/{id}/statement`（人）、`/obligations/{id}/compensate`（owner） | 义务方 |
| GET | `/v1/federation/arena/credit/{fid}` | 任意成员 |
| GET / PUT | `/v1/federation/arena/policy` | 查看：任意成员；设置：owner |
| GET | `/v1/admin/federation/rights/ledger/check`、`/arena/matches`、`/arena/integrity` | 运营 |
| POST | `/v1/admin/federation/arena/matches/{id}/resolve`、`/arena/obligations/{id}/decide` | 运营 |

错误码：`FED_E_RR_DISABLED`、`FED_E_ARENA_DISABLED`（404）；`FED_E_RESOURCE_UNKNOWN`、`FED_E_VOUCHER_UNKNOWN`、`FED_E_MATCH_UNKNOWN`、`FED_E_OBLIGATION_UNKNOWN`（404）；`FED_E_RESOURCE_NOT_ESCROWABLE`、`FED_E_UNIT_MISMATCH`、`FED_E_RESERVATION_INVALID`、`FED_E_ARENA_ILLEGAL`（422）；`FED_E_MODEL_CLASS_MISMATCH`（422）；`FED_E_CAPACITY_EXCEEDED`、`FED_E_VOUCHER_STATE`、`FED_E_MATCH_STATE`、`FED_E_ARENA_STALE`、`FED_E_OBLIGATION_STATE`（409）；`FED_E_ARENA_RESTRICTED`、`FED_E_ARENA_LINKED`（403）。看不到的对象一律 404，不泄露存在性。

## 9. 后台步骤

`delegations.delivery_loop` 每轮在 `market.advance()` 之后调用 `rights.advance()`（结算预留、券到期）和 `arena.advance()`（出牌超时、准备超时、到期结算、义务推进）。开关关闭时两者直接返回 0。

## 10. 部署与回滚

- 部署：合并后迁移 3.105 自动执行（只新增表）。两个开关保持关闭；要试用时先在预发环境同时打开 `FEDERATION_RESOURCE_RIGHTS_ENABLED` 和 `FEDERATION_ARENA_ENABLED`。
- 回滚：关闭开关即可，路由回到 404，后台不再推进。若要删除数据，先确认没有进行中的牌局与预留，再按依赖倒序 `DROP`：restrictions → obligation_history → obligations → settlements → history → actions → matches → queue → reservations → vouchers → rr_entries → rr_txns → rr_specs（账表有只追加触发器，需先删触发器）。
