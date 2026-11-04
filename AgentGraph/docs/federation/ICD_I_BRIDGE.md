# 联邦 I-BRIDGE：任务桥（双本地任务）ICD（接口契约）

> 图块：`federation` / I-BRIDGE（依赖 I-GRANT、I-MSG）。本文只定义契约；F-01d 落表，F-07/F-08 实现。
> 依据：愿景 v0.3 §7、§8、§21（F-012…F-018）；ADR-004、ADR-005、ADR-011；[ICD_I_GRANT.md](ICD_I_GRANT.md)；[ICD_I_MSG.md](ICD_I_MSG.md)。
> 规范用语：**必须** / **禁止** / **应当**。`【P1 占位】` 表示首版取的保守值。

## 1. 结论摘要

1. **委派不是远程调用**：A 请 B 做一件事，B 用**自己的** agent、资源和权限在**自己的**工作区执行，再把经授权的结果交回。A 拿不到 B 的任何凭证，B 也拿不到 A 的（ADR-005）。
2. **三角关系**：A 的本地任务、B 的本地任务、双方各存一份的联邦委派记录（`federation_job_id`）。任何一侧都**不能**把另一侧的任务 ID 当成自己的数据库对象读写（ADR-004）。
3. **本地任务状态机不变**：两侧本地任务只通过现有任务服务的公开函数改变状态，不绕过 lease、review、权限、人类锁和质量门。跨域状态只存在于委派记录上。
4. **两个“接受”不同**：`ACCEPTED` 是 B 接单；`ACCEPTED_RESULT` 是 A 验收结果。接单不等于完成，更不等于允许付款。
5. **验收条款冻结**：B 接单时冻结目标、验收标准、输入摘要、预算、期限、取消政策，算出 `acceptance_hash`；之后任何改动都要双方的人重新同意（F-015）。
6. **幂等与不回退**：同一委派重复投递，B 至多建一个本地任务（F-012）；晚到的旧消息存档，但不改变已确认的状态（F-008）。

## 2. 委派对象

委派由 A 发起，作为 `delegate.offer` 信封的 `payload`。字段（全部必填，未启用的显式写 `null` 并标明）：

| 字段 | 说明 |
|---|---|
| `federation_job_id` | `fjob_` + 26 位小写 base32 随机数，A 生成，双方共用 |
| `objective` | 要做什么，纯文本，≤ 4000 字符 |
| `acceptance_criteria[]` | 验收标准，每条纯文本，1–20 条 |
| `input_summary` | 输入摘要；正文只能是摘要，不能夹带 A 的内部指针（§6） |
| `data_classification` | `public` / `internal` / `confidential`【P1 占位】 |
| `allowed_use` | B 可以如何使用输入，例如“仅用于本次任务，不得留存” |
| `deadline` | RFC 3339 UTC |
| `budget_cap` | `{amount, unit}` 或 `null`；P1 只允许 `null` 或 `{"amount": 0, "unit": "none"}`，并显式标注“未启用结算” |
| `result_contract` | 结果格式说明（例如“一份 Markdown 报告 + 文件清单与 SHA-256”） |
| `cancellation_policy` | `hard_cancel` / `drain`；P1 只允许 `drain`【P1 占位，愿景 §5.3 TBD】 |
| `external_side_effects` | 布尔值；为 `true` 时 B 必须由本侧人批准才能接单 |

**BRIDGE-INV-1 无 delegate 无副作用**：A→B 方向的连接上，A 的 `outbound_scopes` 和 B 的 `inbound_scopes` 都必须含 `delegate`（I-GRANT GRANT-INV-3）。否则 B 在写入任何数据之前返回 `FED_E_SCOPE_DENIED`，不建本地任务（F-002）。

## 3. 委派状态机（只在委派记录上）

