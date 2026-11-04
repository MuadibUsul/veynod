# 联邦 I-GRANT：授权状态机 ICD（接口契约）

> 图块：`federation` / I-GRANT（依赖 I-ID）。本文只定义契约；F-01b 落表，F-03a–c 实现流程。
> 依据：愿景 `agentgraph/federation-vision` v0.3 §5、§21（F-002…F-005）；ADR-002、ADR-009、ADR-011（[ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md)）；
> I-ID 身份契约 `doc://federation-i-id-identity-icd`（ADR-001a 已确认）。
> 规范用语：**必须** / **禁止** / **应当**。`【P1 占位】` 表示首版取的保守值，开放 Public Opt-in 前复审。

## 1. 结论摘要

1. **连接 = 两个工作区之间的一份授权**，按工作区规范 ID（`fed:workspace:*`）绑定，不按别名（ID-INV-9）。
2. **双方各存一份**：每个工作区在自己的租户表里保存“我这一侧看到的连接”。不建跨根 JOIN（ADR-011）。
3. **方向性 scope**：`outbound_scopes` 是“我可以对对方做什么”，`inbound_scopes` 是“对方可以对我做什么”。两边分开批准、分开撤销（ADR-002）。
4. **只有人能批准**：进入 `ACTIVE`、扩大 scope、续连、解冻都需要本侧 owner 级人类用户的决定；Agent 只能发起申请（F-003）。
5. **`grant_version` 只追加**：每一次状态或 scope 变化都写一行历史，版本号严格加 1，旧行永不改写。
6. **撤销是 fence**：撤销、冻结、拉黑、过期后，所有入口按“当前状态 + 当前 `grant_version`/`revocation_epoch`”校验，携带旧版本的重试一律拒绝（F-005）。

## 2. 连接对象

本侧存储（F-01b：`federation_grants` + `federation_grant_history`，均为租户表，RLS）：

| 字段 | 说明 |
|---|---|
| `connection_id` | 连接 ID，`fcn_` + 26 位小写 base32 随机数。由发起方生成，双方共用；只用于关联，不具权威 |
| `local_workspace_fid` / `peer_workspace_fid` | 本侧与对方工作区的规范 ID |
| `peer_subject_fid` | 对方主权主体；用于展示“对方是谁”与拉黑范围 |
| `initiator` | `local` / `peer`：谁发起的申请 |
| `state` | §3 |
| `outbound_scopes` / `inbound_scopes` | §4，字符串数组 |
| `grant_version` | 单调递增，从 1 开始 |
| `revocation_epoch` | 单调递增；每次撤销、冻结、拉黑加 1 |
| `expires_at` | 到期时间，必填【P1 占位：默认 90 天，最长 365 天】 |
| `peer_key_set_version` | 批准时看到的对方 `key_set_version`（愿景 §5.1“密钥指纹”） |
| `encryption_policy` | P1 只允许 `server_readable`（ADR-009）；见 I-MSG §6 |
| `may_redelegate` | 恒为 `false`（ADR-002，愿景 §3.3） |
| `reason` | 申请理由，必填，≥ 5 个字符 |

**GRANT-INV-1 不跨连接继承**：A↔B 与 B↔C 是两份独立授权，A 不因此获得对 C 的任何权限（F-004）。

## 3. 状态机

```text
                 ┌───────────── reject ─────────────▶ REJECTED (终态)
                 │
 [申请] ──▶ REQUESTED ── approve(人) ──▶ ACTIVE ◀── renew(人) ── EXPIRED
                 │                         │  ▲                      ▲
                 │                         │  └── unfreeze(人) ── SUSPENDED
                 │                         ├── freeze(人/策略) ──────┘
                 │                         ├── 到期 ───────────────▶ EXPIRED
                 ├── 邀请过期 ─────────────┼───────────────────────▶ EXPIRED
                 │                         ├── revoke(任一方) ─────▶ REVOKED (终态)
                 └── block(本侧) ──────────┴── block(本侧) ────────▶ BLOCKED (终态)
```

