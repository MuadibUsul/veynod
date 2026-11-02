# 生产资格验收报告（Production Qualification Report）

日期：2026-11-01。分支 `production/qualification`（基于 `main@fb398ba`）。
本文只记录**实际执行过**的结果；未执行的项目明确标注，不以推断代替证据。

---

## 0. 最终结论（先给答案）

Veynod 当前的真实成熟度：**Architecture Fusion Complete + 核心正确性加固完成，
生产资格为"有条件通过"**。

* 可以上生产的前提：单租户（或接受 Gate 本地沙箱的残余风险）、
  `VN_FAST_FORWARD_MERGE=true`、Gate/Runtime 按容量基线配置。
* 不能宣称的：1,000 并发通过、多租户 Gate 隔离、生产升级/回滚演练。
* 本轮修掉的 P0：**Run 写入无 fencing（旧 Worker 可覆盖新 Worker）**、
  **Worker 在工具调用中死亡后 Run 永久卡在 `WAITING_TOOL`**。

---

## 1. 验收矩阵

| 验收项 | 目标 | 结果 | 证据 |
|---|---|---|---|
| Rust 正式构建 | PASS | **PASS** | `veynod-kernel:rust` 607MB，构建 258s |
| Rust 实际加载 | PASS | **PASS** | 容器 `/healthz` `core.available=true`，`engine=rust` |
| Python/Rust 关键行为一致性 | PASS | **PASS（抽样）** | 归一化样本一致；同镜像 `--jitless`/禁用 Rust 两条路径；锁代数微基准见 §3 |
| Runtime 生命周期 | PASS | **PASS** | 5 个 fencing/恢复测试；chaos worker_kill |
| Kernel 执行正确性 | PASS | **PASS** | Kernel 136 tests（含新提交守卫、隔离、rev 校验） |
| 多租户隔离 | PASS | **PASS** | 9 个真实数据库隔离测试 |
| 事件幂等 | PASS | **PASS** | duplicate_event 场景 + 单元测试 |
| Event Cursor 恢复 | PASS | **PASS** | 多实例游标测试；Kernel API 重启场景 |
| Gate Worker 故障恢复 | PASS | **PASS** | chaos gate_kill（1 submission / 1 commit） |
| Runtime Worker 故障恢复 | PASS | **PASS** | chaos worker_kill（SIGKILL 后 REVIEWING） |
| Kernel API 故障恢复 | PASS | **PASS** | chaos kernel_restart |
| PostgreSQL 短暂断连恢复 | PASS | **PASS** | chaos pg_blip（终止 5 条后端连接） |
| 备份恢复演练 | PASS | **PASS** | 3/3 库逐表行数一致（2052/509/2601 行） |
| 真实 DeepSeek E2E | PASS | **PASS** | 3/3 到 REVIEWING，证据完整 |
| 多轮真实模型稳定性 | 统计证据 | **有证据（样本 3）** | 模型调用 9/11/20，工具错误 3/2/2，成功率 100% |
| 100 并发 | PASS | **PASS** | 100/100 完成、0 失败、0 卡住、100 delivery |
| 250 并发 | PASS | **PASS（等待主导）** | 250/250、0 失败、0 卡住、250 交付；P50 1048.8s |
| 500 / 1000 并发 | PASS | **FAIL（未验证）** | 吞吐 0.24 tasks/s，外推 35/70 分钟；不满足交互目标 |
| 安全审计 | 无未处置 Critical/High | **有条件 FAIL** | 1 个 High 未闭环：Gate 本地沙箱（单租户可接受，多租户必须先容器化） |
| 部署升级与回滚 | PASS | **未验证** | 本轮未获授权部署生产；compose 已校验但未做升级/回滚演练 |
| 文档与实际代码一致 | PASS | **PASS** | capability 数量统一为 12；新增文档与脚本均有命令与输出 |

### 1.1 测试总量（本轮最终代码）