```text
 DRAFT ─offer─▶ OFFERED ─accept─▶ ACCEPTED ─start─▶ RUNNING ─submit─▶ SUBMITTED ─accept_result─▶ ACCEPTED_RESULT (终态)
                   │                  │                 │  ▲                 │
                   ├─reject─▶ REJECTED (终态)           │  └─revise──────────┤ (REVISION_REQUESTED)
                   ├─过期──▶ EXPIRED (终态)             │                    └─dispute─▶ DISPUTED
                   │                  └──────────┬──────┘
                   │                       cancel_request
                   │                             ▼
                   │                     CANCEL_REQUESTED ─▶ CANCELLED / STOP_NOT_CONFIRMED (终态)
                   └──────────────────────── fail ─────────────▶ FAILED (终态)
```

| 迁移 | 谁发起 | 信封 `type` | 前置 |
|---|---|---|---|
| `DRAFT → OFFERED` | A | `delegate.offer` | 连接 ACTIVE，双边 `delegate` scope |
| `OFFERED → ACCEPTED` | B | `delegate.accept`（带 `acceptance_hash`、B 的委派版本） | B 本地已建任务；`external_side_effects` 时需 B 的人批准 |
| `OFFERED → REJECTED` | B | `delegate.reject` | — |
| `OFFERED → EXPIRED` | 系统 | — | 超过接单期限【P1 占位：24 小时】 |
| `ACCEPTED → RUNNING` | B | `delegate.progress { state: running }` | B 本地任务被认领 |
| `RUNNING → SUBMITTED` | B | `delegate.result`（结果摘要 + 每个产物的 SHA-256） | — |
| `SUBMITTED → ACCEPTED_RESULT` | A | `delegate.accept_result`（验收者、依据、结果版本） | A 的人或 A 的审核流程 |
| `SUBMITTED → RUNNING` | A | `delegate.revise`（返工理由） | 返工次数 ≤ 3【P1 占位】 |
| `SUBMITTED → DISPUTED` | A | `delegate.dispute` | 不自动付款、不标完成（F-014） |
| `* → CANCEL_REQUESTED` | 任一方 | `delegate.cancel` | 非终态 |
| `CANCEL_REQUESTED → CANCELLED` | B | `delegate.cancel_ack` | B 确认已停止 |
| `CANCEL_REQUESTED → STOP_NOT_CONFIRMED` | 系统 | — | 超时未确认；界面必须显示“可能仍在执行”（F-016） |
| `* → FAILED` | B | `delegate.fail` | — |

- 每次迁移把委派记录的 `version` 加 1。信封携带发送方看到的 `version`；接收方只接受 `version` 恰好比本地大 1 的对象，更小的存档不生效（F-008），更大的先对账（§5）。
- 所有 `delegate.*` 信封必须带 `audience`（ID-INV-12），并在 I-GRANT §6 的撤销 fence 之后才生效。

## 4. 两侧本地任务

| 侧 | 何时建 | 怎么建 | 状态如何联动 |
|---|---|---|---|
| A（请求方） | 发出 offer 前，A 的人或 agent 在本地建一个“外包”任务 | 现有任务服务，`kind` 标为 `federation_request` | A 的本地任务由 A 自己的流程推进；收到 `delegate.result` 后进入 A 的审核 |
| B（执行方） | 接单（`ACCEPTED`）时 | 现有任务服务，`kind` 标为 `federation_provide`，正文只放冻结的委派对象，并带 `content_origin = federation` 不可信标记 | B 的 agent 照常认领、执行、审核；B 本地任务进 `REVIEWING`/`DONE` 时，B 向 A 发 `delegate.result` |

- **BRIDGE-INV-2 不改本地状态机**：委派状态的变化**不直接**改另一侧的本地任务；只通过各自的任务服务、由各自的流程推进。
- **BRIDGE-INV-3 一单一任务**：B 侧以 `(peer_workspace, federation_job_id)` 唯一；重复的 `delegate.offer` 返回同一个回执，不建第二个任务（F-012）。

## 5. 一致性、超时与对账

- 本地事务 + outbox（I-MSG §4）；不做跨租户两阶段提交。
- 超时分五种，各自处理：接单超时（→ `EXPIRED`）、执行心跳超时（提示，不自动失败）、截止时间（`deadline`，提示双方的人）、验收超时（提示 A）、网络超时（重试投递）。
- 网络断开恢复后，先发 `delegate.status_query` 查对方的真实状态和 `version`，再决定是否重发；**禁止**因为超时就新建第二个委派（F-013）。
- 死信（I-MSG §4.1）进入人工处理入口，双方的人都能看到：谁委派、谁接、现在谁在等谁、最近证据、下一步。

