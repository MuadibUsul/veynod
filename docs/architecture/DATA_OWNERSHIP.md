# DATA_OWNERSHIP — 数据与状态写入权威

> 目的：回答“谁拥有哪个事实、谁可以写、其他系统如何获得”，为 Phase 1 的契约和 Phase 3 的投影提供依据。
> 原则：一个状态只能有一个写入权威；跨 bounded context 不直接写对方数据库；不把事件/缓存当作唯一权威。
> 证据：源码、迁移、测试；日期 2026-10-31。

---

## 1. 数据边界

| 数据库/领域 | 内容 | 当前状态 |
|---|---|---|
| AgentGraph PostgreSQL（`agentgraph`） | 用户、租户、workspace、membership、产品任务、文档、审批、审计、预算、Webhook、事件 | 48 张 ORM 表；69 个迁移；RLS 部分启用 |
| Agent Runtime（目标） | Agent Run、Step、Checkpoint、Invocation、Runtime Event | **不存在** |
| Kernel PostgreSQL（`veynod_dev`） | workspace、artifact、DAG task、dependency、symbol edge、event、token、submission、gate lease、merge、admission、authority、outbox | 15 张产品表 + `_commit_probe`；8 个迁移文件 |
| 对象存储 | 大型模型输入输出、Artifact | 当前未形成独立对象存储层；文档/Blob 在 AgentGraph DB |
| Git 裸仓库 | 代码与 commit | Kernel 管理；`workspaces.repo_path` |

**没有跨库 FK。** AgentGraph 通过 `collab_tasks.input_context.kernel` 保存 Kernel workspace/task id；Kernel 不保存 AgentGraph task id（除事件/调用方上下文）。

---

## 2. AgentGraph 数据所有权

### 2.1 核心表分组

| 组 | 表 | 所有者/主要写入方 | 唯一权威？ |
|---|---|---|---|
| 身份/租户 | `tenants`、`workspaces`、`memberships`、`human_users`、`human_sessions`、`agent_tokens` | `services/accounts.py`、`workspace.py`、`members.py`、`token_tree.py`、`auth_routes.py` | 大体是；token 子树/委托有多入口 |
| 产品任务 | `collab_tasks` | `services/tasks.py`、`leases.py`、`collab.py`、`scheduler.py`、`reviews.py`、`diagrams.py`、`audit.py`、`quality.py`、admin 路由 | **否**（9+ 写入方） |
| 任务租约 | `leases` + `collab_tasks.lease_*` | `leases.py`、`tasks.py`、`collab.py`、`diagrams.py`、DB trigger | **否**（镜像 + 多入口） |
| 任务审计 | `audits`、`state_audit_diffs` | `common.record_audit`（44 调用点） | 单函数多调用；无 append-only 触发器 |
| 文档/黑板 | `shared_documents`、`document_versions`、`document_chunks`、`document_refs`、`document_blobs`、`namespaces`、`document_proposals`、`proposal_votes` | `documents.py`、`blackboard.py`、`proposals.py`、`indexer.py` | 大体按服务；DSAR 可改写 |
| 状态槽 | `context_slots` | `slots.py` | 是 |
| 事件 | `workspace_events`、`timeline_streams`、`event_subscriptions` | DB trigger `notify_workspace_change`；`common.append_event` | 正常 append 唯一；DSAR 可改 payload |
| Inbox | 无表；`workspace_events.targets` + `watermarks` | `inbox.py`、`digest.py`、`leases.py`（转移） | 投影，非存储事实 |
| Runner | `runner_hosts`、`runner_placements` | `runners.py`、`scheduler.py` | 是（各一服务） |
| 质量/审批 | `quality_gate_runs`、`audits` | `quality.py`、`tasks.py` | gate 单函数；审批状态在 task |
| 预算/计量 | `workspace_budgets`、`usage_ledger`、`rate_limit_windows` | `budget.py`、`token_budget.py` | 大体是 |
| Webhook/外部动作 | `webhook_rules`、`webhook_deliveries`、`external_actions`、`external_action_attempts` | `webhooks.py`、`external_actions.py` | 是 |
| Blob/分享 | `blobs`、`blob_uploads`、`blob_upload_parts`、`share_links`、`share_link_accesses` | `blobs.py`、`share_links.py` | 是 |
| 消息 | `conversations`、`conversation_members`、`messages`、`message_deliveries` | `messaging.py` | 是 |
| 治理 | `commander_grants`、`constitution_acks`、`access_audit`、`alias_redirects`、`tombstones`、`cleanup_grants`、`grant_snapshots` | 多个治理服务 | 部分 |
| 成本/增长/事件其他 | `path_policies`、`taint_edges`、`event_subscriptions` 等 | 多个 | 部分 |