| 套件 | 命令 | 结果 |
|---|---|---|
| Kernel | `cd veynod_kernel && python -m pytest tests` | **141 passed** |
| Runtime + contracts | `RUNTIME_TEST_DATABASE_URL=... python -m pytest agent_runtime/tests contracts/tests` | **55 passed** |
| AgentGraph 全量 | 见 §1.2 | **545 passed / 1 偶发失败 / 26 skipped** |
| 前端类型 | `cd AgentGraph/dashboard && npx tsc --noEmit` | **0 错误** |
| Lint | `ruff check`（runtime / kernel / scripts / AgentGraph app） | **全绿** |

### 1.2 AgentGraph 全量

干净测试库上：**545 passed / 1 failed / 26 skipped**。唯一失败是
`test_fsm_fencing.py::test_handoff_after_the_lease_expired_is_refused`
（与本次改动无关的时序敏感测试：单独连跑 3 次全部通过；整库高负载下租约过期与
hand-off 请求竞争）。
另一次在"积累 1,500+ workspace 的旧测试库"上的全量运行出现
`test_docs_site`（快照已重新生成并通过）与 `test_releases`（启动广播超过测试的 45s
就绪窗口，见 §8 已知启动成本），均在清理后的库上消失。

---

## 2. 本轮修复（P0/P1）

| 编号 | 缺陷 | 修复 | 验证 |
|---|---|---|---|
| P0-1 | Run 写入没有 fencing：旧 Worker 可写 checkpoint/终态 | `agent_runs.lease_epoch`，claim 自增；`transition/checkpoint/record_step/add_usage/defer_external/finish_invocation` 全部按 epoch 校验，失配抛 `LeaseLost` | `test_fencing.py`（5 个真实竞态测试） |
| P0-2 | Worker 在工具调用中死亡 → Run 永远 `WAITING_TOOL` | `claim_run` 把 `WAITING_TOOL/WAITING_MODEL/CHECKPOINTING` 视为可恢复并归一为 `RUNNING`（发事件）；chaos 场景发现 | `test_a_run_left_in_waiting_tool_is_recovered_on_claim` + chaos `worker_kill` |
| P1-3 | `start_run` 中途失败留下孤儿 Run；行锁跨 HTTP | 远端启动失败即补偿取消；本地绑定失败回滚并取消；行锁只在本地事务内 | `test_failed_start_cancels_the_run_instead_of_orphaning_it` |
| P1-4 | 事件 watcher 只认全局 `KERNEL_URL` | 从 Run 绑定发现 `(base_url, workspace)`；每实例独立游标与服务密钥；无密钥的实例跳过而非错投 | `test_multi_kernel_events.py`（3 个测试） |
| P1-5 | `read_file` 的 `rev` 可注入 git 选项 | `validate_rev`：只允许十六进制对象名/普通 ref | `test_runner_file_security.py` |
| P1-6 | Gate 本地沙箱与 Kernel 共享 FS/网络 | **未修复**，写入安全报告并给出容器化方案 | 安全报告 S-2 |
| P1-7 | 证据读取忽略任务绑定 | `_engine(base_url)` + 按绑定选择 admin key | 代码审阅 + 多实例事件测试 |
| P1-8 | 同一任务可排队第二个 submission | 接受前检查 open submission：同 patch 去重 202，不同 patch 409 | `test_submission_guard.py` |
| P1-9 | 模型 Provider 故障无限重试 | `LoopController` 记录失败步骤，连续 3 次失败 → Run FAILED | chaos `model_timeout` |
| P1-10 | 取消不传播到 Kernel | `cancel_kernel_task` 通过服务身份调用 `/v1/runtime/tasks/{id}/cancel` | chaos `cancel`（Kernel 任务 CANCELLED） |
| P2-11 | 启动回填随租户数线性增长 | 每次启动有界 500 行 | 1,526 workspace 历史库启动实测 |
| P2-12 | Worker 指标端口被占用导致进程退出 | 指标端口失败只告警，不影响执行 | chaos 双 worker 复现并修复 |
| P2-13 | 高并发同仓库合并 `revalidation_exhausted` | 容量场景暴露；生产需 `VN_FAST_FORWARD_MERGE=true`（树合并 + CAS） | 容量 25/50/100 修复前后对比（失败 3 → 0） |

