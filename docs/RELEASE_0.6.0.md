# 发布清单：服务端 + CLI 0.6.0（nerve 唯一派发、只走 Kernel）

这一版是**两端同时切换**的破坏性变更，按顺序做，不要跳步。

| 组合 | 结果 |
|---|---|
| 新服务端 + CLI 0.6.0 | 正常 |
| 新服务端 + 旧 CLI（≤ 0.5.x） | 电脑能连上、心跳正常，但**收不到任何工作**。服务端不会给它派活，控制台和 doctor 会点名，提示升级 |
| 旧服务端（生产现为 `88626e3`）+ CLI 0.6.0 | **收不到任何工作**：旧服务端没有 `/v1/ws/nerve` |

所以顺序固定为：停止派新活 → 部署服务端 → 发布 CLI 0.6.0 → 逐台升级电脑 → 恢复派活。
中间那段时间电脑不接新活，已经在跑的任务不受影响。

部署形态、禁忌与回滚的总说明见 [PRODUCTION_OPERATIONS.md](PRODUCTION_OPERATIONS.md)。
下文命令都在 VPS 的 `/home/deploy/veynod-monorepo` 下执行，`TAG` 是这次的提交短 SHA。

---

## 0. 动手之前（在开发机上）

- [ ] 分支 `claude/cool-cerf-it7a0j` 已合并到 `main`，CI（`.github/workflows/ci.yml`）全绿。
      本地等价命令：`scripts/test_all.sh`。
- [ ] `AgentGraph/cli/package.json` 的 `version` 与 `AgentGraph/cli/bin/veynod.mjs` 的 `VERSION` 都是 `0.6.0`。
- [ ] 记下 `TAG=$(git rev-parse --short HEAD)`，以及当前生产标签（回滚用，现为 `aa0ce75` / API `monorepo-aa0ce75`）。

## 1. 停止派新活（控制台）

- [ ] Governance → Automation 从 **full** 改为 **semi**：调度只建议，不再派活。
- [ ] 等正在跑的任务结束，或在 Loop 里确认没有处于 `offered` / `claimed` 的放置。
      已经进入 Kernel 门禁的提交会照常跑完，不用等。

## 2. 备份（VPS）

`veynod-db-1` 由旧 compose 管理，不在 unified 文件里。

```bash
docker exec veynod-db-1 pg_dump -U veynod -Fc veynod         > backups/veynod-$TAG.dump
docker exec veynod-db-1 pg_dump -U veynod -Fc veynod_kernel  > backups/kernel-$TAG.dump
docker exec veynod-db-1 pg_dump -U veynod -Fc veynod_runtime > backups/runtime-$TAG.dump
```

- [ ] 三个 dump 都非空。仓库目录（`REPOS_DIR`）要和 `veynod_kernel` 一起备份。

## 3. 构建三个镜像（都从仓库根目录）

```bash
git fetch origin main && git checkout --detach "$TAG"
docker build -f deploy/Dockerfile.kernel  -t veynod-kernel:$TAG .
docker build -f deploy/Dockerfile.runtime -t veynod-runtime:$TAG .
docker build -f deploy/Dockerfile.api     -t veynod:monorepo-$TAG .
```

- [ ] 三个镜像都构建成功。旧的 `AgentGraph/Dockerfile` 已删除，因为它无法打包根目录的 `contracts/`。

## 4. 部署前检查：仓库绑定

现在代码只经 Kernel：带仓库的任务**必须**能解析到 Kernel 绑定，否则不会被派出去，
控制台显示"仓库还没接到 Kernel"。以前"借用同工作区第一个绑定"的回退已删除，
因为它会把补丁合进别的仓库。

```bash
docker exec -i veynod-db-1 psql -U veynod -d veynod <<'SQL'
SELECT t.workspace_id, t.project, t.repo, count(*) AS open_tasks
FROM collab_tasks t
WHERE t.repo IS NOT NULL
  AND t.status IN ('TODO','READY','IN_PROGRESS','REVIEWING')
  AND NOT EXISTS (SELECT 1 FROM kernel_bindings b
                  WHERE b.workspace_id = t.workspace_id AND b.status = 'ACTIVE'
                    AND b.repo_path = t.repo)
GROUP BY 1, 2, 3 ORDER BY open_tasks DESC;
SQL
```

- [ ] 结果为空；或者 `.env` 里设了部署默认绑定（`KERNEL_WORKSPACE_ID`），它就是这些仓库要去的 Kernel。
- [ ] 每一行都处理过：要么在项目里绑定该仓库（`POST /v1/projects/{p}/repositories`），
      要么接受这些任务在绑定前不被派出。

## 5. 环境变量

下列变量已删除。留在 `.env` 里**不会**导致启动失败（两边都是 `extra="ignore"`），
但它们已经不起作用，清掉以免误导：

- [ ] 产品面：`CODE_ENGINE_LEGACY_DIRECT`、`KERNEL_DELIVERY_DEFAULT`、`KERNEL_PROVISION_ON_CREATE`、`ENABLE_INLINE_BACKGROUND_LOOPS`
- [ ] Kernel：`VN_LEGACY_SUBMIT_MODE`、`VN_DEBUG_DASHBOARD`
- [ ] 可选新增：`KERNEL_GATE_HOLD_SECONDS`（默认 3600），门禁持有提交时租约回收等待的上限。
- [ ] 不再有独立的产品 worker 容器：所有后台循环都在 `api` 进程里，因为 nerve 套接字在那里。
      如果手工跑过 `python -m app.worker`（产品面那个），停掉它。Kernel 的 gate worker 不受影响。

