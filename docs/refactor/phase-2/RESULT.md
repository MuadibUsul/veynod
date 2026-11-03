# Phase 2 实施结果

基准 `b8355c0`；分支 `codex/phase2-diagram-kernel`。计划见 `PLAN.md`。

## 结论

计划第 1–6 项全部落地，验收通过：真实 Git 仓库（bare mirror）、真实 Kernel API 与 gate
worker、真实 Diagram Chunk，从冻结、派发、Kernel Lease/Patch/五个阶段门禁到 Diagram
`merged` 与 `merged_commit_sha`；重复事件与对账都不重复合并、不重复解锁。

| 验收 | 结果 |
|---|---|
| `pytest tests/test_phase2_chunk_delivery.py` | **3 passed**（约 2 min 53 s，真实 Kernel + gate worker） |
| `pytest tests/test_diagrams.py` | 21 passed |
| `pytest tests/test_kernel_delivery_scope.py` | 8 passed |
| Kernel 安全/守卫/scope 子集 | 9 passed |
| 真实 Kernel 探针（同一 request_key 两次 provision） | 1 个节点；第二次复用 |

## 落地内容

1. **单绑定**：`v3_80` 给 `diagram_chunks` 加 `kernel_task_id`（唯一索引）、`scope_hash`、
   `merged_commit_sha`，并用 `ck_diagram_chunks_kernel_scope` 约束"Kernel-owned 必有 scope"。
2. **派发即绑定**：`dispatch` 调 `provision_product_task(lease=False)`；失败不静默，写
   `CHUNK_KERNEL_UNBOUND`。租约留给真正执行的一方，避免"等宿主时已持写集锁"。
3. **投影**：`project_kernel_commit` 用 `(chunk, commit)` 幂等键（`diagram_chunk_projections`）
   把 chunk 置 `merged` 并走原有 `_release_waiting`，下游因此解锁。
4. **人工 merge 降级为校验**：Kernel-owned chunk 无 `merged_commit_sha` 时返回 409
   `kernel_not_committed`，不会二次合并。
5. **对账**：`app/services/diagram_reconcile.py` 周期扫"已绑定、未合并、够老"的 chunk，
   以 Kernel 的 `/v1/admin/tasks/{id}/evidence` 为准补投投影；重放安全。
   与 `event_dispatch` 的 at-least-once 一起构成"事件漏了也能自愈"。
6. **双向所有权互斥**：`claim_task` 拒 `active_run_id`，`start_run` 拒已被 host claim 的任务。

## 关键实现细节（下一个会话别推翻）

* **Kernel 没有 get-task 路由**。节点的权威读是 admin evidence 端点，它返回 `task_status`
  与 `merged_commit_sha`；`KernelPlane.task()` 就包这一层。不要改回 `/v1/tasks/{id}`。
* **`plane_for()` 需要 admin key**（`X-Admin-Key`），不是服务键。`plane_for(binding,
  admin_key=...)` 可显式传入，默认取 `settings.kernel_admin_key`。
* **gate 是独立进程**（`python -m app.worker`）。同步端点 `/v1/syscalls/submit_patch` 已被
  v2 取代，需 `VN_LEGACY_SUBMIT_MODE=sync` 才可用；端到端测试用同步路径（它跑同一套五阶段
  门禁），异步队列的行为由 `test_nerve_dispatch` 之外的既有 kernel 测试覆盖。
* **Kernel 必须跑在自己的解释器上**（`tree_sitter_python` 不在 AgentGraph 的 venv 里）；
  测试用 `KERNEL_PYTHON` 或宿主 `python`。
* **仓库必须是 bare mirror**（gate 用 `update-ref` 推进分支），非 bare 会被 422 拒绝。
* **`KERNEL_URL` 是事件轮询的全局设置**，不来自 binding；测试必须把它指到同一个 Kernel。

## 仍未做

* 生产部署仍是 `88626e3`，缺 `v4_017`（Kernel）与 `v3_80`/`v3_81`（产品）。部署后需核对
  63 个历史 chunk（多为无绑定的 legacy）。
* `01/02/03/04` 架构附属文档仍是旧基准。
* 本轮测试库被历史数据污染，`event_dispatch` 会对不可达的旧 kernel workspace 报 404
  （已按 workspace 隔离，不影响本阶段用例）。
