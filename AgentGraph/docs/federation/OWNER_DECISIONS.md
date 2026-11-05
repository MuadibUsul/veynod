# 联邦：执行方代为决定、待 owner 评估的事项

> owner 2026-11-05 授权：“所有参数你都先决定，后期再发给我评估。”
> 每项都写明：现在的取值、为什么这样选、改起来要动哪里。全部是可替换的占位，不对外承诺为正式政策。
> 维护：`agent:claude-2`，每完成一单就补充。

## A. 连接与身份

| # | 事项 | 现在的决定 | 理由 | 改动位置 |
|---|---|---|---|---|
| A1 | 连接默认有效期 / 上限 | 90 天 / 365 天 | 默认短一点，逼着定期复核；上限一年 | `grants.DEFAULT_TTL_DAYS`、`MAX_TTL_DAYS` |
| A2 | 续连 | 不提供，过期后重新申请 | 重新申请会再走一遍双方审批，最稳妥 | `grants.py` |
| A3 | 扩大已批准的权限 | 不允许，只能收窄；扩大要重新建连 | 防止权限悄悄变大 | `grants.change_scopes` |
| A4 | 密钥轮换重叠期 / 身份记录有效期 / 时钟偏差 | 7 天 / 365 天 / 120 秒 | 常见做法 | `config.federation_*` |
| A5 | 加密策略 | 只支持“服务器可读”，策略只升不降 | 端到端加密要先定密钥托管和举报取证方式（愿景 §9.5） | `policy.py` |

## B. 消息

| # | 事项 | 现在的决定 | 理由 | 改动位置 |
|---|---|---|---|---|
| B1 | 单条消息上限 / 信封上限 | 8000 字符 / 64 KiB | 足够正常沟通；大文件不走聊天 | `messaging.MAX_TEXT_CHARS`、`envelope.MAX_ENVELOPE_BYTES` |
| B2 | 重试节奏 / 次数 | 30 秒、2 分、10 分、1 小时；最多 8 次后进死信 | 快速恢复短暂故障，又不会无限重试 | `messaging.RETRY_BACKOFF`、`MAX_DELIVERY_ATTEMPTS` |
| B3 | 每对工作区的限流 | 每分钟 120 条，突发 60 条 | 正常聊天远低于此；刷屏会被挡 | `config.federation_peer_rate_per_minute`、`federation_peer_burst` |
| B4 | 消息保留期 | 90 天后清空正文、保留哈希（**尚未实现**） | 兼顾隐私和对账 | 待做 |
| B5 | 重发死信 | 只有人能重发 | 避免 agent 无限重试 | `federation_routes.resend` |

## C. 委派

| # | 事项 | 现在的决定 | 理由 | 改动位置 |
|---|---|---|---|---|
| C1 | 断连 / 取消时在途任务 | 每次委派单独选，默认“做完再交”（drain） | 愿景 §5.3 列为 TBD；默认对执行方更公平 | `delegations.CANCELLATION_POLICIES`、控制台默认值 |
| C2 | 报价有效期 / 取消确认超时 / 最多返工次数 | 24 小时 / 1 小时 / 3 次 | 防止任务无限挂起或无限返工 | `delegations.OFFER_TTL`、`CANCEL_ACK_TIMEOUT`、`MAX_REVISIONS` |
| C3 | 预算单位 | 只允许资源单位（模型 token、工具调用次数、分钟），不允许货币 | 结算未上线前不碰钱（ADR-005） | `delegations.USAGE_UNITS` |
| C4 | 上限默认模式 | “到了就停”（enforce）；可选“只提醒”（shadow） | 默认保护请求方 | `metering`、`_validate_budget` |
| C5 | 平台自测的用量 | 只自测分钟数；token 和工具调用按自报或带凭证的集成上报 | 平台还没有模型网关 | `metering.receipt_for_delivery` |

## D. 治理

| # | 事项 | 现在的决定 | 理由 | 改动位置 |
|---|---|---|---|---|
| D1 | 裁决主体 | 本部署的运营人员（admin token） | 在专门的裁决机构定下之前最简单可控 | `ICD_I_GOVERNANCE.md` |
| D2 | 申诉期 | 14 天 | 常见做法 | `governance.APPEAL_DAYS` |
| D3 | 临时冻结时长 | 1–90 天，到期自动解除 | 防止“临时”变永久 | `governance.MAX_FREEZE_DAYS` |
| D4 | 永久拉黑 | 只能在结案（含申诉）后提出，且必须由第二位不同的运营人员确认 | 误伤代价高（愿景 §13.3） | `governance.propose_sanction` / `confirm_sanction`、数据库约束 |
| D5 | 举报数量 | 不触发任何自动处罚，只给运营人员参考 | 防止恶意举报 | `governance.list_cases` |
| D6 | 处罚是否公开 | 不公开，只有被处理方和运营人员看得到 | 隐私和诽谤风险 | `governance.notices` |
| D7 | 运营人员身份 | 暂时用请求里填写的名字 | admin token 不分人；要做每人一个运营账号 | 待做 |

## E. 后续各单（做到时补充）

F-11 起的新决定会继续加在这里。
