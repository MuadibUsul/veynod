# 联邦 P5：交易 ICD（P5-1 Offer/Contract 与托管）

> 任务：`fedv2-p5-1-offer-escrow`（21/24）。对应愿景 §19.1–19.3、§8、§14.2。
> 维护：`agent:claude-2`。参数见 `OWNER_DECISIONS.md` N 节。

## 0. 只是模拟

**本阶段所有金额都是 SIM（演示积分），不是钱。** 不能用钱买，不能提现，不能转出本部署，不接任何支付通道。愿景 §19.2 要求真实结算之前先定下结算介质、托管主体、收费规则，并完成法律和合规评估；这些都还没定，所以只做模拟（任务描述也这样要求）。每个接口的返回都带 `simulated: true` 和说明。

- 每个工作区第一次用到钱包时，获得一次性 1000 SIM 演示额度（`federation_sim_allowance`）。
- 运营人员可以加发演示积分（`POST /v1/admin/federation/market/mint`，需写明原因）。

## 1. 三条价值流，共用一层

上层统一为 **Offer → Contract → Receipt**；履约状态按价值流分开（愿景 §19.1）：

| 价值流 | 履约状态 | 放款条件 |
|---|---|---|
| task（委派任务） | `FUNDED → DELIVERED → ACCEPTED → RELEASED`；`DISPUTED → RELEASED / REFUNDED`；`CANCELLED → REFUNDED` | 请求方验收结果 |
| skill | `FUNDED → INSTALLED → RELEASED`；`CANCELLED → REFUNDED` | 买方安装了那个版本 |
| tool | `FUNDED → CALLED → RELEASED`；`CANCELLED → REFUNDED` | 已定义，**暂不开放**（跨工作区工具调用还没开放，愿景 §4.1） |

- **Offer**：卖方的人报价。task 报价针对买方发来的某个委派（`subject` = job id，必须写明买方，委派还没交付）；skill 报价针对自己发布的某个版本（`subject` = `name@version`，可以不指定买方）。价格 1–1,000,000 SIM，有效期 1 小时到 30 天，条款（类别、标的、价格、单位、双方、费率）的哈希记为 `terms_hash`。
- **Contract**：买方的人接受报价时生成，**同一笔数据库事务里**把价款从买方转进这份合同的托管账户（`FUNDED`）。余额不够，返回 `FED_E_INSUFFICIENT_FUNDS`，什么都不动。
- **Receipt**：结算完成后的收据：付给谁、多少、对应哪一笔账。

## 2. 托管

- 托管账户 `escrow:<contract_id>`，独立于双方。钱在里面时谁都拿不走。
- task：委派交付 → `DELIVERED`；请求方验收 → `ACCEPTED`（排队放款）；请求方提出争议 → `DISPUTED`，钱留在托管里，等运营人员裁定 `release` 或 `refund`（`POST /v1/admin/federation/market/contracts/{id}/resolve`）。
- 交付之前，买方可以取消（`CANCELLED`，排队退款）；委派被拒、过期、取消、失败，也自动取消退款。
- 平台不收费（`FEE_PERCENT = 0`）：收费规则待定。

## 3. 结算与交易解耦

- 合同状态变化**只排队一条结算指令**（`federation_settlements`），每份合同最多一条（数据库唯一约束）。
- 结算步骤（后台循环 `market.advance`，也可单独 `settle_due`）把指令执行成一笔复式记账交易：托管 → 卖方（放款）或托管 → 买方（退款），然后把合同标为 `RELEASED` / `REFUNDED`。重复执行不会多付（指令状态 + 账本幂等键）。
- 账本（`federation_ledger_txns` / `federation_ledger_entries`）：每笔交易借贷相加为 0，由数据库在提交时检查；只能追加，不能改、不能删。账户：`ws:<工作区地址>`、`escrow:<合同>`、`mint`（演示积分来源，唯一允许为负）。`GET /v1/admin/federation/market/ledger/check` 检查全账本平衡、无负余额。

## 4. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/federation/market/wallet` | 本工作区 SIM 余额、托管中、流水 |
| `GET` | `/v1/federation/market/machines` | 三条价值流的状态机 |
| `POST` | `/v1/federation/market/offers` | 报价（人） |
| `GET` | `/v1/federation/market/offers` | 我发出的、发给我的、公开的 skill 报价 |
| `POST` | `/v1/federation/market/offers/{id}/accept` | 接受并托管（买方的人） |
| `POST` | `/v1/federation/market/offers/{id}/withdraw` | 撤回未成交的报价（卖方的人） |
| `GET` | `/v1/federation/market/contracts`、`/contracts/{id}` | 合同、托管额、收据、历史 |
| `POST` | `/v1/federation/market/contracts/{id}/cancel` | 交付前取消（买方的人） |
| `POST` | `/v1/admin/federation/market/mint` | 运营：加发演示积分 |
| `POST` | `/v1/admin/federation/market/contracts/{id}/resolve` | 运营：裁定争议 |
| `GET` | `/v1/admin/federation/market/ledger/check` | 运营：账本检查 |

错误码：`FED_E_OFFER_UNKNOWN`（404）、`FED_E_OFFER_STATE`（409）、`FED_E_CONTRACT_UNKNOWN`（404）、`FED_E_CONTRACT_STATE`（409）、`FED_E_INSUFFICIENT_FUNDS`（409）、`FED_E_NOT_AVAILABLE`（422）。迁移 3.98。
