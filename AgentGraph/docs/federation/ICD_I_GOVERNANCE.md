# 联邦 I-GOV：基础治理 ICD（F-10）

> 依据：愿景 v0.3 §13（举报、封禁、申诉）、§21 F-019、F-020；任务 `fedv2-f10-governance`。
> 规范用语：**必须** / **禁止**。占位值标【P1 占位】，裁决主体等政策仍是 owner 的决定（愿景 §13.3 TBD）。

## 1. 四件事分开

| 名称 | 谁做 | 效果 | 不代表什么 |
|---|---|---|---|
| 本地拉黑 | 本工作区的负责人（`grants.block`） | 只是本工作区拒绝对方 | 不是平台认定，别人不受影响 |
| 举报 | 本工作区的人（不能是 agent） | **只立案**（F-019） | 不自动处罚，多少条举报都一样 |
| 案件处理 | 本部署的运营人员（admin token），逐步手动推进 | 记录每一步 | 不会自动推进 |
| 处罚 | 运营人员 | 临时冻结；或永久拉黑（需要第二位运营人员确认） | 永久拉黑不会因举报数量自动触发 |

## 2. 举报

- `POST /v1/federation/reports`：`accused`（工作区或 agent 的联邦 ID）、`category`（`spam` / `abuse` / `fraud` / `impersonation` / `data_leak` / `prompt_injection` / `other`）、`statement`（10–4000 字，对方看不到）、`evidence`（引用列表）、`block`（是否同时本地拉黑）。
- **证据只存摘要**：消息引用变成 `message_id`、方向、`payload_sha256`、对方签名的 SHA-256；委派引用变成 `federation_job_id`、角色、状态、版本、`acceptance_hash`。正文不复制进案件。只能引用本工作区与被举报方之间的消息或委派。
- 不能举报自己；`statement` 里出现疑似凭证会被拒（与消息同一套检查）。
- 举报人可以在提出处理意见之前撤回（`/withdraw`）。

## 3. 案件状态机

```
SUBMITTED → TRIAGED → INVESTIGATING → ACTION_PROPOSED → DECIDED → APPEAL_WINDOW → CLOSED
     ↘ DISMISSED      ↘ INSUFFICIENT_EVIDENCE / DISMISSED   ↘ DISMISSED
（举报人撤回：SUBMITTED / TRIAGED / INVESTIGATING → WITHDRAWN）
```

- 只能按上图一步一步走，每一步记下运营人员姓名和备注（只追加）。进入 `DECIDED` 必须写明决定理由。
- 进入 `APPEAL_WINDOW` 时开始申诉期：14 天【P1 占位】。申诉期内，没人申诉时不能结案；被处理方的负责人可以申诉一次。
- 被处理方通过 `GET /v1/federation/governance/notices` 看到类别、决定、申诉期限和处罚，**看不到**举报人和举报内容。

## 4. 处罚

- **临时冻结**：只能在案件 `DECIDED` 之后，1–90 天【P1 占位】，立即生效。被冻结的身份在所有联邦入口都被拒（`FED_E_IDENTITY_FROZEN`），到期由投递循环自动解除；运营人员也可以提前解除。
- **永久拉黑**：只能在案件 `CLOSED`（申诉期已过或申诉已处理）之后**提出**；提出后不生效，必须由**另一位**运营人员确认（数据库约束：`confirmed_by <> proposed_by`）。
- 运营人员身份目前是请求里填写的姓名：admin token 本身不区分人。按人区分的运营账号属于后续工作。
- 处罚记录在 `federation_sanctions`（部署级表，应用角色无权读写）。查询：`GET /v1/admin/federation/identities/{fid}/status`。

## 5. 伪造的平台通知（F-020）

- 平台的决定只在本部署由运营人员做出，**从不**经由联邦信封下发。
- 任何工作区发来的 `governance.notice` 一律拒绝：签名验证不过的，按验签错误拒绝；签名验证通过的，也返回 `FED_E_AUTHORITY_INVALID`。没有哪个工作区能代表平台。两种情况都记安全事件，全局处罚状态不变。
- 聊天里自称“平台裁决”的文字只是外部内容（ICD_I_MSG §5），不改变任何状态。

## 6. 安全事件

`GET /v1/federation/security-events`：其他工作区尝试过、因安全原因被拒的事（目前包括伪造平台通知、消息夹带凭证）。只追加。

## 7. 运营接口（admin token）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/admin/federation/cases?state=` | 案件列表（含被举报方累计被举报次数，只作参考） |
| `GET` | `/v1/admin/federation/cases/{id}` | 单个案件与历史 |
| `POST` | `/v1/admin/federation/cases/{id}/move` | 推进一步：`to_state`、`operator`、`note`、`decision` |
| `POST` | `/v1/admin/federation/sanctions` | 临时冻结（`days`）或提出永久拉黑 |
| `POST` | `/v1/admin/federation/sanctions/{id}/confirm` | 第二位运营人员确认永久拉黑 |
| `POST` | `/v1/admin/federation/sanctions/{id}/lift` | 解除 |
| `GET` | `/v1/admin/federation/identities/{fid}/status` | 某身份的处罚状态 |

## 8. 错误码

`FED_E_CASE_UNKNOWN`（404）、`FED_E_CASE_TRANSITION`（409）、`FED_E_SANCTION_RULE`（409）、`FED_E_AUTHORITY_INVALID`（403）。

## 9. 表（迁移 3.88）

`federation_reports`、`federation_report_history`（只追加）、`federation_security_events`（只追加）：按工作区行级安全。`federation_sanctions`：部署级，应用角色无权限。

## 10. 待 owner 决定

裁决主体、永久拉黑的标准、申诉期长度、证据保留期限、处罚是否对其他用户公开（愿景 §13.3）。在定下之前，本实现只提供手动、可复核、默认保守的流程。