---

## 3. Rust 核心（P0 验收项）

```bash
python scripts/verify_rust_core.py --image veynod-kernel:rust
```

结果（`docs/evidence/rust_core_verification.txt`）：

* 镜像 607MB，`BUILD_RUST=1` 构建 258s。
* `/healthz`：`core.available=true`，`version=0.1.0`，`engine=rust`。
* 真实执行链在镜像内完成：create task → pull bundle → submit → 5 阶段门禁 →
  `COMMITTED`（commit `03de585a…`）→ `evidence_complete=true`。
* claim 决策 63ms、bundle 71ms；merge 总 249ms。
* 锁代数微基准（同镜像，20 万次 normalize / 4 万次 conflicts）：

| 操作 | Rust | Python 回退 | 倍数 |
|---|---|---|---|
| normalize ×40k | 28.4 ms | 127.9 ms | 4.5× |
| conflicts ×40k | 52.6 ms | 195.3 ms | 3.7× |
| 归一化样本 | 一致 | 一致 | — |

Python 回退路径由本机 E2E 与 Kernel 测试覆盖；两条路径都能通过 136 个 Kernel 测试。

---

## 4. 容量（详见 CAPACITY_BASELINE.md）

| 并发 | 完成 | 失败 | 卡住 | 交付 | 端到端 P50 | 吞吐 |
|---|---|---|---|---|---|---|
| 10 (fast) | 10/10 | 0 | 0 | 10 | 38.9s | 0.25/s |
| 25 (fast) | 25/25 | 0 | 0 | 25 | 96.0s | 0.25/s |
| 50 (fast) | 50/50 | 0 | 0 | 50 | 195.8s | 0.24/s |
| 100 (fast) | 100/100 | 0 | 0 | 100 | 393.3s | 0.24/s |
| 250 (fast) | 250/250 | 0 | 0 | 250 | 1048.8s | 0.225/s |
| 10 (real gate) | 10/10 | 0 | 0 | 10 | 42.8s | 0.23/s |
| 25 (real gate) | 25/25 | 0 | 0 | 25 | 116.7s | 0.21/s |

结论：

* **实测安全并发：250**（0 失败、0 卡住、全量交付）；100 与 250 的吞吐一致（0.22–0.24/s），
  并发只增加排队延迟，不增加吞吐。
* **500 / 1,000 未通过验证**：按 0.225 tasks/s 外推，1,000 需要约 74 分钟，
  端到端 P50 约 4,400s。
* 瓶颈是 Gate/Merge 并行度（2 个 gate worker）与 Runtime worker 数，不是 pyright
  （完整门禁只比打桩慢 10–18%）。

---

## 5. 混沌（详见 CHAOS_TEST_MATRIX.md）

9 个场景全部真实故障注入（SIGKILL / 进程重启 / 终止数据库连接 / 重复事件）：

worker_kill、gate_kill、kernel_restart、duplicate_event、cancel、model_timeout、
pg_blip、two_tasks_same_repo、runtime_api_restart —— **9/9 PASS**。

其中 `worker_kill` 的第一版运行**失败**并暴露了 P0-2（`WAITING_TOOL` 永久卡死），
修复后重跑通过；这就是混沌测试的价值，不是"为了通过而调测试"。

---

## 6. 安全（详见 SECURITY_QUALIFICATION_REPORT.md）

* 已修复：`rev` 注入（High）。
* 已测试通过：跨 workspace 的任务/证据/文件读取、写集越界、管理面鉴权、
  service key 校验、路径穿越、产品面跨租户访问。
* **未闭环 High**：Gate `local` 沙箱与 Kernel 同容器 → 恶意仓库测试可读其他
  workspace 的 bare repo 与内网。单租户可接受；多租户必须先启用容器沙箱/cell 隔离。

---

## 7. 真实模型（DeepSeek）

