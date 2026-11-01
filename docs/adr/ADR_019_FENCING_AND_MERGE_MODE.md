# ADR-019 / ADR-020：写隔离与并发合并

状态：**Accepted**（2026-11-01，生产资格轮）。

---

## ADR-019 Run 写入使用 lease epoch 作为 fencing token

**背景**：`RunStore.claim_run` 只替换租约令牌；校验是"写之前检查一次"
（`LoopController._check_guard`）。Worker A 在慢模型/慢工具中阻塞、租约过期、
Worker B 接管后，A 返回仍可写 checkpoint、step，甚至把 Run 置为终态。
契约（一个任务一个活动执行者）在数据库层面没有强制。

**候选**：
A. 让所有写都包在"检查+写"的同一事务里（每次多一次行锁）：可行但漏在
   `record_step`/`checkpoint` 分散路径，容易漏改。
B. Fencing token（epoch）：claim 自增 epoch，所有 worker 写携带 epoch，
   失配即拒绝。
C. 分布式锁服务：引入新基础设施，拒绝。

**决策**：B。

* `agent_runs.lease_epoch BIGINT`，`claim_run` 原子自增并返回。
* `transition`、`checkpoint`、`record_step`、`add_usage`、`defer_external`、
  `finish_invocation` 接受 `fence`，在同一事务内锁 Run 行并比较 epoch；
  不符抛 `LeaseLost`。
* API/事件侧的写入（取消、wakeup 恢复）不传 fence：它们不是租约持有者。
* 顺带修复：`claim_run` 把 `WAITING_TOOL/WAITING_MODEL/CHECKPOINTING`
  视为可恢复状态，接管时归一为 `RUNNING` 并发事件——否则 Worker 在工具调用中
  死亡会让 Run 永久卡死（混沌测试发现）。

**取舍**：每个 worker 写多一次行锁（同一行，成本可控）；换来"旧执行者绝不可能
覆盖新执行者"的硬保证。

**验收**：`agent_runtime/tests/test_fencing.py`（5 例）与 chaos `worker_kill`。

---

## ADR-020 同仓库多任务必须使用树合并（`VN_FAST_FORWARD_MERGE=true`）

**背景**：容量测试在 10–25 并发时出现 `revalidation_exhausted`
（"Main advanced through a verified dependency repeatedly"）。默认
`fast_forward_merge=false` 时，只要 HEAD 被其他任务推进，merge 就要求重新跑门禁；
连续 3 次被推进即失败。对写集不相交的任务，这是不必要的串行化。

**决策**：

* 生产默认建议 `VN_FAST_FORWARD_MERGE=true`：HEAD 前进时走 `_tree_merge`
  （把已验证的 task commit 树合并进新 HEAD，再走 CAS），依赖变更仍由
  `_dependency_changed` 触发重验证。
* 该设置已写入统一 compose 与容量/混沌脚本；不使用它的部署在多任务同仓库时
  会出现可预期的合并失败。

**取舍**：树合并比快进多一次 tree 计算与敏感文件快照；换来多任务同仓库的可用吞吐
（容量 25/50/100 的失败数从 3 降到 0）。

**验收**：`CAPACITY_BASELINE.md`（修复前后）、chaos `two_tasks_same_repo`。
