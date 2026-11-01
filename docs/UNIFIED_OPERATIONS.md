# Veynod 迁移与运维手册

面向部署 Veynod 的工程师。命令默认在仓库根目录执行。

---

## 1. 部署形态

```
                 ┌──────────────┐
  用户 ── 8000 ──▶│  api         │  唯一对外服务（dashboard + REST + MCP）
                 └──┬────┬──────┘
                    │    │
        ┌───────────┘    └──────────────┐
        ▼                               ▼
 ┌──────────────┐               ┌───────────────┐
 │ runtime-api  │◀── worker ───▶│ kernel-api    │
 └──────┬───────┘               └──────┬────────┘
        │                              │ gate worker
        └────────────┬─────────────────┘
                     ▼
              PostgreSQL 16 + pgvector
        veynod / veynod_kernel / veynod_runtime
```

* 只有 `api` 发布端口。Runtime 与 Kernel 在 internal 网络内，用服务名互相访问。
* 一个 PostgreSQL 集群，三个数据库（bounded context 隔离，不做跨库外键）。
* 所有进程 `restart: unless-stopped`；Runtime worker 另有进程内 supervisor。

---

## 2. 首次部署

```bash
cp deploy/.env.unified.example .env
# 必填：POSTGRES_PASSWORD / ADMIN_TOKEN(>=32) / KERNEL_ADMIN_KEY /
#       KERNEL_SERVICE_KEY / RUNTIME_API_KEY / KERNEL_RUNTIME_TOKEN /
#       MODEL_BASE_URL / MODEL_API_KEY / MODEL_NAME

docker compose build
./scripts/migrate_all.sh          # 可选：先跑完迁移再起服务
docker compose up -d
curl -fsS http://127.0.0.1:8000/healthz/deep | jq .
```

### 首次接入一个仓库

1. `POST /v1/admin/workspaces`（Kernel，`X-Admin-Key`）→ `workspace_id`。
2. `POST /v1/admin/tokens`（Kernel）→ `vnk_...`，填入 `.env` 的 `KERNEL_RUNTIME_TOKEN`，重启 `runtime-worker`。
3. 产品面创建项目与仓库绑定（见 README §4）。绑定后该项目的任务自动路由到对应 Kernel workspace。
4. 确认仓库是 bare（或 `VN_ALLOW_NONBARE_REPO=1`），`main` 分支存在且包含待实现的 stub 与冻结测试。

### 健康与就绪

| 端点 | 含义 |
|---|---|
| `GET /healthz` | 进程存活 |
| `GET /readyz` | 数据库与嵌入配置 |
| `GET /healthz/deep` | 数据库 + Runtime + Kernel 全部可用（503 表示执行链不可用） |
| `GET kernel:8001/healthz` | Kernel 进程 |
| `gate:8030/metrics` | Gate Worker 指标 |
| `runtime-worker:8031/metrics` | Worker 池快照 |

---

## 3. 升级

1. 备份（见 §5）。
2. `git pull && docker compose build`。
3. `./scripts/migrate_all.sh`（幂等；失败即停，不启动新版本）。
4. `docker compose up -d`（滚动：先 kernel-gate、runtime-worker，再 api）。
5. `curl -fsS /healthz/deep`，跑一次冒烟任务（`scripts/e2e_unified.py` 或产品 UI）。

迁移策略：expand → migrate → contract。新列/新表都带默认值，旧版本代码可以继续读；
删除旧结构至少要跨两个版本。

---

## 4. 回滚

```bash
docker compose down
git checkout <previous-tag>
docker compose build && docker compose up -d
```

* 新增的表和列可以保留（旧代码不读）。
* 如需彻底清理，仅删本版本新增的结构（顺序：`lifecycle_projections`、
  `runtime_wakeups`、`runtime_tool_audit`、`kernel_leases`、`external_invocations`，
  再删 `collab_tasks.correlation/active_run_id`）。不要删历史任务、提交与证据。
* Kernel 的 `X-Workspace-Id` 路由在缺省时回落旧行为，回滚无需改协议。

---

## 5. 备份与恢复

```bash
# 逻辑备份（三库一次）
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc veynod          > veynod.dump
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc veynod_kernel   > kernel.dump
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc veynod_runtime  > runtime.dump

# 恢复
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d veynod --clean --if-exists < veynod.dump
```

注意：

* 仓库本身（`REPOS_DIR`）是代码事实，必须与 `veynod_kernel` 数据库一起备份；
  只恢复数据库会导致 workspace head 与 git 不一致（Kernel 启动时会以 git 为准自愈并告警）。
