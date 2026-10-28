# VPS 部署门（Deploy gate）

> 规格：板子文档 `agentgraph/spec-vps-auto-deploy-gate` v1；任务 `vps-auto-deploy-gate-impl`。schema 3.50，应用 3.13.3。

main 一有新合并，生产 VPS 上的部署门就先在本机把关：检查全过才上线，任何一项不过就不动生产；上线后自动自检，自检不过自动退回上一个镜像。整个过程不用 GitHub Actions 额度。owner 只在控制台的“部署门”页面看结果、用开关。

## 组成

| 部分 | 位置 | 作用 |
|---|---|---|
| 接收与记录 | `app/services/deploy_gate.py`、`app/api/deploy_gate_routes.py`、迁移 `v3_50_deploy_gate.sql` | 收 GitHub webhook（验签）、排队、给 runner 派活、存每次的检查结果和报告、只追加的审计 |
| 执行 | `scripts/deploy_gate.py`（只用标准库） | VPS 上每 2 分钟跑一轮：报到、拉 main、认领一次运行、做检查、调用 `deploy_tlines.sh`、回报结果 |
| 常驻 | `deploy/deploy-gate/`、`scripts/install_deploy_gate.sh` | systemd 定时器；装一次，之后随部署自动更新 |
| 控制台 | `dashboard/app/deploy/page.tsx`（`/dashboard/deploy/`） | 状态、开关、每次上线的人话报告；技术细节默认收起 |

## 安装（owner 在 VPS 上做一次）

1. 生成两个密钥，写进部署目录的 `.env`（不要发到聊天或文档里）：

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   - `DEPLOY_GATE_WEBHOOK_SECRET=`：GitHub webhook 的密钥
   - `DEPLOY_GATE_RUNNER_TOKEN=`：runner 调服务端用的令牌（和 ADMIN_TOKEN 分开）
   - `DEPLOY_GATE_BOARD_WORKSPACE_ID=`：板子所在工作区的 ID（G-03 核对任务验收用）
   - 可选 `DEPLOY_GATE_GITHUB_TOKEN=`：只读令牌，避免 GitHub API 未登录时每小时 60 次的限额

2. 用现有流程部署一次包含本版本的代码，让服务端读到这些配置：

   ```bash
   scripts/deploy_tlines.sh deploy
   ```

3. 安装定时器（可以重复执行）：

   ```bash
   sudo scripts/install_deploy_gate.sh
   ```

4. GitHub 仓库 → Settings → Webhooks → Add webhook：
   - Payload URL：`https://veynod.com/v1/deploy-gate/github`
   - Content type：`application/json`
   - Secret：第 1 步的 `DEPLOY_GATE_WEBHOOK_SECRET`
   - 事件：Pushes 和 Pull requests

5. 打开控制台 → 运营会话 → “部署门”，确认显示“运行中”。门默认**不允许自动上线**；想让它全自动，点一次“允许自动上线”。

## 开关的含义

| 开关 | 效果 |
|---|---|
| 暂停 / 恢复 | 暂停期间什么都不上线，排队的触发记为“已跳过”；恢复后从下一次合并开始 |
| 允许 / 停止自动上线 | 不允许时，webhook 和定时器触发的运行照常检查，但不上线；“立即部署”不受影响 |
| 立即部署 | 检查并上线 main 最新的提交；这是你对这一次的明确授权 |

每次触发、开关变化和结果都写进只追加的审计（`GET /v1/admin/deploy-gate/audit`），记录里有触发人、提交、每项检查的结果、是否上线、是否退回。

## 检查项

全部通过才上线；任何一项不过，生产不动，记录写明是哪一项。