### 2.2 任务状态的多写入方（关键缺陷）

| 写入方 | 写入内容 | 证据 |
|---|---|---|
| `services/tasks.py` | claim IN_PROGRESS、release READY、handoff、review、cancel **FAILED**、reassign、admin update | `tasks.py:678-709, 858-877, 988-1105, 1221-1386, 1638-1738, 1914-1952` |
| `services/leases.py` | lease 回收/孤儿任务 → READY/ESCALATED | `leases.py:542-571` |
| `services/collab.py` | 子任务取消 CANCELLED、escalation 任务 AWAITING_HUMAN、恢复 WAITING_HUMAN→IN_PROGRESS | `collab.py:655-688, 758-792, 869-917` |
| `services/scheduler.py` | placement 失败 → HANDOFF | `scheduler.py:421-438` |
| `services/reviews.py` | peer review 直接 DONE | `reviews.py:148-190` |
| `services/diagrams.py` | 原始 SQL 取消 → CANCELLED | `diagrams.py:1236-1251, 1946-1959` |
| `services/audit.py` | undo 恢复旧 status | `audit.py:280-285, 320-485` |
| `services/quality.py` | review 后可能改 AWAITING_HUMAN | `quality.py:434-451` |
| `api/admin_routes.py` | 管理动作 | admin routes |
| DB trigger | 只 enforce lease/brief/terminal immutability，不校验转换 | `sql/migrations/v3_41_task_invariants.sql:40-110` |

**结论：任务状态没有单一写入权威，也没有合法转换矩阵。** Phase 1 必须建立 `TaskCommandService`（或等价命令层），所有状态写入经它完成，并记录 expected_version/actor/reason/event_id。

### 2.3 事件与 inbox 的权威

- `workspace_events` 由 PostgreSQL trigger 在业务事务内写入；`seq` 由 deferred trigger 按 workspace 分配，commit 顺序、无 gap（`sql/migrations/v3_17_events.sql`）。
- `event_id` 是独立 `evt_<ULID>`，唯一索引。
- 投递语义：至少一次；消费者按 event_id 去重、按 seq 续读；没有 exactly-once 承诺。
- Inbox 是 `workspace_events.targets` 的投影，读取位置是 watermark；没有 inbox 表。
- Webhook 有 `webhook_deliveries` 幂等键和 DEAD 状态；没有 DEAD 重放 API。
- **缺口：** 没有 correlation_id / causation_id / 统一 aggregate_version；SSE 使用 `row.id` 而非 `seq`；DSAR 可改写 `workspace_events.payload`。

### 2.4 审计的权威

- `state_audit_diffs`/`audits` 通过 `record_audit` 写入，但没有 append-only 数据库触发器；`deploy_gate_audit`、`redaction_log`、`incident_notices` 有 append-only 触发器。
- `rights.py` 可在 DSAR 流程中临时禁用触发器并改写任务文本/事件 payload（`rights.py:707-714, 936-943, 1216-1225`）。
- **结论：** 审计不是密码学/数据库级不可变；对合规场景需补 append-only + 签名/哈希链。

---

## 3. Kernel 数据所有权

### 3.1 表与写入方

| 表 | 内容 | 主要写入方 | 唯一权威？ |
|---|---|---|---|
| `workspaces` | repo_path、main_branch、head_commit_sha | admin 创建；runtime reconcile；`on_task_committed`；merge coordinator reconcile | 大体是 Kernel；HEAD 有多个修复路径 |
| `code_artifacts` | 文件/符号/类型/状态/签名/内容哈希/version | admin artifact 创建、artifact_sync、verification_gate、mutator | 多路径；version 递增需审计 |
| `dag_tasks` | DAG 任务、状态、锁、lease、base/result commit、retry | task_factory、scheduler、mutator、verification_gate、merge_coordinator | **多写入方**（见状态机文档） |
| `task_dependencies` | 上游/下游 | task_factory、mutator | 是 |
| `symbol_edges` | caller/callee/fields | artifact_sync、edge_rebuild | 是（异步重建） |
| `kernel_events` | 事件日志（BIGSERIAL 全局 seq） | EventBus | 是 |
| `agent_tokens` | runner token 哈希/label/active | admin token 创建；bootstrap；无 revoke API | 部分 |
| `patch_submissions` | v2 提交、patch、幂等、状态、gate 结果、merge 结果 | submission_service 接受；gate_worker 更新 | 大体是（gate worker + accept） |
| `gate_work_leases` | work lease（若使用；当前 gate_worker 主要用 patch_submissions 列） | gate_worker | 需核对实际使用 |
| `merge_intents` | 已验证 patch、base/task commit、gate 结果、merge 状态 | gate_worker 创建；merge_coordinator 更新 | 是（coordinator fencing） |
| `merge_coordinators` | per-workspace 选举/lease/fencing token | merge_coordinator | 是 |
| `admission_permits` | 准入 permit | scheduler | 是 |
| `scheduler_authorities` | 每 workspace authority lease | scheduler_core_manager | 是（但 token 不 fence 写） |
| `edge_rebuild_jobs` | 持久化 edge 重建 | edge_rebuild | 是 |
| `event_outbox` | outbox → pg_notify | EventBus 写入；outbox dispatcher 标记 | 是 |
| `schema_migrations` | 迁移版本 | v4_002–v4_005 写入；v4_006–008 未写 | **不完整** |

