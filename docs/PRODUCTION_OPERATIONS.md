# Veynod 生产运维手册（Qualification 版）

> 与 [UNIFIED_OPERATIONS.md](UNIFIED_OPERATIONS.md) 配套：那份讲部署形态与参数，本份讲
> **如何诊断一个任务、如何演练恢复、以及本轮验收实测过的操作**。

---

## 1. 一次诊断只需要一个 ID

```bash
GET /v1/tasks/{task_key}/trace
```

返回四段（只读，不触发任何状态变更）：

| 段 | 内容 | 来源 |
|---|---|---|
| `task` | 状态、版本、项目、仓库 | `collab_tasks` |
| `ids` | tenant / workspace / project / task_key / run_id / trace_id / kernel_workspace_id / kernel_task_id / submission_id / commit_sha | 关联列 `collab_tasks.correlation` |
| `run` | Run 状态、usage、最近 40 步、外部等待、事件数 | Agent Runtime |
| `kernel` | submission、merge intent、delivery、5 阶段门禁 | Veynod Kernel（按任务绑定选实例） |
| `timeline` | 产品事件 + Runtime 事件 + Kernel 提交/交付，按时间合并 | 三个平面 |

一个不可达的平面只会在自己的段里返回 `error`，不会让整个视图失败。

辅助入口：`GET /v1/tasks/{key}/execution`（任务详情 UI 使用）、
`GET /v1/runs/{run_id}/steps|external|events`（Runtime 内部面）。

---

## 2. 健康检查

| 端点 | 通过条件 | 用途 |
|---|---|---|
| `GET /healthz` | 进程存活 | liveness |
| `GET /readyz` | 数据库 + 嵌入配置 | readiness |
| `GET /healthz/deep` | 数据库 + Runtime + Kernel 全部可达 | **部署验收**；503 表示执行链不可用 |
| `GET kernel/healthz` | `core.available` 表示 Rust/回退 | Kernel 能力 |
| `GET :8030/metrics` | Gate Worker 指标 | 门禁积压 |
| `GET :8031/metrics` | Runtime 池快照 | Worker 数、停止状态 |

---

## 3. 本轮实测过的恢复操作

### 3.1 有界启动回填

`backfill_all_workspaces` / `backfill_auto` 每次启动最多处理 500 行（幂等，下次启动继续）。
历史库曾累积 1,526 个 workspace 导致启动超过 45 秒；修复后启动时间不随租户数增长。

### 3.2 Worker 崩溃（真实 SIGKILL）

`scripts/chaos_suite.py worker_kill`：杀掉正在执行工具调用的 worker，重启后
新的 worker 以 `lease_epoch + 1` 接管；旧 worker 的任何写入都会被 fencing 拒绝。
**验收结果：REVIEWING，单次 commit。**

### 3.3 Gate Worker 崩溃

`gate_kill`：在 submission 已入库、门禁处理中途杀进程；重启后同一条 submission
继续完成。**验收结果：REVIEWING，1 submission / 1 commit / 1 delivery。**

### 3.4 Kernel API / Runtime API 重启

`kernel_restart` / `runtime_api_restart`：分别在提交后、等待门禁时重启。
**验收结果：都恢复到 REVIEWING，交付记录唯一。**

### 3.5 PostgreSQL 连接中断

`pg_blip`：终止该库所有后端连接（5 条），Worker 与 API 依靠 `pool_pre_ping` 重连。
**验收结果：REVIEWING。**

### 3.6 重复事件

`duplicate_event`：任务完成后把同一 terminal event 再投递 3 次。
**验收结果：状态不变、交付记录仍为 1。**

### 3.7 用户取消

`cancel`：运行中取消，Kernel 侧任务同步 `CANCELLED`（Requires `KERNEL_SERVICE_KEY`）。
**验收结果：产品任务 CANCELLED、Kernel 任务 CANCELLED。**

完整矩阵：[CHAOS_TEST_MATRIX.md](CHAOS_TEST_MATRIX.md)。

---

## 4. 备份与恢复演练（实测）

```bash
# 演练（不触碰源库）：dump -> 创建 *_drill -> restore -> 逐表行数比对 -> 删除
python scripts/restore_drill.py --source-prefix veynod_chaos_
```