## 6. 指针与数据边界（F-018）

- **BRIDGE-INV-4 不跨域解引用**：信封里出现的任何对方内部引用（`doc://`、`blob://`、`task://`、URL、文件路径、本地 ID）在接收方一律**只当文本**。接收方的任何工具都不得据此读取对方的文档、Blob 或调用对方工具。
- 结果交付只走 `delegate.result` 的正文与产物清单：产物内容随信封传递（受 I-MSG 大小上限约束），或者经单独授权的分享链接传递；没有授权的链接不得解引用。
- 发送方在发出前清洗：凭证模式（I-MSG §5）一律拒发，本地 ID 一律替换为联邦 ID 或删除（ID-INV-8）。

## 7. 冻结的验收条款（F-015）

```text
acceptance_hash = SHA-256( JCS({objective, acceptance_criteria, input_summary,
                                data_classification, allowed_use, deadline,
                                budget_cap, result_contract, cancellation_policy,
                                external_side_effects}) )
```

- B 在 `delegate.accept` 里回传它计算的 `acceptance_hash`；A 必须核对与自己算的一致，否则这次接单无效。
- 之后任何字段的修改都走 `delegate.amend`：携带新对象和新 hash，**双方的人**都确认后才生效，旧 hash 留在历史里。agent 不能单方面改验收标准。
- 验收结论（`delegate.accept_result` / `delegate.dispute`）必须记录验收者、依据、所针对的结果版本和产物 hash。

## 8. 成本与额度（ADR-005，愿景 §8）

- P1 不做真实支付，不转借任何凭证。B 只回传 `cost_receipt`：`{metering_source: platform|adapter|self_reported, confidence, usage{...}, price_snapshot_id|null, proof_ref|null}`。
- 自报的消耗在界面上必须和实测分开显示；`budget_cap` 为 `null` 时界面写明“未启用结算”。

## 9. 表（F-01d，下一步落地）

租户表，RLS（ADR-011），双方各存一份：

| 表 | 关键字段 |
|---|---|
| `federation_delegations` | `federation_job_id`、`connection_id`、`role`（requester/provider）、`local_task_id`、`state`、`version`、`acceptance_hash`、`frozen_terms`、`deadline`、`last_peer_version`；唯一 `(workspace, peer_workspace, federation_job_id)` |
| `federation_delegation_history` | 只追加；每次迁移一行（`version`、`from`、`to`、`actor`、信封摘要） |
| `federation_cost_receipts` | `federation_job_id`、`metering_source`、`confidence`、`usage`、`price_snapshot_id`、`proof_ref`、`disputed` |
| `federation_blocks` / `federation_reports` / `federation_security_events` | 本地拉黑、举报案件、安全事件（F-10，单独 ICD） |

## 10. 错误码

`FED_E_JOB_UNKNOWN`（404）、`FED_E_JOB_TRANSITION`（409）、`FED_E_JOB_STALE`（409，版本落后）、`FED_E_ACCEPTANCE_MISMATCH`（409，hash 不一致）、
`FED_E_TERMS_INVALID`（422，委派对象字段不合法）、`FED_E_SETTLEMENT_DISABLED`（422，P1 带了非零预算）；另沿用 `FED_E_SCOPE_DENIED`、`FED_E_GRANT_*`。

## 11. 不变量汇总

| 编号 | 内容 | 实现 / 证明 |
|---|---|---|
| BRIDGE-INV-1 | 无双边 `delegate` scope → 403 且无副作用（F-002） | F-07a 测试 |
| BRIDGE-INV-2 | 不改本地任务状态机 | F-07a |
| BRIDGE-INV-3 | 一单一任务（F-012） | F-01d 唯一约束 + F-07a 测试 |
| BRIDGE-INV-4 | 不跨域解引用（F-018） | F-07c 测试 |
| §3 版本 | 乱序不回退（F-008）；断连先对账（F-013） | F-07b |
| §7 | 验收条款冻结（F-015） | F-08b |