### 3.2 Kernel 任务状态写入方

| 转换 | 写入方 | 证据 |
|---|---|---|
| 创建 → READY/BLOCKED | `task_factory.py` | `task_factory.py:31` |
| SPLIT 子任务 READY/BLOCKED；父 SPLIT_ARCHIVED | `mutator.py` | `mutator.py:143, 232-245` |
| ESCALATE 报告者 SUSPENDED_ESCALATED；受害者 BLOCKED；修订 READY | `mutator.py` | `mutator.py:369-438` |
| INJECT 任务 BLOCKED；新节点 READY | `mutator.py` | `mutator.py:496-544` |
| READY → LEASED | `scheduler.py` | Rust/SQL claim |
| LEASED → READY/FAILED | `scheduler.release_task` / reaper | `scheduler.py:668-690, 747-784` |
| VERIFYING → READY/FAILED | stuck verifier reaper | `scheduler.py:692-721` |
| VERIFYING → COMMITTED | `scheduler.on_task_committed` | `scheduler.py:619-655` |
| LEASED → VERIFYING | `verification_gate` | `verification_gate.py:234-259` |
| VERIFYING → LEASED/FAILED | `verification_gate._reject` | `verification_gate.py:1302-1338` |
| VERIFYING → LEASED | `merge_coordinator._revalidate` | `merge_coordinator.py:431-451` |
| COMMITTED → LEASED/FAILED | `merge_coordinator` reconcile | `merge_coordinator.py:617-641` |

**结论：Kernel 也没有单一状态命令层。** 多个路径直接写 `dag_tasks.status`，部分无行锁/版本比较（`verification_gate._reject` ORM 读改写）。

### 3.3 Runtime authority vs DB

- PostgreSQL 是持久化事实；Rust `SchedulerCore` 是可重建的内存计算状态。
- Rust 决策后，PostgreSQL 条件 UPDATE 是最终 fence（`status='READY' AND lease_token_hash IS NULL AND NOT EXISTS(锁重叠)`）。
- `scheduler_authorities.fencing_token` 只用于选举/续租，不参与任务写 fencing。
- 重启后 Rust 状态从 DB 重建；API lifespan 预热所有 workspace。
- **结论：** “Rust 是权威调度器”只在单进程/单 authority 的决策意义上成立；任务事实权威仍是 PostgreSQL。目标文档应明确这一点，避免把 Rust 内存当唯一事实。

---

## 4. 跨域身份链

当前可用 ID：

```text
AgentGraph tenant_id
  -> workspace_id
    -> collab_task.task_key / id
      -> input_context.kernel.workspace_id
      -> input_context.kernel.task_id
        -> Kernel dag_task.task_id
          -> patch_submissions.submission_id
            -> merge_intents.merge_id
              -> workspaces.head_commit_sha / dag_tasks.result_commit_sha
```

缺口：

- 没有 `project_id`、`repository_id`、`coding_session_id`、`agent_run_id`、`workflow_id`、`workflow_version`、`attempt_id`、`verification_run_id`、`delivery_id`；
- 没有统一 `correlation_id` 贯穿产品任务→Kernel task→submission→commit；
- AgentGraph 不保存 Kernel submission id；Kernel 不保存 AgentGraph task key；
- 跨域关联靠 `input_context` JSONB，不能被数据库 FK/约束保护；
- 代码任务没有独立的 execution attempt 表，只有 `retry_count`。

---

## 5. 派生/投影与双写

| 事实 | 权威 | 派生/投影 | 风险 |
|---|---|---|---|
| AgentGraph task status | AgentGraph | Kernel 无投影 | Kernel 运行期间 AgentGraph 仍 READY |
| Kernel task status | Kernel | AgentGraph `get_run` 按需读取 | 无事件投影；失败/取消不同步 |
| Gate 结果 | Kernel `gate_results` | AgentGraph `quality_gate_runs` | code_engine 合成 PASS，非真实读取 |
| Commit | Kernel Git/`dag_tasks.result_commit_sha` | AgentGraph task brief/quality evidence | commit 与产品任务无硬关联 |
| Inbox | `workspace_events.targets` | watermark | 事件 payload 可被 DSAR 改写 |
| Capacity metrics | 运行进程内存 | bench JSON | 无持久化 metric 历史 |