合法迁移表（其他迁移一律拒绝，`FED_E_GRANT_TRANSITION`）：

| 从 | 到 | 动作 | 谁可以做 | revocation_epoch |
|---|---|---|---|---|
| — | `REQUESTED` | `request` | 本侧人，或持有本侧 commander 授权的 Agent；或收到对方签名的申请 | — |
| `REQUESTED` | `ACTIVE` | `approve` | **本侧 owner 级人类用户** | 不变 |
| `REQUESTED` | `REJECTED` | `reject` | 本侧人 | 不变 |
| `REQUESTED` / `ACTIVE` | `EXPIRED` | `expire` | 系统（请求时发现已过 `expires_at`） | 不变 |
| `EXPIRED` | `ACTIVE` | `renew` | 本侧 owner 级人类用户（双方都要续） | 不变 |
| `ACTIVE` | `SUSPENDED` | `freeze` | 本侧人或风控策略 | +1 |
| `SUSPENDED` | `ACTIVE` | `unfreeze` | 本侧 owner 级人类用户 | 不变 |
| `ACTIVE` / `SUSPENDED` / `EXPIRED` | `REVOKED` | `revoke` | 任一方的人；或收到对方签名的撤销 | +1 |
| 任意非终态 | `BLOCKED` | `block` | 本侧人 | +1 |
| `ACTIVE` | `ACTIVE` | `change_scopes` | 缩小：本侧人；**扩大：本侧 owner 级人类用户** | 缩小时 +1 |

- **GRANT-INV-2 只有人能批准**：`approve`、`renew`、`unfreeze`、扩大 scope 的操作人必须是本地已认证的人类用户（`user:*`），并且在该工作区是 owner 级。Agent token、远端签名对象、自称“人类已批准”的消息字段都**不能**完成这些动作；被拒绝时写 `FED_E_APPROVAL_NOT_HUMAN` 审计（F-003）。
- **GRANT-INV-3 双边生效**：一个方向的能力，需要“发出方的 `outbound_scopes`”和“接收方的 `inbound_scopes`”同时包含它才可用。任何一边单方面撤销，该方向立即失效。
- `BLOCKED` 是本地状态，不等于平台处罚（愿景 §5.2）。拉黑后**禁止**向对方回送任何本侧证据或联系方式。
- 终态（`REJECTED`、`REVOKED`、`BLOCKED`）不可恢复；要重新合作必须发起新的申请，得到新的 `connection_id`。

## 4. scope（方向性能力）

P1 允许的 scope（未知 scope 一律拒绝，`FED_E_SCOPE_UNKNOWN`）：

| scope | 含义 | 首版 |
|---|---|---|
| `chat` | 1:1 消息 | 可用 |
| `delegate` | 发起任务委派（F-07） | 只落字段，F-07 实现前任何委派返回 `FED_E_SCOPE_DENIED` |
| `presence` | 读取对方在线/忙碌摘要 | 只落字段 |

- **GRANT-INV-4 缺 scope 无副作用**：缺少所需 scope 的请求，在写入任何业务数据之前返回 `FED_E_SCOPE_DENIED`（HTTP 403）。例如只有 `chat` 时，聊天可用，委派 403 且对方本地不新建任务（F-002）。
- scope 只能写成精确字符串，不支持通配。

## 5. `grant_version` 与历史

- **GRANT-INV-5 只追加**：`federation_grant_history` 每行记录 `(connection_id, grant_version, action, from_state, to_state, outbound_scopes, inbound_scopes, expires_at, revocation_epoch, actor, approved_by, policy_hash, created_at)`。数据库层**禁止** UPDATE 和 DELETE（触发器）。
- **GRANT-INV-6 严格加 1**：新行的 `grant_version` 必须等于该连接已有最大值 + 1（数据库触发器校验）。`federation_grants` 上的当前值必须与最新历史行一致。
- `policy_hash` = 当前授权快照（状态、两组 scope、到期、加密策略、epoch）的规范 JSON（I-MSG §3 的 JCS）SHA-256，用来让双方核对“我们说的是同一份授权”。
- `actor` 取值 `user:<id>` / `agent:<id>` / `peer:<fed:workspace:*>` / `system`；`approved_by` 只在需要人批准的动作上填 `user:<id>`。