生产操作：

```bash
docker exec <db> pg_dump -U veynod -Fc veynod          > veynod.dump
docker exec <db> pg_dump -U veynod -Fc veynod_kernel   > kernel.dump
docker exec <db> pg_dump -U veynod -Fc veynod_runtime  > runtime.dump
# 恢复
docker exec -i <db> pg_restore -U veynod -d veynod --clean --if-exists < veynod.dump
```

注意：

* **仓库（REPOS_DIR）必须与 `veynod_kernel` 一起备份**：数据库 head 与 git 不一致时，
  Kernel 启动会以 git 为准并告警。
* `veynod_runtime` 丢失只影响进行中的 Run；已 REVIEWING/DONE 的任务不受影响。
* 演练记录：[evidence/restore_drill.txt](evidence/restore_drill.txt)。

---

## 5. 常见故障处置

| 症状 | 先看 | 处置 |
|---|---|---|
| 任务 READY 不动、有 `active_run_id` | `/trace` 的 `run.status` | Runtime 不可用 → 恢复后 `GET /v1/tasks/{key}/run` 投影一次 |
| Run `WAITING_EXTERNAL` 很久 | `runtime_wakeups` 未消费行、gate 是否在跑 | 起/重启 gate；`POST /v1/runs/{id}/wakeups/apply` |
| Run `WAITING_TOOL` 且租约过期 | 旧 worker 是否已死 | 新代码在 claim 时自动恢复；检查 worker 是否在跑 |
| Run `FAILED`，`last_error` 含 `model unavailable` | 模型 Provider | 修复 key/网络后重新 `POST /run`（历史 Run 不可复活） |
| 提交 409 `submission_in_progress` | 已有 submission 在门禁中 | 等它出结果；同一 patch 重试会得到 202 去重 |
| 门禁一直 QUEUED | gate 日志、worktree 磁盘 | 重启 gate；检查 `VN_GATE_MEMORY_MB`（<1024 会使 pyright SIGABRT） |
| 任务 REVIEWING 但门禁缺失 | `/trace` 的 `kernel.gate_results` | 不应发生（证据门控）；查 `lifecycle_projections` 与 `/evidence`，禁止手工补 PASS |

---

## 6. 容量与调优（本轮实测见 CAPACITY_BASELINE.md）

| 参数 | 作用 |
|---|---|
| `RUNTIME_WORKER_COUNT` | 并发 Agent 步骤数（受模型配额与写集并发限制） |
| `VN_GATE_WORKERS` | 并行门禁数；门禁是 CPU 密集，通常最先饱和 |
| `RUNTIME_WORKER_LEASE_SECONDS` | Run 租约；崩溃恢复时间上限 |
| `VN_GATE_MEMORY_MB` | 单个门禁命令的虚拟内存上限（pyright 需 ≥1 GiB） |
| `VN_ADMISSION_*` | 门禁队列准入；积压时启用 defer |
| `RUNTIME_CONTEXT_TOKEN_BUDGET` | 上下文预算（模型成本主因） |

扩缩容顺序：**gate → runtime-worker → api**。Kernel API 是薄层，通常最后才需要加。

---

## 7. 升级与回滚

```bash
./scripts/migrate_all.sh            # 幂等；失败即停
docker compose up -d --build
curl -fsS /healthz/deep
python scripts/e2e_unified.py       # 冒烟：一条真实全链路
```

回滚：保留上一版本镜像标签，`docker compose` 指回旧 tag 即可；所有迁移都是
expand-only（新列有默认值），旧代码可直接运行。详见
[UNIFIED_MIGRATION_PLAN.md](UNIFIED_MIGRATION_PLAN.md) §6。

---

## 8. 运行本轮验收脚本

```bash
python scripts/chaos_suite.py                      # 9 个真实故障场景
python scripts/capacity_unified.py --profile fast --levels 10,25,50,100
python scripts/capacity_unified.py --profile real --levels 10,25
python scripts/verify_rust_core.py                 # Rust 镜像真实执行
python scripts/restore_drill.py --source-prefix veynod_chaos_
python scripts/e2e_unified.py --real-model         # 真实 DeepSeek
```
