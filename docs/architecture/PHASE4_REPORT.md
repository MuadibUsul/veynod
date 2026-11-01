# PHASE 4 REPORT — Kernel 多仓库与 Execution Cell

日期：2026-11-01
范围：Cell 注册、Repository Placement、分区 claim/merge、placement fencing。

---

## 1. 交付

### 1.1 数据模型（`v4_012_cells_repositories.sql`）

- `cells(cell_id, name, endpoint, status, capacity, timestamps)`；默认 cell `default`。
- `repository_placements(workspace_id PK, repository_id UNIQUE, cell_id, placement_epoch, status)`；
  - 每个 workspace（= 一个仓库）自动获得 placement；
  - 新 workspace 创建时自动加入 `default` cell；
  - 移动 cell 时 `placement_epoch + 1`。
- `patch_submissions` 增加 `claimed_cell_id` / `claimed_placement_epoch`（为后续 claim-level fencing 预留）。

### 1.2 分区执行

- `VN_CELL_ID` 绑定一个 API/worker/gate/merge 进程到 cell。
- Gate Worker claim 只领取本 cell 仓库的 submission（`cell_registry.cell_filter_sql`）。
- Edge rebuild claim 只领取本 cell 的 job。
- Merge Coordinator `process_all` / `reconcile_all` 只遍历本 cell 的 workspace。
- 单 workspace syscall 的默认 workspace 解析在本 cell 内选择。
- `verification_gate._merge` 在 Git 临界区前调用
  `assert_workspace_in_current_cell`：仓库被移走后，旧 cell 的 worker 无法提交 Git。

### 1.3 API

- `GET /v1/admin/cells` / `POST /v1/admin/cells`
- `GET /v1/admin/repositories`
- `POST /v1/admin/repositories/{workspace_id}/move`（`cell_id`, `expected_epoch`；epoch 冲突 409）

---

## 2. 验证

| 验证 | 结果 |
|---|---|
| Kernel 集成：创建 cell、列出默认 cell | passed |
| Kernel 集成：repository placement 移动 → epoch+1 | passed |
| Kernel 集成：旧 epoch 移动返回 409 | passed |
| Kernel 完整集成套件（22 tests） | passed |
| `cell_filter_sql` 在未绑定 cell 时为空 | 代码路径保持单部署兼容 |

---

## 3. 限制

1. 仍是 **一个 workspace = 一个 Git 仓库**；本阶段提供的是“一个 Cell 管理多个仓库”的 placement 层，不是同一 workspace 内多仓库。
2. Repository Registry 以 placement 行为准，没有独立的仓库元数据/凭据/默认分支副本。
3. `repository_placements` 的移动只改路由，不搬迁 Git 数据；跨主机仓库迁移需要额外的仓库同步/交接流程。
4. 公平调度仍是 per-workspace admission；没有 cell 级的配额/权重。
5. 未绑定 `VN_CELL_ID` 的进程仍可处理所有仓库（向后兼容部署）。
6. claim-level 的 `claimed_cell_id/epoch` 只落列；真正阻止旧 worker 提交的是 merge 临界区检查，不是 runner 侧提交入口。
7. 没有 Cell 故障自动接管/健康探测；只有路径过滤与 merge fence。

---

## 4. 结论

Cell/Repository Placement 已经是可运行的分区边界：不同 cell 的 worker 不会争抢彼此的仓库，仓库迁移会 fences 旧 cell 的 Git merge。它不是完整的“多仓库单 workspace”或跨主机 Cell 调度器。