**没有业务双写**（AgentGraph 与 Kernel 各写自己的 DB），但存在**投影缺失**和**合成证据**。

---

## 6. 幂等与副作用

### 6.1 已有幂等

- AgentGraph MCP 写工具：`idempotency_key` + 24h 请求/响应记录（`services/idempotency.py`）。
- AgentGraph webhook：`(rule_id, event_id)` 唯一 + 幂等键。
- Kernel v2 submission：`(task_id, lease_token_hash, patch_sha256)` 绑定 + `idempotency_key` 唯一；重复返回原结果。
- Kernel merge intent：`submission_id` 唯一；coordinator token fencing。
- Kernel edge job：持久化 + lease + max attempts。
- AgentGraph external action：intent/attempt/补偿。

### 6.2 缺口

| 操作 | 缺口 |
|---|---|
| AgentGraph 任务 claim/review/cancel | 部分无 expected_version；review 不要求 lease；cancel 无 fencing |
| Kernel claim | 无 expected_version；由条件 UPDATE 保护 |
| Kernel submission accept | 先 SELECT 后 INSERT，无行锁；TOCTOU（gate worker 会复核） |
| Kernel heartbeat | 不检查 `lease_expires_at > NOW()`，可复活过期未回收 lease |
| Kernel SPLIT/ESCALATE/INJECT | 只检查 `status='LEASED'`，不检查 token/expiry |
| Kernel merge | 不复核 task lease/submission status；可能合并已失败/已取消（虽然取消不存在）的提交 |
| AgentGraph→Kernel 创建 code run | artifact/task 创建非原子；Kernel task 创建成功但 AgentGraph task 创建失败会留下孤儿 Kernel task |
| AgentGraph gate 回写 | 无幂等/证据校验；重复 sync 可能重复写 gate？`quality.record_gate_result` 需检查唯一约束 |

### 6.3 外部副作用

- 模型 Provider 调用在外部 runner 进程，平台不持久化 invocation；超时/重试语义由 runner 负责。
- Git commit 是外部副作用：回滚不能假装撤销 commit；Kernel 用 CAS/reconcile 处理。
- Webhook/外发动作：有 outbox/重试/DEAD；无人工重放。

---

## 7. 数据保留、隐私与迁移

### 7.1 保留

- AgentGraph 事件默认保留 7 天（`event_retention_days`），过期后 cursor 410，需要 resync；没有 event DLQ。
- Kernel `kernel_events` 无 retention/pruning；长期运行会膨胀。
- AgentGraph backup/PITR 有测试（`tests/test_durability.py`）；Kernel 没有备份/恢复测试。
- 对象存储/大型模型输入输出未形成独立保留策略。

### 7.2 隐私

- AgentGraph 有 DSAR/eraser/redaction（`rights.py`、`redaction_log`），但会绕过普通写入路径。
- Kernel 事件/submission 保存 `git_diff`、`gate_results`、`error_detail`；没有租户隔离和保留策略。
- 密钥/凭证：Kernel token 只存哈希；AgentGraph token 只存哈希；但默认 dev key 和 HTTP 明文是风险。

### 7.3 迁移

- AgentGraph 69 个迁移，顺序应用；无统一 down migration；生产迁移需手工备份。
- Kernel `init_db` 每次启动重放 genesis + 所有迁移，依赖幂等；`schema_migrations` 仅 v4_002–005 记录，v4_006–008 缺失记录。
- 没有迁移回滚脚本/演练；Phase 1/4 必须补。

---

## 8. 数据权威结论

1. **AgentGraph 任务状态、lease、inbox watermark 没有单一写入权威。**
2. **Kernel DAG task 状态有 6+ 写入方，多个路径缺少行锁/版本比较。**
3. **AgentGraph 与 Kernel 之间没有稳定身份链，只有 `input_context` JSONB 映射。**
4. **Gate 证据存在合成路径，不能作为可信交付证据。**
5. **事件至少有可用的 at-least-once 基础，但两套协议不一致，缺 correlation/causation、DLQ、统一投影。**
6. **Kernel 无租户列/隔离，submission/task 读取可跨 workspace。**
7. **迁移 ledger 不完整，Kernel 无备份/恢复演练。**

Phase 1 必须先定义**身份链 + 状态权威矩阵 + 事件 envelope + 幂等语义**，再改任何表结构。
