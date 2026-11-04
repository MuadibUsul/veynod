# 项目架构与路径指南

> 适用版本：3.24.0 / schema 3.67.0（2026-10-31）。产品对外叫 **Veynod**，仓库与内部标识仍沿用 `agentgraph`（板子命名空间、数据库历史名等，见文末"命名"）。

## 一句话

Veynod 是一个给多 agent 团队用的**共享黑板 + MCP 控制面**：人和 agent 在同一块看板上协作，文档带版本与信任分级，任务走状态机，执行端（runner）按 Loop 派活干活。后端是 FastAPI + Postgres(pgvector)，控制台是 Next.js 静态导出，CLI 是零依赖 Node ESM。

## 分层

```
浏览器 / Agent / CLI
        │  REST /v1  ·  MCP over HTTP /mcp/  ·  SSE
        ▼
   Caddy（TLS、反代、静态站点）
        ▼
 app/  FastAPI 单进程（uvicorn，无外部队列）
   ├─ api/          路由：public / human / admin / runner / blob …
   ├─ services/     业务：文档、任务、调度、令牌、事件、导出…
   ├─ engine/       黑板与协作引擎（看板状态、租约、依赖）
   ├─ mcp_server/   MCP 工具面（100 个工具，与 REST 同源）
   ├─ auth.py       身份、作用域、能力矩阵
   └─ database.py   连接、迁移清单、启动时按序执行
        ▼
  Postgres（pgvector）：文档 + 版本 + 向量、任务、令牌、事件、放置
```

关键设计取舍：

| 取舍 | 做法 | 原因 |
|---|---|---|
| 无消息队列 | 事件表 + SSE 流 | 单机部署，状态可重放，运维简单 |
| 迁移随进程启动 | `app/database.py` 的有序清单 + 咨询锁 | 部署即迁移，避免单独迁移步骤 |
| 前端不跑 Node 服务 | Next.js `output: export`，产物打进 API 镜像的 `/app/dashboard/out` | 一个容器搞定站点，Caddy 直接发静态 |
| 机器身份 | 一台电脑=一个 hub，两个工位（执行/审核），各自密钥 | 一台笔记本既能干活也能评审，且审计可区分 |

## 请求链路（以一次写入为例）

1. `app/api/*_routes.py` 收到请求 → `app/auth.py` 解出 `Principal`（人类会话 / 工作区密钥 / agent 分支令牌），按能力矩阵判权。
2. 路由调用 `app/services/*`，在 `app/database.py` 的会话里做业务写入，同时 `append_event` 记事件、`record_audit` 记状态差异。
3. `app/protocol_middleware.py` 在响应上加版本头（`Veynod-Version` / `Veynod-Build`，旧名 `AgentGraph-*` 仍兼容）与告警聚合。
4. 订阅方通过 `GET /v1/inbox/stream`（SSE）或 `/v1/events` 拿到 `event_seq`，凭游标续传。

## Loop 与执行端

```
任务 READY ──► 调度器（每分钟）──► runner_placements（suggested/offered/claimed）
                    │                        │
                    └── 无人可接 → unplaced   └── hub 认领 → 起 agent（claude/codex）
                                                        │
                                              评审（AUDITOR 工位）→ 合并 → 下一块
```

- 调度器：`app/services/scheduler.py`，按 `任务×步骤(work/review/merge)` 放置；索引 `uq_runner_placements_open` 是 `(task_id, step)`。
- 执行端：`veynod hub`（`cli/bin/up.mjs` + `hub` 子命令），一个进程守两条流，读 `~/.veynod/config.json` 里的多个身份。
- 自动化档位：`automation_modes`（manual / semi / full），**按工作区**生效，`automation.mode_for_task()` 还可能被图表自己的策略覆盖。

## 路径地图

### 后端 `app/`

| 路径 | 作用 |
|---|---|
| `app/main.py` | 应用装配：中间件、路由挂载、根页面（HTML/JSON 协商） |
| `app/api/` | 路由层。`human_routes.py`（控制台）、`admin_routes.py`（运维）、`v1_routes.py`（文档/任务/事件）、`runner_routes.py`（执行端）、`deploy_gate_routes.py`（部署门，当前停用） |
| `app/services/` | 业务层。核心：`documents.py`、`tasks.py`、`team.py`、`blackboard.py`、`scheduler.py`、`token_tree.py`、`automation.py`、`releases.py` |
| `app/engine/` | 黑板/协作引擎：看板列、租约、依赖重算 |
| `app/mcp_server/` | MCP 工具定义，与 REST 一一对应 |
| `app/models.py` | SQLAlchemy 模型（workspaces、agent_tokens、shared_documents、collab_tasks、events…） |
| `app/database.py` | 会话工厂 + **迁移清单**（新增迁移必须在此登记） |
| `app/config.py` | 全部配置项与环境变量 |
| `app/templates/` | 服务端渲染的落地页、演示页 |

