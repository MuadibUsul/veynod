# 联邦 P4：声誉 ICD（P4-1 履约证明与多维指标，P4-2 目录与可解释排序）

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

## 8. 目录与可解释排序（P4-2，迁移 3.95）

### 8.1 上目录

工作区默认**不在目录里**。由工作区的人 `PUT /v1/federation/directory/listing {listed, headline, capabilities, availability}` 主动上架；`availability` 为 `open` / `busy` / `closed`。随时可下架。目录条目除了这几项自述，其余都是从履约证明、平台实测和已发布 Skill 算出来的。

### 8.2 搜索

`GET /v1/federation/directory`，参数：`q`、`capability`、`min_acceptance`（0–1，证据不足的不算）、`max_response_seconds`、`price`（`paid` 返回空）、`availability`、`sort`（`rank` 默认 / `evidence` / `availability` / `relevance`）、`include_closed`、`limit`、`offset`。

### 8.3 排序公式（公开、每条结果自带解释）

```
score = safety × (0.45 × relevance + 0.35 × evidence + 0.20 × availability)
```

| 因素 | 怎么算 |
|---|---|
| relevance | 指定能力：自己列了 1.0，发布的 Skill 有 0.8，都没有则不出现；关键词：命中词数 / 查询词数；无查询 1.0 |
| evidence | 0.6×验收率 + 0.4×交付率（按 P4-1 权重），再向中性值 0.5 收拢：可信度 = min(1, 证明权重/10)。没有证明的新工作区 = 0.5，不惩罚 |
| availability | 0.6×状态（open 1.0；open 但 14 天内没回应过报价 0.7；busy 0.3）+ 0.4×速度（≤1 小时 1.0，≤1 天 0.7，更慢 0.4，未知 0.5） |
| safety | 有生效中的处罚：直接不出现；有过处罚：×0.8；身份被冻结：不出现 |
| promotion | 0。付费推广（目前没有）只放在单独的 `promoted` 列表，不改变自然排序 |

每条结果的 `rank.explanation` 列出每个因素的值、权重、贡献和原因，`rank.score` 等于贡献之和；`rank.position` 是名次。返回里还有 `formula`（权重）和 `excluded`（因处罚、冻结被排除的数量）。

`GET /v1/federation/directory/{fid}`：单个已上架工作区的条目（自己可看未上架的自己）。

MCP：`federation` 工具的 `directory` 动作。

## 9. 不泄露私密工作（P4-2 A-03，F-026）

- 目录条目、搜索结果只含：自述、汇总比例与计数、合作对象**数量**、已发布 Skill。不含任务内容、聊天内容、任务 id，也**不含合作对象是谁**。
- 履约证明里写着双方地址，所以 `GET /reputation/{fid}/proofs` 只给当事方看：执行方本人看全部，请求方只看自己签的那些，其他人看到空列表。
- `GET /reputation/{fid}`：未上架的工作区，只有它自己和与它有连接的工作区能看；陌生人得到 404（`FED_E_NOT_LISTED`）。

新增错误码 `FED_E_NOT_LISTED`（404）。表 `federation_directory_listings`（按工作区行级安全）。
