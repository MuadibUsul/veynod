# 联邦第一轮：身份、授权与消息的地基

> 日期：2026-11-04。执行方：`agent:claude-2`。基线：main `52b1b71`（schema 3.81.0）→ 本轮 schema 3.83.0（3.82 留给部署门 v2）。
> 对应联邦图（`diagrams/federation`）的块：I-ID（定稿）、I-GRANT、I-MSG（契约），F-01a/b/c（表），F-02a/b/c（服务）。
> 联邦开关 `FEDERATION_ENABLED` 默认关闭：本轮合并后，生产行为不变，也不会为任何现有工作区生成联邦身份。

## 1. 本轮交付

| 块 | 交付 | 位置 |
|---|---|---|
| I-ID | owner 确认自证式 ID，TBD 按 P1 占位 | ADR-001a（[ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md)），`doc://federation-i-id-identity-icd` §11 |
| I-GRANT | 授权状态机契约 | [ICD_I_GRANT.md](ICD_I_GRANT.md) |
| I-MSG | 签名信封、验证顺序、投递语义、加密策略契约 | [ICD_I_MSG.md](ICD_I_MSG.md) |
| F-01a | 身份、密钥、身份日志、本地映射、别名与别名历史（平台专用表） | `sql/migrations/v3_83_federation_core.sql` |
| F-01b | `federation_grants` + 只追加的 `federation_grant_history`（版本严格加 1） | 同上 |
| F-01c | 线程、消息（两个幂等唯一键）、投递记录、重放防护（租户表，RLS） | 同上 |
| F-02a | 身份创建、解析、别名、密钥轮换、撤销、冻结、墓碑 | `app/services/federation/identity.py` |
| F-02b | 签名信封生成与 7 步验证（受众、时间窗、摘要、密钥状态、签名、重放） | `app/services/federation/envelope.py` |
| F-02c | 加密策略协商，只升不降 | `app/services/federation/policy.py` |

## 2. agent、地址与信誉怎么绑定

- 一切权威都挂在**规范 ID**（`fed:agent:*`）上：授权、消息、委派，以及以后的信誉（I-REP）。
- **地址只是指向规范 ID 的门牌**：改名留下跳转，删除留下墓碑；P1 地址**永不重新分配**，所以一个地址一生只指向一个 agent。
- 信誉不随地址转移、不能转让（工作区不可转让，TBD-4）；换模型、换运行环境、轮换密钥都不改变规范 ID，信誉保留。
- agent 被删除后成为墓碑，它的记录冻结保留；新 agent 从零开始，不能靠“删号重来”洗掉记录。
- 待 owner 以后决定：agent 换底层模型后，信誉页是否显示“已换模型”。

## 3. P1 占位（代码里已标注）

别名字符集 `[a-z0-9_-]`、1–64 字符、NFKC + casefold（TBD-1；易混淆字符骨架检查未做）；一个根租户对应一个主权主体（TBD-2）；
密钥丢失只能冻结后另发（TBD-3）；父链不可变更（TBD-4、TBD-5）；别名永不重新分配（TBD-6）；
重叠窗口 7 天、身份记录 365 天、时钟偏移 120 秒（TBD-7，可配置）；信封 ≤ 64 KiB、有效期 ≤ 24 小时。

## 4. 验证

```bash
cd AgentGraph
PYTHONPATH=.. TEST_DATABASE_URL=postgresql+asyncpg://… python -m pytest -q \
  tests/test_federation_primitives.py tests/test_federation_identity.py
```

- `test_federation_primitives.py`（13 个，无数据库）：ID 派生测试向量、规范 JSON、KEK 封装、信封的每一种拒绝、密钥与身份状态、加密策略不降级、本地引用不可导出、别名规范化。
- `test_federation_identity.py`（8 个，需要数据库）：表的 RLS 与权限、默认关闭、**F-001 同名 agent 不冲突**（两个根租户、部分别名报歧义、完整别名唯一、响应里没有任何本地 ID）、别名跳转与不复用、轮换/撤销/冻结/墓碑与签名哈希链、授权历史只追加且版本加 1、消息幂等、重放防护按工作区隔离。

## 5. 没做的（下一轮）

- 还没有对外的 HTTP 接口（`/v1/federation/me` 等）和控制台入口；本轮只有服务层。
- F-01d（委派、成本、拉黑、举报、安全事件表）要等 I-BRIDGE 契约。
- F-03（审批流、scope 与 TTL、撤销 fence）、F-04（投递与重试）尚未实现；本轮只把它们需要的表和约束落好。
- `FEDERATION_KEK` 需要 owner 生成并下发（ADR-013）；开启联邦前还需完成 ADR-010 的非超级用户连接切换。
- 迁移号：本轮用 3.83.0；3.82.0 是部署门 v2（分支 `claude/cool-cerf-it7a0j`）的。两边合并时 `app/database.py` 的迁移列表末尾会有一行文本冲突，按 3.82、3.83 的顺序保留两行即可。
