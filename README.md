# Veynod — AgentGraph 控制面 + Kernel 核心引擎

> **一个产品，两个 bounded context。**
>
> - **AgentGraph / Veynod 3.x**：身份、工作区、任务、审计、人类控制台。
> - **Veynod Kernel 4.x**：仓库、AST 契约、符号锁、确定性门禁、Git 原子合入。
>
> Kernel 是 **headless code engine**，没有自己的登录、dashboard 或产品壳；AgentGraph 后端直接调用它。`veynod_platform/` 是早期独立网关原型，已不在主链路上。

完整的项目架构、目录路径、数据边界和容量现状见 [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md)。

---

## 1. 现在的拓扑

```text
浏览器 / Agent
      │  只登录 AgentGraph（/dashboard/）
      ▼
AgentGraph（FastAPI + Next.js console）
  ├─ identity / workspace / task / audit / SSE
  ├─ /v1/code/*  ──────────────┐   AgentGraph 的 Code 页面
  └─ code_engine service        │
                                ▼
                     Veynod Kernel（headless）
                       /v1/admin/*（AgentGraph 内部调用）
                       /v1/syscalls/* 或 /mcp（Agent/Runner）
                       Git / worktree / AST / gate / commit
```

- **用户入口**：`http://127.0.0.1:8000/dashboard/`
- **Code 页面**：`http://127.0.0.1:8000/dashboard/code/`
- **Kernel**：`http://127.0.0.1:8001`，只作为引擎，不作为产品入口
- **Platform 原型**：`http://127.0.0.1:8010`，已停用；保留代码作参考

---

## 2. Kernel 如何成为 AgentGraph 的核心引擎

新增：

```text
AgentGraph/app/services/code_engine.py
AgentGraph/app/api/code_routes.py
AgentGraph/app/api/code_page.py
AgentGraph/app/templates/code.html
AgentGraph/dashboard/app/page.tsx   # 侧边栏 Code 入口
```

`AgentGraph` 直接持有 Kernel 的：

```text
KERNEL_URL
KERNEL_ADMIN_KEY
KERNEL_WORKSPACE_ID
KERNEL_REPO_PATH
```

不再经过 Platform。

### Code run 生命周期

```text
1. 用户/Agent 在 AgentGraph 创建 code run
2. AgentGraph 调 Kernel admin：
   - 创建 target/test artifacts（FROZEN）
   - 创建 Kernel dag_task
3. AgentGraph 创建 product task，input_context 记录 kernel workspace/task
4. Kernel Runner 领任务、改代码、跑冻结测试、门禁、commit
5. AgentGraph GET /v1/code/runs/{task}
   - 读取 Kernel task
   - COMMITTED → 回写 CI/TESTS PASS
   - claim → handoff REVIEWING
6. 人类/审查者在 AgentGraph 做最终 DONE
```

**AgentGraph 是产品事实，Kernel 是代码执行事实。**

---

## 3. 实际跑通的结果

最近一次 **直接经由 AgentGraph → Kernel**（不经 Platform）：

```text
AgentGraph task: code-284425ab8f1343fb
Kernel task:     7a086cfe-7230-40e1-95b3-4a7714feb13f
Kernel commit:   13607c9f0231164500dc9e576fdf2a3f57a95307
AgentGraph status: REVIEWING
Gates: CI PASS, TESTS PASS
```

Code 页面：

```text
http://127.0.0.1:8000/dashboard/code/
```

页面复用 AgentGraph 人类登录会话；不需要第二个账号。Kernel 没有自己的 UI 入口。

---

## 4. 为什么 Kernel 不做 UI

Kernel 的接口是给 Agent/内部服务用的：

- `pull_task_bundle`
- `submit_patch`
- `request_task_split`
- `escalate_contract_defect`
- `inject_dependency_task`
- `query_symbol_signature`

它只需要 `/healthz`、指标和日志。  
Dashboard / Code 页面属于 **AgentGraph 的人类控制面**，不是 Kernel 的产品面。

---

## 5. 目录

```text
E:\Codes\veynod\
├── AgentGraph\                 # 产品控制面（3.24.0）
│   ├── app/services/code_engine.py   # Kernel 作为核心引擎的适配层
│   ├── app/api/code_routes.py        # /v1/code/*
│   ├── app/api/code_page.py          # /dashboard/code/
│   ├── app/templates/code.html
│   └── dashboard/app/page.tsx        # Code 侧边栏入口
├── veynod_kernel\              # headless code engine（4.0）
├── veynod_platform\            # 早期独立网关原型（已不在主链路）
└── README.md
```

