# Phase 0 架构断层与风险登记

基准：本地 HEAD `d37b781`。P0 是阻止按目标架构直接上线的断层；P1 是影响可靠性、安全或扩展性的重大问题；P2 是需要验收的差距。风险代表代码可达的失败情形，不等于已在生产发生。

| ID | 级别 | 已确认事实、影响与证据 | 最小后续处理 |
|---|---|---|---|
| G01 | P0 | **缺少端到端 Write Scope**：Diagram 仅将 `allowed_write_paths` 写入 Product Task 的 `input_context` 和 `context_slice` (`AgentGraph/app/services/diagrams.py:749-778`)；Runtime Kernel 工具从模型参数取 `write_set_symbols` (`agent_runtime/tools/kernel.py:417-429`)；Kernel 只验证自身 Lease 写集 (`veynod_kernel/app/mcp_server/syscalls.py:774-788`)。因此 Kernel 的有效边界不由冻结 Diagram 强制导出。 | 定义版本化 WorkScope，服务端映射并在 Kernel Task/Lease 上校验哈希与子集。 |
| G02 | P0 | **同一任务有双执行所有权窗口**：Scheduler 排除 `active_run_id` (`scheduler.py:405-416`)，但 `task_lifecycle.start_run` 不检查已有 Product Lease/Placement (`task_lifecycle.py:234-265,310-344`)，`tasks.claim_task` 不检查 `active_run_id` (`tasks.py:678-746`)。两个入口分别可占有同一 Product Task，尤其是先 placement 后 start_run 或 Run 已启动后直接 claim。竞态是否在生产触发未验证。 | 用同一 attempt/owner 记录和原子 CAS 覆盖 placement、claim、Run 创建及撤销。 |
| G03 | P0 | **Kernel Merge 不驱动 Diagram**：Kernel 事件 inbox 只进 `code_engine.sync_kernel_task_event`，Runtime 事件只进 `task_lifecycle.project_run_event` (`event_dispatch.py:340-360`)；Diagram 下游解锁只由 Diagram `merge` 调 `_release_waiting` (`diagrams.py:1514-1557,1647-1668`)。故 Kernel 成功不保证 Chunk 完成、下游自动开始。 | 以 Kernel commit evidence 建幂等 Chunk 投影，解锁同事务/可重试。 |
| G04 | P0 | **两套 Gate/Merge 可并存**：Diagram `record_ci/gate/queue_check/merge` 接受调用者提交的结果或 artifact (`diagrams.py:850-895,1100-1159,1466-1557`)；Kernel Gate Worker/Merge Coordinator 独立验证和 Git 合并 (`veynod_kernel/app/services/gate_worker.py:75-150,591-665`；`merge_coordinator.py:214-352`)。没有共同 attempt/evidence fence；Diagram 测试以 `abc123` 即合并并解锁 (`AgentGraph/tests/test_loop_steps.py:97-118`)。 | 把 Diagram Gate/Merge 降为 Kernel 事实投影；旧入口受兼容策略管控。 |
| G05 | P1 | **Diagram 文件锁不是 Kernel 写集锁**：空 `allowed_write_paths` 被接受、`_lock_conflicts` 直接返回无冲突 (`diagrams.py:100-139,664-683`)；路径重叠只靠双向 `fnmatch`，不是规范化路径/符号锁；Chunk 的 `task_key` 只是文本键 (`AgentGraph/sql/migrations/v3_51_diagrams.sql:37-65`)。 | 冻结前拒绝不合法或空写域（有明确定义的只读任务除外），统一路径解析和冲突代数。 |
| G06 | P1 | **Host Runner 仍走分支/PR 和提示词约束**：它在本地建 Worktree，提示 Agent 自行 commit/push/PR、执行 merge (`AgentGraph/cli/bin/runner.mjs:200-257,397-445,623-684`)；该路径不调用 Kernel Lease/Submit，且没有发现与 Kernel Gate 的绑定。 | 将 Host 执行接入 Kernel Lease/提交协议；旧路径逐步关停。 |
| G07 | P1 | **生产配置里的 Gate 隔离级别低于目标**：部署清单两处 `VN_GATE_SANDBOX: local` (`deploy/vps/docker-compose.unified.yml:35,74`)；本地模式仅净化环境并在同一主机执行 (`veynod_kernel/app/services/gate_sandbox.py:59-75,115-132,275-284`)。真实运行配置本轮未登录核实。 | 在独立容器/受限执行器内运行不可信检查，实测文件、网络、资源边界。 |
| G08 | P1 | **容量目标无当前端到端证据**：部署清单 Runtime Worker 默认 2 (`deploy/vps/docker-compose.unified.yml:129-133`)；`scripts/capacity_unified.py:1-13` 支持分级压测，但当前 HEAD 没有本轮真实 1000 Runner + 全链路报告。既有文档明确说未重测 1000 Runner、历史 P99 54s FAIL (`docs/UNIFIED_ACCEPTANCE_REPORT.md:248`)；该文档不是本次实测。 | 先定义并发执行指标和门槛，再做 10→50→100→250→500→1000 多机真实链路压测。 |
| G09 | P1 | **代码基准与部署分叉**：当前本地 HEAD `d37b781`，GitHub main 当前 `88626e3`（2026-11-02 `git ls-remote`），后者新增 `kernel_delivery.py` 和 Worktree Lease；`git diff --stat` 为 33 文件、2290 插入、128 删除。故不能把规范引用的 `88626e3` 能力算作本地 HEAD。VPS 本轮 SSH 公钥拒绝，当前镜像版本 NOT VERIFIED。 | 审核后先选定代码基准与部署证据，再评估新提交是否替代 G06 部分工作。 |
| G10 | P2 | **兼容路径仍在**：`CODE_ENGINE_LEGACY_DIRECT` 可直接建 Kernel Task (`AgentGraph/app/services/code_engine.py:308-350`)；Kernel v1 同步提交可经 `VN_LEGACY_SUBMIT_MODE=sync` 打开，但默认 reject (`veynod_kernel/app/config.py:263-269`, `app/mcp_server/syscalls.py:990-1023`)。部署实际开关 NOT VERIFIED。 | 盘点配置/调用量，设置关闭条件与退路。 |
| G11 | P2 | **测试缺少统一链路证明**：Diagram 集成测试本机 25 skipped；Runtime 所选测试 2 passed、16 skipped；Kernel 所选 15 passed；Host Runner 19 passed、3 failed（Windows CLI 探测/CRLF/路径转义）。`test_loop_steps.py:97-118` 的 Diagram 合并只用伪 artifact。 | 补真正跨服务、多 Runner、崩溃恢复及负面安全用例，保留分层结果。 |
| G12 | P2 | **跨域契约包尚未接线**：`contracts/identifiers.py:15-27,72-77` 已定义 `attempt_id` 等，`contracts/events.py:58-84` 已定义事件包；`contracts/tests/test_contracts.py:21-66` 5 passed，但对三个服务请求路径的导入搜索未见使用，`contracts/README.md:40-46` 也将此列为未完成。 | 在任务/事件协议改造时复用并接入这些定义，加入跨服务契约测试。 |

## 状态冲突与恢复后果

- `diagram_chunks.state='merged'` 可由调用者提供的 artifact 触发 (`diagrams.py:1533-1555`)，而 Kernel Task 可能未 COMMITTED；反向地 Kernel COMMITTED 也不会自动改变 Chunk (`event_dispatch.py:340-360`)。这两种不一致有可达代码路径，但生产发生率 **UNKNOWN**。
- `collab_tasks.active_run_id` 只保证单个 Runtime Run 绑定，不能排除 Product Task 的 Host claim (`AgentGraph/sql/migrations/v3_75_unified_runtime.sql:16-21`；`tasks.py:678-746`)。
- AgentGraph 持久 inbox 有重试和 dead 状态 (`event_dispatch.py:365-388`)，Kernel 也有 outbox (`veynod_kernel/app/services/event_bus.py:61-75`)；可靠传输本身已具备基础，缺的是 Diagram 投影和统一幂等关联。
