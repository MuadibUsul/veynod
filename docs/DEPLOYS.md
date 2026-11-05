# 部署（部署门 v2）：每个项目都能自动部署合并后的镜像

部署门 v2 对**所有用户**开放：一个项目配好服务器，就能把合并到主分支的提交自动部署上去。
用户自己打开或关闭，可以每次合并都部署、在时间窗口内部署，或只在手动点击时部署。
v1（只服务 Veynod 自己、只有运营者能用）已于 2026-11-05 退役，见文末。

## 一张图

```
 合并到 main ──► CI 绿 ──► Release 工作流（.github/workflows/release.yml）
                            │  只发布 main 的最新提交；main 已经往前走了就跳过
                            │  scripts/release.py：每个服务算"输入哈希"
                            │  有同哈希的镜像 → 只打新标签；没有 → BuildKit 构建一次并推送 GHCR
                            ▼
                 POST /v1/deploys/releases（目标的发布令牌）
                            │  记录 release：每个服务的 image@digest
                            ▼
                 部署门（app/services/deploys.py）
                            │  只认目标分支、单调递增、每个目标同时只跑一个、等待中的合并成最新
                            │  计划：只有 digest 变了的服务才动
                            ▼
                 runner_placements（step = deploy）──nerve──► 服务器上的部署电脑
                                                              （veynod up --deploy）
                            ◄── deploy-report：running → deployed / rolled_back / failed
```

- **服务器不开端口、Veynod 不持有 SSH 密钥。** 部署电脑和任何执行端电脑一样从里往外连 nerve；
  部署就是一个 `step = deploy` 的放置，走同一套两次回执。
- **只有人能**打开部署、立即部署、回滚、生成发布令牌（宪法：上线和不可逆操作只属于人）。
  Agent 能看设置和记录，看不到令牌。
- 失败或已回滚的部署出现在 Loop 的"需要你"里（按钮打开 设置 → 部署），点"已看过"后消失。自动和定时触发**不会**重试
  同一个失败过的 release，只有人能重新部署它。

## 增量：不浪费资源

三层，各管一段：

| 层 | 在哪 | 做什么 |
|---|---|---|
| 构建 | `scripts/release.py` + `contracts/deploy_plan.input_digest` | 每个服务的输入哈希 = 它构建所用的被跟踪文件（`.veynod/deploy.yml` 的 `paths` + `shared` + Dockerfile）的 blob 哈希。GHCR 已有 `image:inputs-<哈希>` 就只 `imagetools create` 打新标签，不构建。只改文档的提交一个镜像都不构建 |
| 层缓存 | `deploy/Dockerfile.*`、`type=gha` 缓存 | Dockerfile 先装依赖再拷代码；代码变了，依赖层照样命中缓存 |
| 部署 | `contracts/deploy_plan.plan` + `cli/bin/deploy.mjs` | 只 `pull` 和 `up -d --no-deps` digest 变了的服务；一个服务都没变的 release 直接记为已部署，不派活 |

`.veynod/deploy.yml` 示例（Veynod 自己的见仓库根目录）：

```yaml
services:
  api:
    image: ghcr.io/acme/app-api          # 不带标签
    dockerfile: deploy/Dockerfile.api    # 从仓库根目录构建
    compose: [api]                       # 跑这个镜像的 compose 服务（默认同名）
    paths: ["app/**", "requirements.txt"]
shared: ["lib/**"]                       # 改了这里，所有服务都重建
```

compose 文件里每个服务的镜像写成 `${VEYNOD_IMAGE_<服务名大写>:-本地默认}`，部署电脑会把
`image@digest` 写进 `<compose 目录>/.veynod/deploy.env`。`contracts/tests/test_repository.py`
检查 Veynod 自己的配置、Dockerfile 和 compose 文件三者一致。

## 给一个项目打开部署（任何用户）

1. **服务器上**接入部署电脑（需要 Docker 和 compose 插件）：

   ```bash
   npm install -g veynod
   veynod up --deploy --compose-dir /srv/app --compose-file docker-compose.yml \
     --before-deploy ./scripts/migrate.sh      # 可选：部署前跑的本机命令，不经过 shell
   ```

   控制台批准后，它出现在 Loop → 执行端电脑，不接任何别的活。`veynod doctor` 检查 docker 和 compose 文件。
2. **控制台** 设置 → 部署 → 项目下的目标 → 配置：勾选"部署这个项目"，选部署电脑、触发方式
   （每次合并 / 时间窗口 / 只手动）、分支、健康检查 URL、失败是否回滚，保存。
   等价 API：`PUT /v1/projects/{p}/deploys/production`。
3. 点"生成发布令牌"，把它存为仓库 CI 的密钥 `VEYNOD_RELEASE_TOKEN`（只显示一次，服务端只存哈希）。
   等价 API：`POST /v1/projects/{p}/deploys/production/release-token`。
4. 在仓库里放 `.veynod/deploy.yml` 和 `.github/workflows/release.yml`（照抄本仓库的，改镜像名），
   以及在仓库变量里设 `VEYNOD_HOST`（默认 `https://veynod.com`）。

关闭：取消勾选即可。正在跑的那次会跑完，排队中的会被跳过。

## Veynod 自己的 VPS：第一个目标

