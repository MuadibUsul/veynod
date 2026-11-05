# 联邦 P3：Skill 仓库 ICD（P3-1 发布与版本，P3-2 搜索与发现）

> 依据：愿景 v0.3 §11、§21 F-023、F-024；任务 `fedv2-p3-1-publish-version`。P3-2（搜索与发现）、P3-3（沙箱检测与安装）、P3-4（撤回与通知）在此基础上补充。
> 收费不在本单：发布与安装都是免费的（愿景 §11.1：收费方式 TBD）。

## 1. 结论

- 一个 Skill 版本 = 清单（manifest）+ 若干文本文件。它的 `digest` 是 `{manifest, files}` 规范化 JSON 的 SHA-256。
- 发布方工作区用自己的联邦密钥签一份 `skill.release`：`{name, version, digest, publisher}`。
- 版本一经发布**永不修改、永不删除**（数据库触发器保证），只能撤回（yank）。撤回后不能再被新装，也不能再被新的版本依赖，但 digest 和全部历史保留（F-023）。
- 安装 = 拉取 + 重新计算 digest + 用发布方公钥验签 + 按 digest 锁定。依赖按同样方式一起安装。使用时只返回锁定的那个版本，并且每次都再校验一遍。

## 2. 清单

| 字段 | 规则 |
|---|---|
| `name` | 2–63 位小写字母、数字或短横线；第一个发布的工作区拥有这个名字，之后只有它能发新版本 |
| `version` | 严格的 `MAJOR.MINOR.PATCH`；新版本必须比所有已发布版本都高，不能覆盖 |
| `kind` | `declarative`（给 agent 读的文本，默认）或 `executable` |
| `permissions` | `network` / `filesystem` / `exec` / `secrets` 的子集；`declarative` 必须为空 |
| `dependencies` | `[{name, version}]`，只接受确切版本；发布时锁定为对方的 digest |
| `entry` | 入口文件，必须是文件之一 |
| `description` / `inputs` / `outputs` / `license` | 说明性字段 |

文件：最多 50 个，合计最多 256 KiB，只能是文本，路径只能是相对路径且不能含 `..`。发布前扫描凭证和本地 ID，有就拒绝。

## 3. 安装与使用

- `POST /v1/federation/registry/installs {name, version, accept_permissions}`：只有人能安装。
- 安装 `executable` 版本时，`accept_permissions` 必须**正好等于**它声明的权限，否则返回 `FED_E_PERMISSIONS_NOT_ACCEPTED`。这样新版本不能悄悄扩大权限（F-024 的基础，完整检测在 P3-3）。
- 依赖跟着一起安装，记录 `as_dependency_of`。
- `GET /v1/federation/registry/installs/{name}/content`：agent 读取锁定版本的内容，每次重新校验 digest 和签名，并标记为外部内容（`trust = untrusted`）。
- 已安装的版本后来被撤回：仍然锁定可用，但标记 `yanked = true`；通知已安装者在 P3-4 实现。

## 4. 撤回

- `POST /v1/federation/registry/packages/{name}/versions/{version}/yank {reason}`：只有发布方的人可以撤回。
- 撤回只改状态（`yanked_at`、`yanked_by`、`yank_reason`），不能撤销撤回；要恢复就发布新版本。

## 5. 防篡改

- 数据库触发器禁止修改版本的内容字段，也禁止删除。
- 即使有人绕过数据库约束改了内容，安装和使用时重新计算的 digest 也会对不上，返回 `FED_E_DIGEST_MISMATCH`。
- 发布方密钥被撤销后，它签的版本验签失败（`FED_E_BAD_SIGNATURE`）。

## 6. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/federation/registry/packages` | 发布（人） |
| `GET` | `/v1/federation/registry/packages/{name}` | 包与全部版本（含已撤回的，带 digest） |
| `GET` | `/v1/federation/registry/packages/{name}/versions/{version}` | 单个版本：清单、文件、digest、签名 |
| `POST` | `/v1/federation/registry/packages/{name}/versions/{version}/yank` | 撤回（发布方的人） |
| `POST` | `/v1/federation/registry/installs` | 安装（人） |
| `GET` | `/v1/federation/registry/installs` | 本工作区已安装的 |
| `GET` | `/v1/federation/registry/installs/{name}/content` | 读取锁定的内容 |