同一 E2E，真实 Provider，3 次独立样本（每次全新仓库与数据库）：

| 样本 | 结果 | 模型调用 | prompt tokens | completion | 工具参数错误 | commit |
|---|---|---|---|---|---|---|
| 1 | REVIEWING | 9 | 24,674 | 1,572 | 3 | `ba91b8de…` |
| 2 | REVIEWING | 11 | 30,291 | 2,214 | 2 | `86ac758e…` |
| 3 | REVIEWING | 20 | 75,942 | 4,232 | 2 | `aa5b6caa…` |

观察：模型仍会犯参数错误（缺字段、空 diff），但都能被工具错误反馈修正；
样本 3 的调用数明显偏高（20 次），说明上下文/提示仍有优化空间。
样本量 3，不能外推成功率。

---

## 8. 当前最大风险与下一步

**最大可靠性风险**：Gate 本地沙箱（多租户）与合并串行化（吞吐）。
**已知启动成本**：`record_release` 在新版本首次启动时同步向所有 workspace 写 changelog 与
`PLATFORM_RELEASED` 事件，成本 O(workspaces)。1,500 个 workspace 的测试库上会超过
45 秒。生产租户数较小时可接受；规模上来后应改为后台分批（本轮未改语义）。
一次性脚手架回填已改为每次启动最多 500 行。
**最大性能瓶颈**：Gate/Merge 与 Runtime worker 的固定并行度。
**生产部署条件**：单租户 + `VN_FAST_FORWARD_MERGE=true` + 至少 2 个 Gate worker；
多租户必须先把 Gate 放进独立容器并限制仓库可见范围。

下一阶段三个最值得投入的任务：

1. **Gate 隔离**：`VN_GATE_SANDBOX=docker`、每 gate 仅挂载自己的 worktree；
   验收标准：恶意测试无法读取 `/repos` 下其他仓库、无法访问内网。
2. **合并吞吐**：多 Gate worker 下的 merge 队列/批提交，目标把 0.24 tasks/s 提到 ≥1/s，
   100 并发端到端 P95 从 416s 降到 ≤120s。
3. **500/1000 真实分级验证**：在独立压测环境（多机或更强单机）重跑，
   记录 P50/P95/P99 与 stuck 数；未通过则保持 FAIL。

---

## 9. 未执行 / 明确标注

| 项 | 状态 | 原因 |
|---|---|---|
| 500 / 1,000 并发 | **未运行** | 单机吞吐 0.24 tasks/s，需 35–70 分钟且不满足交互目标 |
| 生产升级 / 回滚演练 | **未执行** | 本轮未获授权部署生产；compose 已校验 |
| Gate 容器沙箱 | **未实现** | 需要 Docker socket/DinD 或 cell 拆分；已给方案 |
| RLS 实测 | **未覆盖** | 测试环境以 owner 运行；生产 compose 已开启 RLS |
| 多租户跨 gate 读仓库 | **已知漏洞** | 安全报告 S-2 |
| 3 样本以外的模型稳定性 | **未统计** | 成本与时间；已记录 3 次 |

---

## 10. 复现全部证据

```bash
python scripts/chaos_suite.py
python scripts/capacity_unified.py --levels 10,25,50,100 --profile fast --workers 4 --gate-workers 2
python scripts/capacity_unified.py --levels 10,25 --profile real --workers 4 --gate-workers 2
python scripts/verify_rust_core.py
python scripts/restore_drill.py --source-prefix veynod_chaos_
MODEL_BASE_URL=https://api.deepseek.com/v1 MODEL_API_KEY=$DEEPSEEK_API_KEY \
  MODEL_NAME=deepseek-chat python scripts/e2e_unified.py --real-model
cd veynod_kernel && python -m pytest tests -q
RUNTIME_TEST_DATABASE_URL=... python -m pytest agent_runtime/tests contracts/tests -q
cd AgentGraph && TEST_DATABASE_URL=... .venv/Scripts/python.exe -m pytest tests -q
```
