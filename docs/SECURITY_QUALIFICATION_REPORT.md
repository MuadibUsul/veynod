# 安全与多租户隔离资格报告

日期：2026-11-01。范围：`production/qualification` 分支（基于 `main@fb398ba`）。
方法：逐条审查代码边界 + 真实数据库隔离测试（无 mock 的跨 workspace 访问）。

---

## 1. 边界模型（代码事实）

| 身份 | 用途 | 能做什么 | 证据 |
|---|---|---|---|
| Human session / workspace key | 产品面 | 自己 workspace 的 task/document/project；不能进 `/v1/admin` | `AgentGraph/app/auth.py`，`test_agent_isolation.py` |
| Admin token (`ADMIN_TOKEN`) | 产品运维 | 创建 workspace、签发 key、重放事件 | `app/api/admin_routes.py` |
| Kernel admin key (`VN_ADMIN_API_KEY`) | Kernel 运维 | 建 workspace/artifact/task、读 evidence、取消 | `veynod_kernel/app/api/admin_routes.py` |
| Kernel runner token (`vnk_...`) | Runtime/外部 runner | syscall 平面；绑定一个 workspace | `app/auth.py::authenticate_worker_context`，`syscalls.py::get_authenticated_worker_id` |
| Kernel service key (`VN_RUNTIME_SERVICE_KEY`) | Agent Runtime | 事件流、任务创建、evidence、取消（workspace 头限定） | `app/api/runtime_routes.py` |
| Runtime API key (`RUNTIME_API_KEY`) | AgentGraph | `/v1/runs/*`、events、usage | `agent_runtime/api.py::require_key` |

关键约束：

* **Runtime 不持有 Kernel admin key**：`agent_runtime/` 中没有任何 `KERNEL_ADMIN_KEY` 读取；
  代码检索 `grep -rn "admin_key" agent_runtime/` 为空（只有工具侧 `service_key`）。
* 模型只能调用注册表内的 12 个工具，参数在任何 HTTP 请求之前校验
  （`agent_runtime/tools/kernel.py::CAPABILITIES`、`execute`）。
* Kernel lease token 从不进入模型上下文：`_do_pull_task_bundle` 删除 `lease_token`，
  存入 `agent_runtime.kernel_leases`（`test_pull_task_bundle_redacts_lease_and_persists_it`）。
* 每个 Kernel 请求都带 `X-Workspace-Id`；runner token 与 workspace 不匹配直接 403
  （`syscalls.py::get_authenticated_worker_id`）。
* 跨实例证据读取按任务绑定选择 base URL 与对应 admin key
  （`task_lifecycle.collect_evidence`、`code_engine._engine(base_url)`）。

---

## 2. 测试结果

命令：

```bash
# Kernel 平面（真实 PostgreSQL，两个 workspace、两个 token）
cd veynod_kernel && python -m pytest tests/test_security_isolation.py tests/test_runner_file_security.py tests/test_submission_guard.py -q
# 产品平面（两个 workspace）
cd AgentGraph && TEST_DATABASE_URL=... .venv/Scripts/python.exe -m pytest tests/test_agent_isolation.py -q
```