## 7. 表（迁移 3.90）

`registry_packages`、`registry_versions`：部署级，应用角色无权限，版本表有不可变触发器。`registry_installs`：按工作区行级安全。

## 8. 错误码

`FED_E_PACKAGE_UNKNOWN`（404）、`FED_E_PACKAGE_INVALID`（422）、`FED_E_NAME_TAKEN`（409）、`FED_E_VERSION_EXISTS`（409）、`FED_E_YANKED`（410）、`FED_E_DIGEST_MISMATCH`（409）、`FED_E_PERMISSIONS_NOT_ACCEPTED`（403）。

## 9. 搜索与发现（P3-2，迁移 3.91）

清单多了两个可选字段：`category`（`research`、`writing`、`coding`、`data`、`design`、`operations`、`other` 之一，默认 `other`）和 `capabilities`（最多 10 个小写标签）。版本本身仍不可变；包上记录的分类、标签和简介跟随最新版本。

条目上的每个数字都标明来源（愿景 §12.1）：

| 来源 | 内容 |
|---|---|
| 发布方自述 | 简介、分类、能力标签 |
| 机器验证 | 每个版本的 digest 和发布方签名 |
| 其他工作区 | 下载量、评分 |
| 平台实测 | 发布方作为执行方回应委派报价的速度、被验收的委派数 |

- **下载量**：拉取过某个版本的**不同工作区**数（安装与作为依赖安装都算）；同一工作区重装不重复计数。
- **评分**：1–5 星，可附 1000 字以内评语（会做凭证扫描）。只有人能评；只有装过这个包的工作区能评；发布方不能给自己评；每个工作区对每个包只有一条，再评即覆盖。评语展示时**不显示评分方地址**。
- **平均分**：至少 3 条评分才显示，不足时显示“证据不足”（`enough_evidence: false`），不给新包打低分。
- **响应速度**：发布方工作区作为执行方，从收到委派报价到接单或拒单的中位时间，统计最近 90 天，至少 3 次才显示。
- **价格**：目前全部免费（`{"model": "free"}`）；按“收费”筛选返回空。

### 9.1 搜索

`GET /v1/federation/registry/search`，参数：

| 参数 | 含义 |
|---|---|
| `q` | 名称或简介包含的词 |
| `category` / `capability` / `kind` | 分类 / 能力标签 / `declarative` 或 `executable` |
| `min_rating` | 平均分不低于此值（证据不足的不算） |
| `price` | `any`、`free`、`paid` |
| `max_response_seconds` | 发布方响应中位时间不超过此值（证据不足的不算） |
| `sort` | `relevance`（默认）、`downloads`、`rating`、`response`、`recent` |
| `include_yanked` | 是否包含已无可装版本的包（默认否） |
| `limit` / `offset` | 每页最多 50 |

### 9.2 条目主页

`GET /v1/federation/registry/entries/{name}` 返回：

- `address`：包名、发布方联邦地址和别名、安装会锁定的版本（`pin`）及其 digest；
- `history`：全部版本（新到旧），每个带 digest、权限、状态、撤回原因、该版本下载量和评分；
- `rating`：平均分、条数、1–5 星分布、最近评语；
- `dependencies` / `dependants`：最新版本依赖了谁（及其当前状态），谁依赖了它；
- `publisher`：包数量、被验收的委派数、响应速度；
- `representative_works`：同一发布方下载量最高的其他 3 个包；
- `evidence`：上面各项来自哪一类来源。

### 9.3 评分与其他入口

| 方法 | 路径 | 说明 |
|---|---|---|
| `PUT` | `/v1/federation/registry/packages/{name}/rating` | 评分（人；装过的工作区；发布方除外） |
| `GET` | `/v1/federation/registry/categories` | 分类列表 |

MCP：`federation` 工具新增 `skills`（搜索，参数同上）和 `skill`（`{name}`，条目主页）。安装与评分仍是人的决定。

新增错误码：`FED_E_RATING_NOT_ALLOWED`（403）。表：`registry_downloads`、`registry_ratings`，部署级，应用角色无权限。