Veynod 部署在 `/home/deploy/veynod-monorepo`，用 `deploy/vps/docker-compose.unified.yml`。
**先按 [RELEASE_0.6.0.md](RELEASE_0.6.0.md) 完成 0.6.0 切换**（v2 依赖 0.6.0 的服务端和 CLI），然后：

1. GitHub → 仓库 Settings → Actions：允许工作流写 packages（`release.yml` 已声明 `packages: write`）。
   第一次推送后，把 `ghcr.io/muadibusul/veynod-{api,kernel,runtime}` 三个包设为私有并允许 VPS 拉取：
   在 VPS 上用一个只有 `read:packages` 的令牌执行 `docker login ghcr.io`。
2. VPS 上（`deploy` 用户）：

   ```bash
   cd /home/deploy/veynod-monorepo
   cp deploy/vps/docker-compose.unified.yml docker-compose.unified.yml   # 若还没有
   npm install -g veynod@0.6.0
   veynod up --deploy --name veynod-vps-deploy \
     --compose-dir /home/deploy/veynod-monorepo --compose-file docker-compose.unified.yml \
     --before-deploy ./scripts/migrate_all.sh
   ```

   `migrate_all.sh` 由部署电脑调用时会读 `VEYNOD_DEPLOY_ENV`，用即将上线的镜像先迁移；迁移失败就不重启任何服务。
3. 控制台（Veynod 自己的工作区）→ 项目 `veynod` → 部署设置：部署电脑选 `veynod-vps-deploy`，
   分支 `main`，**先选"只在我点部署时"**，健康检查 `https://veynod.com/healthz/deep`，回滚打开。
4. 生成发布令牌 → GitHub 仓库 Secrets 设 `VEYNOD_RELEASE_TOKEN`。
5. 合并一个只改文档的提交：Release 工作流应当三个镜像都"retag"、不构建；控制台出现 release。
6. 点"部署最新"。期望：部署电脑日志出现 `pull`、`restart`、`health: ok`；控制台该次显示"已上线"，
   并列出重启了哪些服务。之后再把触发方式改为"每次合并"或一个夜间窗口。

部署禁忌照旧（[PRODUCTION_OPERATIONS.md](PRODUCTION_OPERATIONS.md)）：部署电脑只会执行
`docker compose ... up -d --no-deps <服务>`，从不 `down`、不 `--remove-orphans`。数据库容器 `veynod-db-1`
不在 unified 文件里，部署永远碰不到它。

## 门的规则（A3，`tests/test_deploys.py` 覆盖）

| 规则 | 行为 |
|---|---|
| 分支 | 只有目标分支上的 release 会排队（`wrong_branch`） |
| 单调 | 比当前线上或在跑的更旧的 release 不排队（`not_newer`）；人点名旧提交就是回滚（`trigger = rollback`）。"旧"按 release 到达的序号（`seq`）算，不看 git 历史，所以 Release 工作流只发布 main 的最新提交：重跑旧提交的 CI、或几次 CI 乱序跑完，都不会把旧代码当成最新的发出去 |
| 一个在跑 | 每个目标同时最多一个 `offered`/`running`（唯一索引保证） |
| 合并 | 最多一个 `queued`；新 release 到来时替换它（`coalesced`），不排在后面 |
| 失败不自动重试 | 失败或回滚过的 release，自动/定时都不再排队（`failed_before`），人可以手动重新部署 |
| 送不到 | 部署电脑离线：保持排队，原因写在记录上；送出后 `runner_claim_timeout_seconds` 内没开始：放回队列 |
| 超时 | 跑了超过 `DEPLOY_RUN_TIMEOUT_SECONDS`（默认 1800）记为失败 |

## v1 部署门退役（2026-11-05）

v1 自 2026-10-29 起暂停、不在任务流程里。原计划等 v2 在 VPS 上连续成功部署两周再拆；owner 在
2026-11-05 决定提前拆（生产 `.env` 里没有 v1 的 runner 令牌，它本来就没在运行）。

1. **已删（2026-11-05）**：`/v1/deploy-gate/*` 和 `/v1/admin/deploy-gate/*` 路由、`app/services/deploy_gate.py`、
   `app/schemas_deploy_gate.py`、`app/api/deploy_gate_routes.py`、`AgentGraph/scripts/{deploy_gate.py,install_deploy_gate.sh}`、
   `AgentGraph/deploy/deploy-gate/`、`deploy/gate-quarantine.txt`、`DEPLOY_GATE_*` 和 `DEPLOY_EVIDENCE_REQUIRED` 配置、
   "任务要等上线证据才 DONE"（Loop A-03，默认一直关着）、控制台"允许自动上线"开关（只对 v1 起作用；
   `auto_deploy_allowed` 字段为兼容仍可读写，但不再有作用）、相关测试和 `docs/DEPLOY_GATE.md`。
   `/dashboard/deploy/` 跳转到 设置 → 部署。
2. **保留**：`AgentGraph/scripts/deploy_tlines.sh`，它仍是手动部署生产的脚本（见 `AgentGraph/docs/PROJECT_MAP.md`）。
3. **VPS 上要做一次**（若装过 v1 的定时器）：`sudo systemctl disable --now veynod-deploy-gate.timer`；
   `.env` 里残留的 `DEPLOY_GATE_*` 行会被忽略，可以删掉。
4. **下一个版本**：新迁移删除 v1 的表（`deploy_gate_state`、`deploy_gate_runs`、`deploy_gate_audit`）。
   表这次先留着，万一要回退代码，数据还在。
