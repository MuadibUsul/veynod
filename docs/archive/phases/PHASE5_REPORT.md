# PHASE 5 REPORT — Code Intelligence 与并发修改

日期：2026-11-01
范围：Task Snapshot、symbol graph version、受影响测试、merge conflict 持久化。

---

## 1. 交付

### 1.1 Task Snapshot Binding（`v4_013_code_intelligence.sql`）

- `task_snapshots(snapshot_id, workspace_id, base_sha, graph_version, manifest, manifest_hash)`。
- 每个新任务在 `task_factory.insert_task` 中生成 snapshot：
  - 记录 read/target/test artifacts 的 `signature_hash`、`content_hash`、`version`；
  - 记录 write/ancestor locks；
  - 记录任务创建时的 `base_commit_sha` 和 `symbol_graph_versions.version`；
  - `dag_tasks.snapshot_id` 绑定。
- `patch_submissions` 增加 `base_sha` / `snapshot_id`，提交时从任务快照写入，作为后续 gate/merge 的可追溯输入。

### 1.2 Symbol graph version

- `symbol_graph_versions(workspace_id PK, version)`。
- 每次 edge rebuild 完成时递增；snapshot 的 `graph_version` 因此可判断符号图是否已过期。

### 1.3 受影响测试选择

- `verification_gate._test_files()` 现在合并：
  - 任务显式 `test_artifact_ids`；
  - 通过 `symbol_edges` 递归（深度 ≤3）找到的、调用目标符号的 test artifacts。
- 降低“只跑固定测试、漏掉依赖测试”的风险；没有符号边时退回显式测试。

### 1.4 Merge conflict 持久化

- `merge_conflicts(workspace_id, task_id, merge_id, kind, paths, detail, status)`。
- 主分支在敏感文件上前进时，Merge Coordinator 写入 `dependency_changed` 冲突，发布
  `CONFLICT_ESCALATED` 事件，再执行 revalidation；
- merge 成功后自动把该 task 的 OPEN 冲突标记 `RESOLVED`。
- 运营接口：
  - `GET /v1/admin/merge-conflicts?workspace_id=&status=`
  - `POST /v1/admin/merge-conflicts/{id}/resolve`

---

## 2. 验证

| 验证 | 结果 |
|---|---|
| Kernel 完整集成套件（22 tests） | passed |
| 完整实现流：snapshot endpoint 返回创建时 base_sha + artifacts | passed |
| 完整实现流：delivery/gate 证据仍完整 | passed |
| 受影响测试选择 | 代码路径启用；现有测试未覆盖 symbol_edges 命中（需补专门用例） |
| merge conflict 写路径 | 代码路径 + 事件；未构造真实并发冲突测试 |

---

## 3. 限制

1. Snapshot 是**元数据**，不是仓库内容副本；不保存文件 blob，重放仍依赖 Git。
2. 没有严格的“过期 snapshot 拒绝提交”策略：gate 仍按当前 HEAD 重新验证；snapshot 主要用于追溯和 graph version。
3. `symbol_graph_versions` 只在 edge rebuild 完成时递增；同步重建路径也递增，但未建立跨仓库全局版本。
4. 受影响测试选择依赖 `symbol_edges` 的启发式扫描（identifier/attribute），不是编译器级调用图。
5. Merge conflict 目前只在 `dependency_changed` 路径持久化；`git merge-tree` 的文本冲突仍走 gate rerun，没有单独分类为 `CONFLICT`。
6. 动态写集扩展（INJECT）仍没有“必须由 Kernel 授权扩展”的强制校验。
7. 没有并发回放测试（同一 repo 多任务冲突/快照过期的系统级 fault injection）。

---

## 4. 结论

Phase 5 建立了快照绑定、图版本、受影响测试和冲突持久化的基础；它提高了可追溯性和测试覆盖，但还不是完整的 AST/依赖图冲突引擎，也没有强制拒绝过期快照。