* `veynod_runtime` 丢失后，进行中的 Run 无法续跑；已 REVIEWING/DONE 的任务不受影响。
* AgentGraph 自带 `BACKUP_DIR` 定时备份（原 3.x 能力），与本手册的集群级备份互补。

---

## 6. 故障处理

| 症状 | 检查 | 处理 |
|---|---|---|
| `/healthz/deep` 503，`kernel: unavailable` | Kernel 容器日志、DB 连接 | 重启 `kernel-api`；确认 `VN_ADMIN_API_KEY` 长度 ≥32 |
| Run 停在 `WAITING_EXTERNAL` | `runtime_wakeups` 是否有未消费行；`kernel-gate` 是否在跑 | 启动/重启 `kernel-gate`；`POST /v1/runs/{id}/wakeups/apply` 手动推进一次 |
| Run 停在 `RUNNING` 但无进展 | worker 日志、`/metrics` 的 `ids` | 重启 `runtime-worker`；租约到期后自动重新领取 |
| 任务一直 `READY` 且有 `active_run_id` | Runtime 是否可用 | 恢复 Runtime 后 `GET /v1/tasks/{key}/run` 触发一次投影 |
| Run `FAILED` 且 `last_error` 是 `AUTH` | `KERNEL_RUNTIME_TOKEN` 是否绑定该 workspace | 重新签发 token 并更新 `.env` |
| Gate 一直 `QUEUED` | `patch_submissions` 状态、gate 日志 | gate worker 崩溃会由 `restart` 拉起；检查 worktree 磁盘空间 |
| 任务在 REVIEWING 但缺失门禁 | 不应发生（证据门控） | 查 `lifecycle_projections` 与 Kernel `/evidence`；不要手工补 PASS |
| 端口冲突 | `docker compose ps` | 8000 只应属于 `api` |

### 常见根因

* **token 与 workspace 不匹配**：Kernel 直接 403（不会静默串到别的仓库）。
* **仓库路径未挂载**：`kernel-api`/`kernel-gate` 的 `REPOS_DIR` 必须指向同一份仓库。
* **模型不可用**：Runtime 把模型错误按 `TRANSPORT` 重试，超过预算后 Run FAILED；
  `/v1/provider-health` 可看失败率。

---

## 7. 容量与调优

| 参数 | 作用 | 建议 |
|---|---|---|
| `VN_GATE_WORKERS` | 并行门禁数 | 与 CPU 核数同阶；门禁是 CPU/IO 密集 |
| `RUNTIME_WORKER_COUNT` | 并行 Agent 步骤数 | 受模型配额与 Kernel 写集并发限制 |
| `RUNTIME_WORKER_LEASE_SECONDS` | Run 租约 | 默认 120；工具调用慢时提高 |
| `VN_DB_POOL_SIZE/MAX_OVERFLOW` | Kernel 连接池 | 各进程之和 < `max_connections` |
| `VN_ADMISSION_*` | 准入控制 | 门禁队列过深时启用 defer 模式 |
| `RUNTIME_CONTEXT_TOKEN_BUDGET` | 上下文预算 | 大仓库提高，但会增加模型成本 |

扩缩容顺序：先 `kernel-gate`（门禁积压）→ `runtime-worker`（步骤积压）→ `api`（HTTP）。
容量基线与已知瓶颈见 `docs/architecture/CAPACITY_BASELINE.md` 与验收报告。

---

## 8. 多租户与多仓库

* 每个 Tenant 有多个 Workspace；每个 Workspace 有多个 Project；每个 Project 可绑定多个 Repository。
* `kernel_bindings` 决定一个 `(workspace, project, repo)` 使用哪个 Kernel 实例/workspace。
* Kernel token 绑定 workspace；跨 workspace 的请求直接 403，不存在隐式回落。
* 同一 Kernel 承载多个 workspace（`X-Workspace-Id`）；分片时不同 binding 指向不同实例，产品无感。

---

## 9. 运维禁区

* 不要直接 `UPDATE collab_tasks.status`：执行态只经过 `task_lifecycle`。
* 不要为"让任务继续"手工写 PASS 门禁结果。
* 不要把 Kernel admin key 或 runner token 交给模型/浏览器。
* 不要删除 `patch_submissions`/`merge_intents`/`delivery_records`：它们是证据。
* 不要在 `kernel-gate` 运行中修改仓库工作树。
