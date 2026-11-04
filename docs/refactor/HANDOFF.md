# 交接文档

> **2026-11-04 现状**（先读这一节）：下面 2026-11-02 的内容已经在 #89 合并；之后又有三批改动，
> 都在分支 `claude/cool-cerf-it7a0j`：
>
> 1. **电脑 / Agent / Loop 的循环**：`/kernel/*` 路由 500（缺 import）；交付错误码被压成
>    `http_error`；slots=1 没有评审座位后，不归 Kernel 的 REVIEWING 任务永远等不到评审电脑、
>    Kernel 已合并的任务也不进“需要你”；控制台对已不需要的 -review 座位报错；`up` 在改 slots 时
>    重复铸造 `-review-N` 身份；nerve 200 ms 固定超时在远距离链路上反复踢下线；nerve worker
>    连错服务器、清空电脑的 repos、找不到 `plc_` placement；CLI 包漏发 `veynod-messenger.mjs`。
> 2. **Kernel 交付的循环**：提交幂等键不含补丁摘要，门禁失败后的修订永远 409；Kernel 错误码丢失；
>    runner 把“代理已提交、门禁进行中”当失败；租约回收在门禁进行中把任务交给别的电脑重做。
> 3. **一个仓库**：API 镜像从根目录构建（原来无法构建）；根目录唯一 CI；`AgentGraph/cli` 是 CLI
>    唯一源；三服务依赖精确一致；删除 `veynod_platform`；三个 Kernel HTTP 客户端合并为
>    `contracts/kernel_http.py`；文档地图 [docs/README.md](../README.md)，阶段报告归档。
>
> 仍需人来决定：`AgentGraph/docker-compose.prod.yml` 仍管理生产数据库容器 `veynod-db-1`，
> 把它并入 unified 编排需要一次有备份的维护窗口；G10 生产对齐（迁移 `v4_017`、`v3_80`、`v3_81`）。

---

## 2026-11-02：Phase 0 审计 + G01–G09 修复 + IM/Phase 2 落地（已在 #89 合并）

> **2026-11-02 后续一轮**：`docs/veynod_im.md` 的四步与 `phase-2/PLAN.md` 的第 2/5 项
> 也已做完，各有一份记录（[IM_IMPLEMENTATION.md](IM_IMPLEMENTATION.md)、
> [phase-2/RESULT.md](phase-2/RESULT.md)）。本文 §4 的待办只剩生产部署（G10）与
> 旧附属文档校正（G11）。

---

## 1. 结论先看

Phase 0 审计发现了 11 个断层（G01–G11，见 `docs/refactor/00_CURRENT_STATE_VERIFICATION.md`），
本轮**已修复其中 9 个**（G01–G07、G09 + 部分 G06），**未提交**。

修复后：

| 验证 | 结果 |
|---|---|
| `pytest tests/test_diagrams.py tests/test_loop_steps.py tests/test_kernel_delivery_scope.py tests/test_task_lifecycle.py` | **38 passed, 1 skipped** |
| `pytest tests/test_kernel_delivery_scope.py`（含新增 4 例） | 8 passed |
| `pytest veynod_kernel/tests/test_security_isolation.py test_submission_guard.py test_work_scope_boundary.py` | 9 passed |
| 真实 Kernel 探针（同 request_key 两次 provision） | **1 个节点，第二次复用**（见 §3） |
| `ruff check`（改动文件） | 全绿 |
| `veynod_kernel/tests` 全量 | **149 passed** |
| `AgentGraph/tests` 全量 | **578 passed / 26 skipped / 0 failed** |

AgentGraph 全量在基线 `b8355c0` 上是 **6 failed / 548 passed / 26 skipped**。本分支
把既有 6 个全部修掉（见 §4.1），并新增 Phase 2 的 3 个端到端用例与 nerve 的 17 个用例。

---

## 2. 本轮改了什么（逐条对应 G 编号）

