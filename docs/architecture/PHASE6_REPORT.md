# PHASE 6 REPORT — 安全执行与交付 Pipeline

日期：2026-11-01
范围：Gate Sandbox 加固、Delivery Record、证据链。

---

## 1. 交付

### 1.1 Docker Sandbox 加固

`app/services/gate_sandbox.py` 的 Docker 路径现在包含：

- `--network $VN_GATE_DOCKER_NETWORK`（默认 `none`）；
- `--cpus`、`--memory`、`--pids-limit`；
- `--ulimit nofile=VN_GATE_NOFILE_LIMIT`；
- `--cap-drop ALL`；
- `--security-opt no-new-privileges`；
- `--read-only` + `/tmp` tmpfs；
- 可选 `--user $VN_GATE_DOCKER_USER`；
- 只挂载 worktree，Docker CLI 环境只透传 `PATH/DOCKER_HOST/DOCKER_CONFIG/Windows` 变量，不传数据库/admin/云凭据。

生产校验（`app/security.py`）：

- `VN_AUTH_MODE=prod` + `VN_GATE_SANDBOX=docker` 时：
  - 必须设置 `VN_GATE_DOCKER_USER`（非 root）；
  - `VN_GATE_DOCKER_NETWORK` 必须为 `none`；
- `local` sandbox 仍要求显式 `VN_ALLOW_LOCAL_GATE_IN_PROD=true`。

### 1.2 Delivery Record（`v4_014_delivery_records.sql`）

- `delivery_records(delivery_id, workspace_id, task_id UNIQUE, submission_id, merge_id, commit_sha, base_sha, merge_mode, snapshot_id, gate_results, evidence_hash)`。
- 在 `verification_gate._finalize_merge` 的同一 DB 事务内写入：
  - 与 Git commit 同时提交；
  - `evidence_hash` 覆盖 commit/base/changed_files/merge_mode/gate_results；
  - 一个 task 只交付一次（`ON CONFLICT (task_id) DO NOTHING`）。
- `/v1/admin/tasks/{id}/evidence` 现在返回 `delivery`；
- 新接口：
  - `GET /v1/admin/tasks/{id}/delivery`
  - `GET /v1/admin/tasks/{id}/snapshot`

### 1.3 证据消费

- AgentGraph 的 Kernel 客户端新增 `delivery()`；
- 产品层 `/v1/deliveries` 能显示 commit、merge mode、gate 结果；
- 仍坚持“只读 Kernel 证据，不合成 PASS”。

---

## 2. 验证

| 验证 | 结果 |
|---|---|
| Kernel 安全配置测试（8 tests） | passed |
| `prod + docker` 无 user → 拒绝 | passed |
| `prod + docker` user + network none → 接受 | passed |
| 完整实现流：delivery record 与 commit 一致 | passed |
| 完整实现流：evidence 返回 delivery | passed |
| Docker 实际执行 | 未在 CI 运行（无 gate image）；本地默认 `local` |

---

## 3. 限制

1. 没有提供 `veynod-gate` Docker 镜像/Dockerfile；生产需要自备包含 pyright/ruff/pytest 的镜像。
2. `VN_GATE_DOCKER_USER` 默认空；production 校验会拒绝 docker 模式未设置 user。
3. 没有磁盘配额（bind mount 的 worktree 可写）；只有 `/tmp` tmpfs 限额。
4. 没有 seccomp/AppArmor 自定义 profile，只使用 Docker 默认 + no-new-privileges + cap-drop。
5. Delivery Record 不是密码学签名/哈希链；`evidence_hash` 是内容哈希，不是签名。
6. Artifact 仍是 Git + AST 元数据，没有对象存储/制品库。
7. Worker 崩溃恢复依赖 work lease/merge intent，已有测试；没有专门的“kill -9 mid-gate”故障注入测试。

---

## 4. 结论

Phase 6 把 gate 的 Docker 隔离参数、生产校验和交付记录补齐；现在一次成功 merge 会留下可审计的 `delivery_records`。生产使用前仍需自备 sandbox 镜像并设置非 root user。
