# 联邦第二轮：连接审批、授权 fence 与对外接口

> 日期：2026-11-04。执行方：`agent:claude-2`。基线：main `605f1a1`（schema 3.83.0，第一轮已合并）。本轮**没有新迁移**。
> 对应联邦图的块：I-BRIDGE（契约）、F-03a/b/c（审批流、scope 与有效期、撤销 fence），以及第一个对外 HTTP 接口。
> `FEDERATION_ENABLED` 仍默认关闭；关闭时所有 `/v1/federation/*` 一律回 404 `federation_disabled`。

## 1. 本轮交付

| 内容 | 位置 |
|---|---|
| I-BRIDGE 任务桥契约：双本地任务 + 联邦委派记录、委派状态机、冻结验收条款、跨域指针禁令、不结算的成本收据 | [ICD_I_BRIDGE.md](ICD_I_BRIDGE.md) |
| 连接状态机服务：申请、批准（只有 owner，可收窄）、拒绝、撤销（通知对方）、冻结/解冻、拉黑（本地，不泄露）、收窄 scope、按需过期、两侧各自的历史 | `app/services/federation/grants.py` |
| `authorize()`：每个联邦入口在产生副作用前调用的 fence（状态、有效期、对方身份、方向性 scope、“限制之前签的对象一律作废”） | 同上 |
| 同部署投递 `LocalTransport`：把签名信封交给目标工作区，接收方按网络输入同样完整地验签（ADR-006） | 同上 |
| HTTP 接口 | `app/api/federation_routes.py`（见 §2） |
| 6 个对外错误码，具体联邦错误码放在 `details.federation_code` | `app/error_registry.py`、`docs/ERROR_CODES.md` |

## 2. 接口

| 方法 | 路径 | 谁可以 |
|---|---|---|
| GET | `/v1/federation/me` | 本工作区成员；未开启联邦时 `identity` 为 `null` |
| POST | `/v1/federation/enable` | owner 级的人 |
| PUT | `/v1/federation/me/alias` | owner 级的人 |
| GET | `/v1/federation/identities/{federation_id}` | 本工作区成员 |
| GET | `/v1/federation/resolve?subject=&workspace=&agent=` | 本工作区成员；有歧义时返回全部候选，从不替你选 |
| GET | `/v1/federation/connections` | 本工作区成员 |
| POST | `/v1/federation/connections` | owner 级的人（P1） |
| GET | `/v1/federation/connections/{id}/history` | 本工作区成员 |
| POST | `/v1/federation/connections/{id}/approve` | owner 级的人；可只批准一部分 scope |
| POST | `/v1/federation/connections/{id}/{reject,revoke,freeze,block}` | 本工作区的人 |
| POST | `/v1/federation/connections/{id}/unfreeze` | owner 级的人 |
| PATCH | `/v1/federation/connections/{id}/scopes` | 本工作区的人；只能收窄 |

“人”指登录的人类账号或运营控制台；工作区密钥和 agent token 都算 agent，不能批准任何东西（F-003）。

## 3. 验收用例

- `tests/test_federation_grants.py`（服务层，5 组）：
  - 双方 owner 都同意才生效，scope 分方向；
  - agent 和非 owner 不能批准（F-003）；
  - 第三方伪造的批准或撤销无效（F-003）；
  - A↔B、B↔C 不等于 A↔C（F-004）；
  - 冻结、撤销、拉黑、过期之后，新请求和“限制之前签好”的旧请求都被拒（F-005）；
  - 收窄 scope 也算一次 fence。
- `tests/test_federation_api.py`（HTTP，2 组）：
  - 部署关闭联邦时返回 `federation_disabled`；
  - 完整走一遍连接生命周期：开启、发布别名、解析、申请、他人批准被拒、owner 批准、收窄、拒绝扩大、撤销、两侧历史；
  - 跨工作区看不到别人的连接；
  - 响应里没有任何本地 ID。
- 无 delegate scope 时委派被拒（F-002）：本轮由 `authorize(..., "delegate")` 返回 `FED_E_SCOPE_DENIED` 证明；真正的委派入口（F-07）上线时会复用同一个检查。

## 4. P1 的简化（有意为之，后续轮次复审）

- 只有 owner 能发起连接；agent 代发需要先接上“升级给人”的流程。
- `change_scopes` 只能收窄；要扩大，需撤销后重新申请，双方 owner 都要重新同意。
- 不提供“续连”：过期后重新申请。
- 冻结、拉黑只在本侧生效，不通知对方；对方下次请求得到 `FED_E_GRANT_INACTIVE`，看不出原因。
- 信封时间只精确到秒：同一秒内签的对象，紧跟一次限制操作时会被保守地判为过期，需要重签。
- 服务层用平台会话读写租户表（与第一轮一致）。生产切换到非超级用户连接（ADR-010）时，平台角色需要 BYPASSRLS。

## 5. 下一轮

F-01d 委派表 → F-07 任务桥（offer/accept、双本地任务、幂等接单、断连对账、指针 fence）→ F-04 消息投递与重试 → 控制台“联邦”入口（F-05）。