| ID | 修复 | 文件 |
|---|---|---|
| G02 | `_do_create_task` 改用 `product:<product_task_id>` 作为 `request_key`，与 `kernel_delivery` 一致，避免同一任务建出第二个 Kernel 节点 | `agent_runtime/tools/kernel.py:419` |
| G03 | `_NOT_WIRED` 增加 `needs_write_set` / `lease_denied` / `admission`，这些状态不再被当作 Kernel-owned，review/merge 继续正常放置 | `AgentGraph/app/services/kernel_delivery.py:37` |
| G07 | 空 `allowed_write_paths`：`_incomplete()` 现在要求非空（422 拦截）；`work_scope_for()` 空集合时返回 `None` 走旧写集，不再把 Kernel 必拒的空 scope 发下去 | `diagrams.py:132`、`kernel_delivery.py:68` |
| G08 | 迁移 `v3_80`：`diagram_chunks.kernel_task_id/kernel_workspace_id/scope_hash/merged_commit_sha`、唯一索引、`diagram_chunk_projections` 去重表、scope 约束 | `AgentGraph/sql/migrations/v3_80_diagram_kernel_binding.sql`（已注册到 `database.py`） |
| G01 | `dispatch` 调 `provision_product_task(lease=False)` 绑定节点；失败不静默，写 `CHUNK_KERNEL_UNBOUND` 事件 | `diagrams.py:_provision_kernel_node` / `_dispatch_one` |
| G04 | 新增 `project_kernel_commit()`：commit 事件把 chunk 置 `merged`、写 `merged_commit_sha`、走原 `_release_waiting` 解锁下游，`(chunk, commit)` 去重；`code_engine.sync_kernel_task_event` 调用它；人工 `merge` 对 Kernel-owned chunk 改为校验（无 commit 则 409 `kernel_not_committed`） | `diagrams.py`、`code_engine._project_chunk` |
| G05 | `claim_task` 拒绝有 `active_run_id` 的任务（409 `task_owned_by_run`）；`start_run` 拒绝已被 host claim 的 IN_PROGRESS 任务 | `tasks.py:699`、`task_lifecycle.py:266` |
| G06 | `open_lease` 不再复用已过期租约（`_lease_expired`）；新增 `KernelPlane.release` + `release_task_lease()`；`release_task` 与 `_take_back` 都调用它 | `kernel_delivery.py`、`tasks.py`、`scheduler.py:157` |
| G09 | `runner.mjs` 新增 `touchedSymbolsFromDiff()`，从 diff 解析真实符号提交，不再拿租约写集冒充 | `AgentGraph/cli/bin/runner.mjs:452` |

顺带修的基础设施问题：

* `kernel_delivery.py` 直接 `import contracts` 会让**测试收集失败**（单测用 `importlib` 绕开包导入）。加了 `sys.path` 兜底，并让 `AgentGraph/Dockerfile` 复制 `contracts`。
* Kernel 测试的 gate worker 子进程缺 `PYTHONPATH`（Phase 1 引入 `contracts` 后必踩），已在 `test_integration.py` 环境里补上；`veynod_kernel/tests/conftest.py` 也把仓库根加入 `sys.path`。
* `test_diagrams.py` 两处断言按新规则更新（写路径必填、`CHUNK_KERNEL_UNBOUND` 是预期事件）。

---

## 3. 探针证据（可复现）

临时库 + 真 git 仓库 + 真实 Kernel：

```
same key -> same node: True
p1: 200 leased=True
p2: 200 leased=False          # 同一节点已被租
kernel nodes: 1
```

对比修复前：`runtime create: 201 same task? False` / `k tasks: 2` / 定向也领不到。

探针脚本内容见本文 §6，可直接重跑。

---

## 4. 尚未做完的事（按优先级）

1. ~~跑完两条全量回归~~ **已完成**：Kernel 149 passed；AgentGraph 全量见最后一次运行，
   仍有的失败都是既有问题（见 §4.1）。
2. ~~补 G01/G04 的集成测试~~ **已完成**：`tests/test_phase2_chunk_delivery.py`（3 passed）。
3. **G10 生产对齐**（剩下唯一必须做的事）：VPS `VEYNOD_TAG=88626e3`、Kernel 库 `v4_014`、
   `dag_tasks` 无 `work_scope` 列。部署前需 `v4_017`（Kernel）+ `v3_80`/`v3_81`（产品）；
   部署后核对 63 个历史 chunk（多为无 kernel 绑定的 legacy）。
4. **G11 文档**：`docs/refactor/00_CURRENT_STATE_VERIFICATION.md` 已改写为 HEAD `b8355c0` 版
   并补了 §8；旧的 `01_CURRENT_ARCHITECTURE.mmd`/`02`/`03`/`04` 仍是旧基准，需按新报告校正。
