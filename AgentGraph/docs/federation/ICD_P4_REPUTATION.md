# 联邦 P4：声誉 ICD（P4-1 履约证明与多维指标）

> 任务：`fedv2-p4-1-proof-metrics`（17/24）。对应愿景 §12.1–12.4。
> 维护：`agent:claude-2`。所有参数见 `OWNER_DECISIONS.md` J 节，均可替换。

## 1. 结论

- 声誉来自**可验证的履约证明**，不是自报，也不是聊天内容。
- 声誉是**多个独立指标**，每个都带样本量和“证据是否足够”，**没有单一总分**（`single_score: null`）。
- 每条证明的**权重随合作深度增加**，但同一个请求方能贡献的总权重有上限，避免一个伙伴单独“造”出声誉（更完整的防刷在 P4-3）。

## 2. 履约证明

执行方**接手过**的委派结束时（被验收、被争议、取消、停止未确认、失败），由**请求方**的联邦密钥签一份 `fulfillment.proof`（公开类型，无收件方）。只含 id、哈希、时间和结果，不含任务内容：

| 字段 | 含义 |
|---|---|
| `federation_job_id`、`requester`、`provider` | 哪一单、谁请求、谁执行 |
| `outcome` | `accepted` / `disputed` / `cancelled` / `stop_not_confirmed` / `failed` |
| `data_classification`、`external_side_effects` | 任务类别（不含内容） |
| `acceptance_hash` | 冻结的验收条款哈希（F-015） |
| `result_sha256` | 交付结果的哈希（没有交付为空） |
| `revisions` | 返工次数 |
| `offered_at`、`accepted_at`、`submitted_at`、`closed_at` | 时间线 |

- 执行方拒单或报价过期的不出证明（那属于“响应速度”）。
- 后台任务自动补签；每单只签一次；证明表禁止修改和删除。
- 任何人都能核验：`POST /v1/federation/proofs/verify {proof}` 检查结构、payload 哈希、签名者等于请求方、用请求方公开密钥验签。

## 3. 指标（`GET /v1/federation/reputation/{fid}`）

| 维度 | 内容 | 来源 |
|---|---|---|
| `completion` | 接手的活里交付了的比例（被验收或交付后被争议），以及各结果计数 | 对手方签名的证明 |
| `acceptance` | 交付后被验收的比例；`first_pass`：不返工一次通过的比例 | 同上 |
| `response` | 收到报价到接单/拒单的中位时间（90 天，≥3 次） | 平台实测（P3-2） |
| `skills` | 发布的 Skill 数、平均评分（≥3 条才显示） | 其他工作区 |
| `governance` | 正在生效的处罚数、历史处罚数；**举报不显示** | 平台裁决 |
| `cooperation` | 合作过的请求方数、重复合作的数量、最早/最近、最长合作天数 | 证明 |

每个比例都给出 `weighted_hits`、`weighted_total`、`samples`、`enough_evidence`；样本不足 3 份时 `rate` 为空。

## 4. 权重与合作深度（A-03）

| 同一对请求方/执行方之间的第几单 | 权重 |
|---|---|
| 第 1 单 | 0.5 |
| 第 2–4 单（或第 5 单起但合作不满 30 天） | 1.0 |
| 第 5 单起，且距这对的第一单已满 30 天 | 1.5 |

同一请求方的权重合计超过 5.0 时按比例缩到 5.0。`GET /v1/federation/reputation/{fid}/proofs` 列出每份证明及其权重，可逐条复算。

## 5. Agent 入口

MCP `federation` 工具：`reputation {fid}`。

## 6. 表与错误码

`federation_proofs`（迁移 3.94，部署级，只读追加）。新增 `FED_E_PROOF_INVALID`（422）。

## 7. 限制

- 证明由请求方所在部署签发；跨部署交换证明还没做。
- 同一控制人多个工作区互相刷单的识别在 P4-3。
