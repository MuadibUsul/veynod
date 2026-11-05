# 联邦 P4：声誉 ICD（P4-1 履约证明与多维指标，P4-2 目录与可解释排序，P4-3 反刷与女巫防护）

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

## 10. 反刷与女巫防护（P4-3，迁移 3.96）

每份履约证明在深度权重之外再乘一个**可信系数**：

| 情况 | 系数 | `counted_as` |
|---|---|---|
| 请求方和执行方是同一控制人：同一主体（同一组织下的工作区），或被运营人员关联，或被判定串谋 | 0 | `same_controller` |
| 这一对看起来在刷：双方互相委派各 ≥2 单（`two_way_flow`），或 ≥3 单从报价到结束不到 10 分钟（`implausibly_fast`），且运营人员还没确认为真实 | 0 | `pending_verification` |
| 请求方签证明时身份不满 7 天 | 0.25 | `fresh_requester` |
| 其他 | 1 | `counted` |

- **冷启动**：执行方身份满 14 天前，各项指标照常显示，但目录排序里的“过往表现”固定为中性 0.5，早期刷量换不来名次。
- **批量造号**：同一主体在 24 小时内建了 ≥5 个工作区，标记 `created_in_a_batch`。
- **集中度**：单一请求方占 ≥60%（≥5 份）时标记 `concentrated_on_one_requester`（仅提示；P4-1 的单伙伴上限已在起作用）。
- **只降权、不处罚**：被标记的证明暂不计入，等运营人员看过；标记本身不会触发任何处罚（愿景 §12.3）。
- 声誉返回新增 `integrity`：`flags`、`cold_start`、`cold_start_ends`、`excluded`（同一控制人 / 待核验的份数）、`discounted`（新地址的份数）。目录条目同样附带，“过往表现”的解释里写明有多少份没被计入。
- `GET /reputation/{fid}/proofs` 每份证明附 `counted_as` 和最终权重。

运营人员入口：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/admin/federation/integrity/anomalies` | 待核验的执行方/请求方对及原因 |
| `POST` | `/v1/admin/federation/integrity/reviews` | `{requester, provider, verdict: genuine|collusive, note, operator}`：真实则恢复计入；串谋则保持 0 并关联为同一控制人 |
| `POST` | `/v1/admin/federation/integrity/links` | `{a, b, reason, operator}`：记录两个工作区同属一个控制人 |

表：`federation_controller_links`、`federation_pair_reviews`（部署级）。

## 11. 隐私边界与投诉记录（P4-4，迁移 3.97）

完整说明见 `docs/federation/PRIVACY.md`；机器可读 `GET /v1/federation/privacy`。

- 上架信息新增 `disclosure`（`completion`、`acceptance`、`response`、`skills`、`cooperation`，默认 true）。隐藏项对他人显示 `{"hidden_by_owner": true}`，并按中性/未知参与排序。
- `GET /reputation/{fid}`：本人看完整；他人看所有者选择公开的。`governance.complaints` 为已结案投诉记录。
- 目录安全系数加入投诉：近一年每条成立 ×0.9（最低 0.6）。