5. ~~Phase 2 剩余~~ **已完成**：对账器见 `app/services/diagram_reconcile.py`。

### 4.1 既有失败（本轮已全部修掉，留档）

* `test_phase1_up_hub.py`（3 个）：`kernel_delivery_default=True` 后 reviewer 不再必需，
  测试仍断言必需。**修法**：给该模块一个关了 Kernel 交付的专属服务（`LEGACY_DELIVERY`），
  `up` 加 `--legacy-review`，config 带 `legacy_review`。这测的本来就是 hub 双身份。
* `test_docs_site` / `test_error_registry::test_generated_error_doc_is_current` /
  `test_onboarding::test_generated_agent_guide_is_current`：新增错误码让生成物过期。
  已重跑 `gen_docs_site.py`、`gen_error_codes.py`、`gen_agent_guide.py`。
* `test_exports`：新增两张表未分类。已登记为 EPHEMERAL。
* `test_phase3_r32::test_scheduled_exports`、`test_phase3_phase7::test_event_inbox_*`：
  与后台循环抢跑，断言太紧。已放宽为"等到至少 N 次"。

### 4.2 本轮踩过的坑（别重犯）

* `asyncio.run()` 写在测试里会关掉当前 loop，**后续所有 async 用例连带失败**
  （表现为 `coroutine ... was never awaited`）。用 `asyncio_mode=auto` 下的 `async def`。
* 测试进程里 `KERNEL_ADMIN_KEY` 不可靠；对账器要显式收 `admin_key`。
* 全量下有两个服务共用测试库，后台 reconciler 会抢先处理同一批 chunk；
  断言要容忍"别的进程先做成了"。

---

## 5. 关键设计决定（下一个会话别推翻）

* **dispatch 只绑定、不租约**。租约留给真正执行的一方（Host 的 `/kernel/lease` 或 Runtime）。
  理由：一个等 host 的节点若已 LEASED，同写集的其他节点全部被 `NOT (write_locks && active_w)` 挡住，
  而执行方又领不到它——这正是 G02 的表现。
* **Kernel-owned 的判定基于 lease 是否真的拿到**（`_NOT_WIRED` 排除未租状态），不是基于"有没有 repo"。
* **人工 merge 保留，但降级为校验**：Kernel-owned chunk 必须已有 `merged_commit_sha`，否则 409。
  这样运维不会因为"图表看起来没合并"而重复合并。
* **投影幂等键用 `kernel:<kernel_task_id>:<commit_sha>`**，写 `diagram_chunk_projections`。

---

## 6. 复现探针

```bash
# 1. 建临时库
python - <<'PY'
import asyncio, asyncpg
async def m():
    c=await asyncpg.connect("postgresql://agentgraph:agentgraph@127.0.0.1:55433/postgres")
    await c.execute("DROP DATABASE IF EXISTS veynod_probe"); await c.execute("CREATE DATABASE veynod_probe"); await c.close()
asyncio.run(m())
PY
# 2. 起 Kernel（注意 PYTHONPATH 指向仓库根）
cd veynod_kernel && PYTHONPATH=/e/Codes/veynod DATABASE_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/veynod_probe \
  VN_AUTO_INIT_DB=true VN_AUTH_MODE=dev VN_ADMIN_API_KEY=probe-admin-key-0123456789abcdef01 \
  VN_RUNTIME_SERVICE_KEY=probe-service-kernel-0123456789ab \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8191 &
# 3. 建 workspace/artifacts，用同一 request_key 调两次 POST /v1/runtime/tasks，两次 pull
#    期望：1 个节点；第一次 leased=True；第二次 leased=False
```

---

## 7. 提交建议

一个提交，消息要点：`Fix Diagram↔Kernel delivery gaps (G01–G09)`，
正文列出：单一 provisioner 键、chunk 绑定列与投影幂等表、dispatch 绑定不租、
commit 事件投影解锁、人工 merge 降级为校验、双向所有权互斥、租约过期检查与 release、
真实 touched symbols、`contracts` 打包与测试 PYTHONPATH 修复。

**不要**把 `Veynod_Next_Generation_Architecture_Refactor_Spec_v1.0.md`（未跟踪）一起提交，
除非确认它应该纳入版本库。