| 检查 | 预期 | 结果 | 证据 |
|---|---|---|---|
| 另一 workspace 的 runner 读任务 | 404 | PASS | `test_security_isolation.py::test_a_runner_cannot_read_another_workspaces_task_evidence_or_files` |
| 另一 workspace 的 runner 读 evidence | 404 | PASS | 同上 |
| 另一 workspace 的 runner 读文件 | 404 | PASS | 同上 |
| 显式路由到别的 workspace | 403 | PASS | 同上（`X-Workspace-Id` 与 token 不符） |
| 提交写集之外的 patch | 403 | PASS | `test_a_patch_outside_the_write_set_is_refused` |
| 无 admin key 访问管理面 | 401/403 | PASS | `test_admin_and_service_planes_reject_unknown_callers` |
| 错误 service key 访问 runtime 面 | 401 | PASS | 同上 |
| `rev` 参数注入（`--output=`、`-c`、`../`） | 422 | PASS | `test_runner_file_security.py` |
| 另一 workspace 读文档/任务/发起 Run | 404 | PASS | `test_agent_isolation.py` |
| workspace key 访问产品管理面 | 401/403 | PASS | 同上 |
| 模型调用未注册工具 | 拒绝（AUTH） | PASS | `agent_runtime/tests/test_kernel_tool.py::test_unknown_or_disallowed_tool_is_rejected` |
| lease token 出现在工具输出 | 不出现 | PASS | `test_pull_task_bundle_redacts_lease_and_persists_it` |
| 同一任务并发第二个提交 | 409 / 去重 202 | PASS | `veynod_kernel/tests/test_submission_guard.py` |

---

## 3. 发现与处置

### S-1（High，已修复）`read_file` 的 `rev` 允许 git 选项注入

`/v1/runner/workspaces/{ws}/file?rev=` 原样进入 `git show {rev}:{path}`；
`--output=<file>` 可以让 git 写文件。修复：`git_ops.validate_rev` 只接受
十六进制对象名或普通 ref（拒绝 `-` 开头、`..`、控制字符），路由映射为 422。
测试覆盖 5 种恶意输入。

### S-2（High，开放）Gate `local` 沙箱与 Kernel 同容器

`gate_sandbox._run_local` 在 Kernel 容器内执行仓库自带测试；恶意仓库可以读
`/repos` 下其他 workspace 的 bare repo、访问 `kernel-api`/`runtime-api`/db 的内网地址。
单租户部署可接受；**多租户生产不可接受**。处置建议（本轮未实现）：

1. `VN_GATE_SANDBOX=docker` + 独立 gate 镜像，每个 gate 只挂载自己的 worktree
   （需要 Docker socket 或 DinD；宿主隔离更强）。
2. 或每个 workspace 一个 cell/节点，仓库与 gate 不共享内核命名空间。

在实现之前：多租户部署**禁止** `VN_ALLOW_LOCAL_GATE_IN_PROD=1`；单租户部署必须在
文档中明确这是已知限制。

### S-3（Medium，接受）仓库文件可对模型构成提示注入

`read_file` 内容进入 checkpoint 与模型上下文。模型可能被仓库文本诱导调用其他工具；
但写集、工具白名单、lease 与门禁仍在服务端强制。残余风险：模型可能浪费调用或
提交无用 patch（门禁拒绝）。没有引入新的越权面。

### S-4（Medium，已缓解）`kernel_base_url` 的 SSRF 面

绑定由产品管理员通过 `POST /v1/projects/{p}/repositories` 创建；Runtime 只使用
Run 携带的绑定，模型无法提供 URL。风险等价于"管理员可以配置任意内网地址"。
建议生产用出站白名单（`OUTBOUND_ALLOWED_HOSTS` 已有于 AgentGraph）。

### S-5（Low）错误信息

Kernel/Runtime 的 HTTP 错误详情截断到 300–800 字符；模型/runner 看不到 DB DSN 或密钥。
未发现把环境变量写入响应的路径（人工审阅 gateway、runtime_routes、admin_routes）。

### S-6（Info）RLS

生产 compose 打开 `RLS_ENABLED=true` + `RLS_ROLE=agentgraph_app`；本轮测试环境以
owner 身份运行，因此 RLS 本身未被覆盖。租户隔离测试验证的是应用层与数据库派生列。

---

## 4. 结论

* 测试范围内**没有未处置的 Critical**。
* 唯一未处置 High 是 **S-2 Gate 本地沙箱**，其风险边界已写清：单租户可接受，
  多租户必须启用容器沙箱或 cell 隔离。
* 已修复的 High：S-1 `rev` 注入。