| 项 | 内容 |
|---|---|
| G-01 合并记录 | 提交在 main 上；比线上版本新（不会回退）；从线上版本到它之间的每个提交都来自已合并的 PR（`DEPLOY_GATE_ALLOW_DIRECT_PUSH=true` 可放宽）；一次最多 50 个提交 |
| G-02 测试 | 在一次性的 docker 网络里起专用 pgvector 数据库，跑 `ruff` 和全量 `pytest`，再跑 cli 和 protocol 测试；不发布任何端口，碰不到生产库和生产端口 |
| G-03 质量 | 已上线的迁移文件没被改过；schema 版本只增不减；在专用库里先按线上版本建库、再按新版本升级，确认能升级成功、版本号和指纹都记上、没有哪张表丢了行级安全、没有新出现没开行级安全的租户表；PR 里点名的板子任务都已被批准（APPROVE 或 DONE），而且没有失败的质量门 |
| G-04 备份 | `deploy_tlines.sh prepare`：数据库备份并校验（非空、能解压、有表），给正在运行的镜像打回滚标签；任一失败都不上线 |
| G-05 上线与自检 | 生产目录切到这个提交，`DEPLOY_STAMP=<上一步的 stamp> deploy_tlines.sh deploy`；再从公网检查 `/readyz`、`/healthz` 版本号、`/docs/`、`/mcp/`。自检不过就 `rollback-image <stamp>`，数据库**不会**自动恢复 |

数据库恢复会丢掉备份之后的新写入，所以只能由 owner 明确执行：`scripts/deploy_tlines.sh rollback <stamp>`。

## 写 PR 时

- 正文写一行 `Board task \`<task_key>\``（或 `Veynod-Task: <task_key>`），门会核对这个任务的评审结果。没有点名任务的合并默认过不了 G-03（`DEPLOY_GATE_REQUIRE_TASK=false` 可放宽）。
- 正文的 `## 哪里变了` 和 `## 怎么试`（或 `## What changed`、`## How to try`）两节会直接进 owner 报告；没有这两节时，报告用 PR 标题和一句通用的试用说明。

## 暂时跳过的测试

`deploy/gate-quarantine.txt` 列出 G-02 暂时跳过的测试，每行一个 pytest 节点 ID。这个文件和代码一样要走评审；只要它不为空，每次上线的报告都会写“有 N 项已知问题这次没有纳入自动测试”。目前它是空的。

## 已知限制

- **现在 main 上有一个测试失败**：`tests/test_events.py::test_seq_is_gap_free_under_concurrent_writes`（写入吞吐问题，板子任务 `perf-write-path-burst`）。在它修好或被明确列进隔离清单之前，G-02 会拦下每一次上线。这是有意的安全默认。
- G-02 在 VPS 上跑全量测试大约 25–35 分钟，期间会占用 CPU 和内存；单次测试上限由 `DEPLOY_GATE_TEST_TIMEOUT`（默认 3600 秒）控制。
- 部署用 `deploy_tlines.sh deploy`，需要 VPS 能构建镜像。生产 compose 里写死的镜像名不随版本变化；线上实际是哪个提交，以部署门记录的 `deployed_commit` 和回滚 stamp 为准。
- runner 需要拉取 `pgvector/pgvector:pg16`、`python:3.12-slim`、`node:22-slim` 三个镜像（可用 `DEPLOY_GATE_*_IMAGE` 覆盖）。

## 日志

- `/var/log/veynod-deploy-gate/gate.log`：每轮的报到和拉取，超过 5 MB 自动轮换
- `/var/log/veynod-deploy-gate/<run_id>.log`：每次运行的完整技术日志
- 日志里的密钥（`.env` 里名字含 TOKEN、SECRET、PASSWORD、KEY、DATABASE_URL、DSN 的值）一律替换为 `***`

## 安全约束对照

| 规格 | 做法 |
|---|---|
| R-01 只部署 main | webhook 只接受配置的仓库和分支上的合并；G-01 再查一次祖先链和 PR 记录；开关变化都有审计 |
| R-02 密钥不外泄 | 密钥只在 `.env`；runner 不打印、日志脱敏；服务端只存检查摘要和人话报告 |
| R-03 失败必回滚 | 自检不过就回滚镜像；部署脚本自身校验失败时它会自己回滚镜像；数据库只能显式恢复 |
| R-04 测试隔离 | 一次性 docker 网络和数据库，不发布端口，测完就删 |
| R-05 不越权 | 门只读任务和质量门结果，不改任务、不改信任状态、不签发令牌；runner 令牌只能操作部署门 |
| R-06 自愈与离线 | systemd 定时器每 2 分钟一轮；超过 10 分钟没报到，控制台显示“离线”；认领后 3 小时没完成的运行记为失败，队列继续往下走 |