---

## 6. 启动

### AgentGraph

```bash
cd E:/Codes/veynod/AgentGraph
docker compose up -d
```

需要在 compose / 环境变量中配置：

```text
KERNEL_URL=http://host.docker.internal:8001
KERNEL_ADMIN_KEY=dev-admin
KERNEL_WORKSPACE_ID=<kernel-workspace-id>
KERNEL_REPO_PATH=<bare-repo-path>
```

### Kernel

```bash
cd E:/Codes/veynod/veynod_kernel
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_dev'
export VN_AUTH_MODE=dev
export VN_ADMIN_API_KEY=dev-admin
export VN_DEFAULT_WORKSPACE_ID='<kernel-workspace-id>'
export VN_FAST_FORWARD_MERGE=true
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Kernel 的单 workspace 协议意味着：**一个 Kernel 实例服务一个代码仓库**。多仓库要么多实例，要么后续给 syscall 加 workspace 维度。

### 创建 Kernel workspace

```bash
curl -s -X POST http://127.0.0.1:8001/v1/admin/workspaces \
  -H 'X-Admin-Key: dev-admin' -H 'Content-Type: application/json' \
  -d '{"name":"Code Plane","repo_path":"/absolute/path/to/repo.git","main_branch":"main"}'
```

把返回的 `workspace_id` 填到 `VN_DEFAULT_WORKSPACE_ID` 和 AgentGraph 的 `KERNEL_WORKSPACE_ID`，重启两边。

### 创建 code run

```bash
curl -s -X POST http://127.0.0.1:8000/v1/code/runs \
  -H 'Authorization: Bearer <AgentGraph session or workspace key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Implement quad",
    "repo_path": "E:/Codes/veynod/.run/integration2.git",
    "symbol": "src/calc.py::quad",
    "test_symbol": "tests/test_quad.py::test_quad",
    "spec": "Implement quad(a) so the frozen test passes."
  }'
```

然后启动 Kernel Runner（见 Kernel README），最后 `GET /v1/code/runs/{task}` 同步。

---

## 7. API

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/dashboard/code/` | AgentGraph 人类 Code 页面（可选） |
| GET | `/v1/code/runs` | 列出 Kernel code runs |
| POST | `/v1/code/runs` | 创建 AgentGraph task + Kernel task |
| GET | `/v1/code/runs/{task}` | 读取/刷新/同步状态 |
| POST | `/v1/code/runs/{task}/sync` | 强制刷新并同步 |
| GET | `/v1/code/runs/{task}/audit` | AgentGraph gate/audit 视图 |

---

## 8. 边界

**AgentGraph 拥有：** 用户、租户、workspace、task、文档、审批、审计、SSE、控制台。

**Kernel 拥有：** repo、artifact、AST 契约、符号锁、门禁、commit。

**没有合并：** 数据库 schema、任务表、租约表、事件总线、两个 dashboard。

**仍然限制：**

- 一个 Kernel 实例一个代码仓库；
- AgentGraph task 到 REVIEWING 后，DONE 仍由人类/审查者决定；
- Kernel 的 6 个 syscall 目前不携带 workspace_id；
- Kernel 的 dashboard 应作为 debug 工具，不对外暴露；
- `veynod_platform/` 是早期原型，不是主链路，可删除或归档。

---

## 9. 验证

```bash
# AgentGraph health
curl -s http://127.0.0.1:8000/healthz

# Code page
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/dashboard/code/

# Direct engine run
curl -s http://127.0.0.1:8000/v1/code/runs/code-284425ab8f1343fb \
  -H 'Authorization: Bearer <AgentGraph workspace key>'
```

当前已验证：

```text
AgentGraph 3.24.0
Kernel 4.0.0 (Rust core available)
direct run: REVIEWING
gates: CI PASS, TESTS PASS
```

一句话：

> **Kernel 不再是独立产品，而是 AgentGraph 的一处 headless 核心引擎；AgentGraph 是唯一产品入口，Kernel 只通过 API/MCP 给 Agent 和内部服务使用。**