## 6. 迁移（先 Kernel，后产品面）

迁移只做扩展（expand-only），旧镜像仍能在新 schema 上运行。会补上生产缺的
`v4_017`（Kernel）与 `v3_80`、`v3_81`（产品面）。

```bash
export VEYNOD_TAG=$TAG COMPOSE_FILE=docker-compose.unified.yml
./scripts/migrate_all.sh
```

- [ ] 输出以 `all schemas are at their current version` 结尾；任何一步失败就停在这里，不要启动服务。

## 7. 逐个启动服务

**禁忌**：不要对 unified 文件执行 `down -v` 或 `up --remove-orphans`，否则会连数据库容器和数据卷一起删掉。
只用 `up -d --no-deps <service>`。

```bash
docker compose -f docker-compose.unified.yml up -d --no-deps kernel-api kernel-gate
docker compose -f docker-compose.unified.yml up -d --no-deps runtime-api runtime-worker
# 产品面镜像名是 veynod:monorepo-$TAG，由同一个 VEYNOD_TAG 决定
docker compose -f docker-compose.unified.yml up -d --no-deps api
```

## 8. 服务端验收

```bash
curl -fsS https://veynod.com/healthz/deep            # database / agent_runtime / kernel 全 ok
docker exec veynod-kernel-api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8001/healthz').read())"
docker logs veynod-api --since 5m 2>&1 | grep -iE "Traceback|nerve reaper pass failed|ImportError" || echo clean
```

- [ ] `/healthz/deep` 返回 200。
- [ ] api 日志里没有 Traceback。nerve 回收循环这一版才真正开始运行，以前它启动即崩。
- [ ] 控制台 Loop → 执行端电脑：仍是旧 CLI 的电脑显示"已经收不到工作了…"，
      `GET /v1/runner/doctor` 的 `cli_version` 检查点名这些电脑。

## 9. 发布 CLI 0.6.0

npm 包 `veynod` 的唯一源码是 `AgentGraph/cli`，由公开仓库 `veynod/veynod` 的工作流发布。

```bash
scripts/export_public_bridge.sh ../veynod-public   # 会先跑 CLI 测试
cd ../veynod-public && git add -A && git commit -m "veynod 0.6.0: nerve is the only way work arrives"
git push origin main
# 然后按公开仓库 publish-npm.yml 的触发方式（标签或手动运行）发布 npm
npm view veynod version                              # 应为 0.6.0
```

- [ ] `npm view veynod version` 返回 `0.6.0`。
- [ ] 本仓库的 `.github/workflows/publish-cli.yml`（推 `cli-v*` 标签触发）只有在 npm 为
      `MuadibUsul/veynod` 配好了 trusted publishing 时才用；两条发布路径**只用一条**。

## 10. 逐台升级电脑

在每一台跑任务的电脑上执行：

```bash
npm install -g veynod@0.6.0
veynod up --run-tasks          # 同一台服务器、钥匙文件还在：复用已保存的身份，不再要求批准；重装 veynod-hub 服务
veynod doctor                  # 全绿
```

- [ ] 控制台里这台电脑的"收不到工作"提示消失，`runner_version` 为 `0.6.0`。
- [ ] 旧的 `-review` 座位不用处理：它留在配置里，但 hub 不会再启动它，面板也不再显示。
- [ ] `veynod up` 装的服务叫 `veynod-hub`；以前单独装过 `veynod service install --runner` 的叫
      `veynod-runner`。两种都只需 `npm install -g veynod@0.6.0` 后重启该服务
      （Linux：`systemctl --user restart veynod-hub` 或 `veynod-runner`；macOS/Windows 再跑一次 `veynod up --run-tasks`）。

## 11. 恢复派活与冒烟

- [ ] Automation 改回 **full**。
- [ ] 冒烟 1：建一个**不带仓库**的小任务。期望一台电脑接走做完，任务进入 REVIEWING；
      如果没有别的电脑能评审，它出现在"需要你"里，批准后变为 DONE。
- [ ] 冒烟 2：在已绑定的仓库上建一个小改动任务。期望电脑日志出现
      `kernel worktree cwd=… pool=worktree_pool`、`submission_service id=`、
      `verification_gate`、`merge_coordinator sha=`，提交合并后任务进入"需要你"。
- [ ] `GET /v1/tasks/{key}/trace` 能看到这两个任务的产品状态、Run 与 Kernel 事实；
      `GET /v1/runner/placements?task_key=<key>` 能看到每个放置的 step、电脑和结局。

## 12. 文档收尾（npm 上真的是 0.6.0 之后）

- [ ] 根 `README.md`、`AgentGraph/cli/README.md`、`AgentGraph/cli/llms.txt` 里
      "npm latest 是 0.5.2（2026-11-02 核对）"这类事实改成 0.6.0 和新的核对日期。
- [ ] `PRODUCTION_OPERATIONS.md` §0 的镜像标签改成 `$TAG`。

---

## 回滚

- **服务端**：`VEYNOD_TAG` 改回上一个标签（API 镜像同理），按第 7 步逐个 `up -d --no-deps`。
  迁移是 expand-only，不需要回滚数据库，dump 只在数据出错时用。
- **电脑**：旧服务端没有 nerve，所以回滚服务端后，电脑也要回到旧 CLI：
  `npm install -g veynod@0.5.2 && veynod up --run-tasks`。
- 已经由 Kernel 合并的提交不受回滚影响：它们在 git 里，产品面重放事件后会补上投影。