## 6. 撤销 fence

每个联邦入口（发送、接收、委派、续约）在产生副作用之前，**必须**按顺序检查（F-005）：

1. 连接存在，且 `state = ACTIVE`；
2. `now < expires_at`，否则就地转为 `EXPIRED` 并拒绝；
3. 对方身份不是 `frozen` / `tombstoned`，签名密钥未撤销（I-ID §5.3）；
4. 所需 scope 同时在发出方 outbound 与接收方 inbound 中（GRANT-INV-3）；
5. 入站对象携带的 `grant_version` ≥ 本侧记录的“最近一次撤销类动作时的版本”，并且其 `revocation_epoch` 等于本侧当前值。

- 第 5 条使“撤销前签好、撤销后重放”的旧请求一律失败，即使签名本身有效（`FED_E_GRANT_STALE`）。
- 校验发生在每次请求时，不缓存建连时的结果（愿景 §5.3）。
- 能保证的：拒绝新的消息、委派、续约；排队中未被对方接受的命令失效。**不能**保证：对方已开始的计算被回滚。在途任务的 `hard_cancel` / `drain` 策略属于 F-08，P1 不承诺。

## 7. 跨实例协议对象（签名信封，I-MSG §2）

| `type` | 发送方 | 作用 |
|---|---|---|
| `grant.request` | 发起方工作区 | 携带 `connection_id`、申请的两组 scope、`reason`、`expires_at` 提议 |
| `grant.decision` | 接收方工作区 | `approved` / `rejected`；`approved` 时带最终 scope、到期、`grant_version` |
| `grant.update` | 任一方 | 缩小 scope、续连、冻结通知 |
| `grant.revoke` | 任一方 | 撤销；接收方立即转 `REVOKED` 并 +1 epoch |

- 所有对象都必须带 `audience`（对方工作区的规范 ID，ID-INV-12）。
- 收到 `grant.decision: approved` 只代表**对方**同意；本侧要进入 `ACTIVE`，仍需本侧人类批准（双向同意）。
- 拉黑不发送任何对象（避免泄露），对方下次请求时得到通用的 `FED_E_GRANT_INACTIVE`。

## 8. 错误码

| 错误码 | HTTP | 条款 |
|---|---|---|
| `FED_E_GRANT_UNKNOWN` | 404 | 连接不存在 |
| `FED_E_GRANT_INACTIVE` | 403 | §6 第 1 条（含 BLOCKED，故意不区分） |
| `FED_E_GRANT_EXPIRED` | 403 | §6 第 2 条 |
| `FED_E_GRANT_STALE` | 409 | §6 第 5 条 |
| `FED_E_GRANT_TRANSITION` | 409 | §3 |
| `FED_E_SCOPE_DENIED` | 403 | GRANT-INV-4 |
| `FED_E_SCOPE_UNKNOWN` | 422 | §4 |
| `FED_E_APPROVAL_NOT_HUMAN` | 403 | GRANT-INV-2 |

## 9. 不变量汇总

| 编号 | 内容 | 实现 / 证明 |
|---|---|---|
| GRANT-INV-1 | 不跨连接继承（F-004） | F-03b 测试 |
| GRANT-INV-2 | 只有人能批准（F-003） | F-03a 测试 |
| GRANT-INV-3 | 双边 scope 同时满足 | F-03b |
| GRANT-INV-4 | 缺 scope 403 且无副作用（F-002） | F-03b 测试 |
| GRANT-INV-5 | 历史只追加 | F-01b 触发器 + 测试 |
| GRANT-INV-6 | `grant_version` 严格加 1 | F-01b 触发器 + 测试 |
| §6 | 撤销 fence（F-005） | F-03c 测试 |