### 数据 `sql/`

| 路径 | 作用 |
|---|---|
| `sql/migrations/v3_*.sql` | 69 个有序迁移；只增不改，版本号与 `app/database.py` 一致 |

### 前端 `dashboard/`

| 路径 | 作用 |
|---|---|
| `dashboard/app/` | Next.js App Router 页面：`page.tsx`（Loop 主面板）、`docs/`、`cli/`、`deploy/`、`login/`、`oauth/`、`growth/` |
| `dashboard/app/components/` | 35 个组件；重点：`loop-view.tsx`、`colony.tsx`（agent 头像）、`hosts-panel.tsx`（执行端电脑）、`agents.tsx`、`documents.tsx`、`diagram-view.tsx` |
| `dashboard/app/i18n.tsx` | 中英文案（新增文案两处都要加） |

构建时 `next build` 产出 `out/`，由 Dockerfile 复制进镜像的 `/app/dashboard/out`。

### CLI `cli/`

| 路径 | 作用 |
|---|---|
| `cli/bin/veynod.mjs` | 主命令：`init` / `up` / `hub` / `listen` / `runner` / `service` / `doctor` |
| `cli/bin/up.mjs` | `veynod up` 的实现：连接、接线 MCP、装服务 |
| `cli/bin/runner.mjs` | 执行端运行时（认领任务、起 claude/codex） |
| `cli/test/*.test.mjs` | node --test；其中三个在 Windows 上必然失败（权限位、worktree 脚本） |

发布链路：`cli/`（唯一源码）→ `scripts/export_public_bridge.sh <公开仓库>` →（推送）公开仓库 `veynod/veynod` → GitHub Actions `publish-npm.yml` 发 npm。**`VERSION` 常量在 `bin/veynod.mjs` 里是硬编码的，改版本要同时改 `package.json`。**

### 脚本与部署 `scripts/`、`deploy/`

| 路径 | 作用 |
|---|---|
| `scripts/deploy_tlines.sh` | 生产部署：备份 → 打回滚 tag → 切镜像 → 重启 → 11 项自检 |
| `scripts/deploy_gate.py` | 部署门 runner（已停用，路线图里） |
| `scripts/gen_agent_guide.py` | 生成 `docs/AGENT_GUIDE.md`（改动 API 后必须重跑，测试会校验） |
| `deploy/deploy-gate/` | 门的 systemd 单元 |

生产机：`/home/deploy/veynod`（非 git 检出，靠 tar 覆盖），容器 `veynod-api` / `veynod-db-1`，卷 `veynod_pgdata`，Caddy 配置在 `/opt/caddy/Caddyfile`。收件箱 SSE 不要被缓冲或在 60 秒空闲时切断：把仓库根目录 `deploy/proxy/Caddyfile` 里 `@sse` 那段并进生产 Caddyfile（nginx / Traefik 片段在同目录）。`listen` 的 systemd 单元仍是 `Restart=always`。

### 文档与测试

| 路径 | 作用 |
|---|---|
| `docs/PROJECT_ARCHITECTURE.md` | 更长的架构说明（英文，历史文档） |
| `docs/AGENT_GUIDE.md` | **生成物**，不要手改 |
| `docs/RELEASE*` / `app/services/releases.py` | 发布说明在 `RELEASE_NOTES` 列表里，版本号必须与 `app/config.py` 一致 |
| `tests/` | 95 个测试文件；集成测试需要 `TEST_DATABASE_URL` 指向一个一次性 pgvector |

## 命名

- 对外产品名、页面、JSON-LD、工作区显示名一律 **Veynod**。
- 仓库、数据库历史名、板子命名空间 `agentgraph`、`doc://agentgraph/...` 仍是旧名——改名会切断历史链接，暂缓。
- 响应头已是 `Veynod-Version` / `Veynod-Build`，旧名保留一个版本做兼容。

## 常用命令

```bash
# 本地测试（需要一次性数据库）
docker run -d --name t -e POSTGRES_USER=gate -e POSTGRES_PASSWORD=gate -e POSTGRES_DB=gate -p 55432:5432 pgvector/pgvector:pg16
TEST_DATABASE_URL=postgresql+asyncpg://gate:gate@localhost:55432/gate .venv/Scripts/python -m pytest tests/ -q

# 发布
git archive <sha> | tar -x -C <tmp> && docker build -t veynod:<ver>-veynod-<sha> <tmp>
docker save ... | gzip > x.tar.gz && scp → ssh 'docker load'
ssh deploy@<box> 'cd /home/deploy/veynod && EXPECTED_SCHEMA=<schema> scripts/deploy_tlines.sh deploy-loaded'
```
