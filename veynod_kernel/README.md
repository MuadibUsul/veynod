# Veynod Kernel v4.0

> 双偶契约 DAG · 写集正交调度 · AST 上下文切片 · 确定性编译门禁
>
> 面向"单人驾驭千级 Agent 开发百万行项目"的分布式编译状态机。Agent 侧只有 6 个强类型系统调用（REST + MCP 同一套 Pydantic Schema），没有主观 Code Review，合入主干只由四阶段确定性门禁决定。
>
> 面向架构评审的详细文档（架构、时序、一致性、故障矩阵、项目路径、评审清单）：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

本仓库是 EDD 的可运行实现，包含：

- PostgreSQL 16 创世 Schema、GIN 写集索引、递归符号图
- `app/engine/`：层级锁代数、O(1) 写集正交调度器、SPLIT/ESCALATE/INJECT 图变异器
- `app/services/`：tree-sitter AST 切片、四阶段门禁 + Git CAS 原子合入、不可变 Git/AST 快照缓存、异步符号边重建、SSE 事件总线、制品/符号图同步
- `rust/veynod_core/`：可选 PyO3 Rust 核心（锁代数/快照规划、AST 切片与单次解析 `file_snapshot`、门禁命令监督），扩展缺失时纯 Python 回退
- `app/mcp_server/syscalls.py`：6 个系统调用原语与自动生成的 MCP JSON-RPC 桥
- `app/engine/kernel_service_impl.py`：`PostgresKernelEngineService`（底层引擎的 SQLAlchemy 实现）
- 管理面、Runner 面、只读 DAG 面 API；Node.js Runner 守护进程；单文件监控画布
- 105 个测试：Python 单元测试 + Rust 对拍/权威调度核心/全仓扫描/超时沙箱 + 真实 Postgres/Git/pytest 端到端集成测试 + 容量验收工具测试
- 100 Runner 压测基线与 merge/claim/gate 细分计时见 [docs/STRESS_TEST.md](docs/STRESS_TEST.md)
- **可信容量验收工具**（隔离数据库、客户端/DB 双口径、退出码门禁、0/1000 SSE 对照）见 [docs/BENCH_ACCEPTANCE.md](docs/BENCH_ACCEPTANCE.md)
- **版本化异步提交协议 v2**（202 + 不可变补丁 + 幂等键 + 重启恢复）见 [docs/ASYNC_SUBMISSIONS.md](docs/ASYNC_SUBMISSIONS.md)
- **独立门禁 Worker**（工作租约/续租/退避/过期回收、fencing、Docker/local 沙箱、AST 范围核验）见 [docs/GATE_WORKER.md](docs/GATE_WORKER.md)
- **每仓库合并协调器**（持久化 merge intent/result、fencing、Git/DB 双顺序崩溃对账、依赖变化重验证）见 [docs/MERGE_COORDINATOR.md](docs/MERGE_COORDINATOR.md)
- **横向扩容与准入控制**（429/延迟重试、单仓库唯一 Rust 权威、持久化 edge job、事务 outbox、SSE 游标续传）见 [docs/HORIZONTAL_SCALE.md](docs/HORIZONTAL_SCALE.md)
- **1000 Runner 容量与瓶颈**（生产 profile 分层实测、accept P99、批处理 Git、门禁/合并吞吐与分片建议）见 [docs/CAPACITY_1000.md](docs/CAPACITY_1000.md)

---

## 1. 15 行装配：如何挂载并接入 `app/engine/`

`app/main.py` 的全部内容（9 个非空行）：

```python
from fastapi import FastAPI
from app.runtime import lifespan
from app.api import api_router
from app.mcp_server.syscalls import router as syscalls_router, get_kernel_engine
from app.engine.kernel_service_impl import PostgresKernelEngineService

app = FastAPI(title="Veynod Kernel v4.0", version="4.0.0", lifespan=lifespan)
app.include_router(syscalls_router)
app.include_router(api_router)
app.dependency_overrides[get_kernel_engine] = PostgresKernelEngineService.from_session_dep
```

等价于 EDD 中给出的最小装配：

```python
from fastapi import FastAPI
from app.mcp_server.syscalls import router as syscalls_router, get_kernel_engine
from app.engine.kernel_service_impl import PostgresKernelEngineService

app = FastAPI(title="Veynod Kernel v4.0", version="4.0.0")
app.include_router(syscalls_router)
app.dependency_overrides[get_kernel_engine] = PostgresKernelEngineService.from_session_dep
```

### 对接原理

1. `syscalls.py` 定义抽象接口 `KernelEngineService`，7 个 async 方法覆盖 6 个原语（`submit_patch` 额外使用 `verify_active_lease`）。
2. `PostgresKernelEngineService` 继承该接口，构造函数只接收一个 `AsyncSession`。
3. `from_session_dep` 是一个 **classmethod FastAPI 依赖**：

```python
@classmethod
async def from_session_dep(
    cls,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> "PostgresKernelEngineService":
    return cls(session)
```

FastAPI 在 `dependency_overrides` 中看到它，会按签名解析 `get_db_session`，**每个请求构造一个绑定请求级事务的引擎实例**。因此 Router 与引擎之间没有全局可变状态，连接/事务边界完全由请求作用域管理。

4. 引擎方法内部再装配 `app/engine/` 与 `app/services/`：

| 接口方法 | 底层实现 |
| :--- | :--- |
| `lease_next_disjoint_task` | `DisjointWriteSetScheduler.claim_next_orthogonal_task` + `ASTContextSlicer` + `_fit_bundle` |
| `verify_and_commit_patch` | `DeterministicVerificationGate.verify_and_merge_patch`（写集守卫 → 契约哈希 → 类型检查 → 测试 → Git CAS 合入） |
| `execute_graph_split` | `GraphMutator.split_task_into_subdag`（写集守恒 + 子图无环 + 并发子任务正交 + 汇点重连） |
| `escalate_and_propagate_stale` | `GraphMutator.escalate_and_propagate_blast_radius`（递归 `symbol_edges` + `TASK_ABORT` SSE） |
| `inject_prerequisite_node` | `GraphMutator.inject_prerequisite_node`（当前节点 BLOCKED + 前置 DESIGN_CONTRACT READY） |
| `lookup_symbol_signatures` | `code_artifacts` 精确/前缀查询，只返回 AST 签名，不返回实现体 |
| `verify_active_lease` | 校验 status=LEASED、runner 身份、过期时间、SHA-256 lease token |

事务边界：引擎方法拥有自己的提交/回滚。`claim` 先在 advisory lock 下原子认领并提交，然后才去读 Git 组装上下文；门禁在最终合入时持有 per-workspace merge advisory lock，并用 `git update-ref <new> <old>` CAS 推进主干。

### 1.1 Python + Rust 混血架构

控制面保持 Python，热路径下沉到 Rust 原生扩展 `veynod_core`（PyO3，abi3-py310）：

| 层 | 实现 | 职责 |
| :--- | :--- | :--- |
| Python 控制面 | FastAPI / SQLAlchemy / auth / SSE | HTTP、MCP、事务、状态机、Git 合并编排 |
| Rust 锁代数 | `rust/veynod_core/src/locks.rs` | 路径规范化、write/ancestor 展开、子集/覆盖/冲突 |
| Rust 权威调度器 | `rust/veynod_core/src/scheduler_core.rs` | 内存任务图、写集索引、blocked 计数、READY 优先队列、claim/revert、1M 节点 |
| Rust AST | `rust/veynod_core/src/ast.rs` | tree-sitter 签名骨架、SHA-256、stub 发现、引用名、8 文件解析树缓存 |
| Rust 门禁 supervisor | `rust/veynod_core/src/sandbox.rs` | 超时、进程组 / `taskkill /T` 终止、输出上限、stdout/stderr 合并 |
| Python 回退 | `app/engine/rust_bridge.py`、`scheduler_core.py` | 扩展缺失或 `VEYNOD_DISABLE_RUST=1` 时切回 SQL/纯 Python |

边界（重要）：

- **Rust `SchedulerCore` 是调度决策的权威**：任务图、锁索引、READY 堆和 claim 选择都在 Rust 内存中完成；单进程内只有一个轻量 `Mutex`，没有 per-task 锁。
- **PostgreSQL 是持久化与 ACID 围栏，不是决策者**：Rust 选中的任务在单 Kernel 模式下直接对独立主键做**单行条件 `UPDATE` + GIN `NOT EXISTS` 复核**（含 `lease_expires_at > NOW()`）。多进程部署时 `scheduler_authorities` 为每个 workspace 选出一个权威实例使用 Rust core，其他实例自动走 SQL/advisory 路径；`VN_SINGLE_KERNEL_CLAIM=false` 可强制所有实例走 SQL 路径。复核失败（陈旧核心快照/跨进程竞争）时重载核心并重试，因此多进程也不会破坏写集正交。
- **版本号来自 DB 时钟**：claim 版本取数据库事务 `NOW()`；核心按 `updated_at` 版本拒绝陈旧同步，避免“同步读到旧 READY 覆盖新 LEASED”的竞态。
- **Rust 沙箱是命令监督器，不是安全沙箱**：它保证超时、进程树清理和输出上限；真正的隔离仍依赖容器/VM。
- 当前单进程内核已经能承载 1M 节点（见下方 1M 测试）。长驻 Rust sidecar 只在多 kernel/federated 部署、或要让多个 API 进程共享同一内存图时才需要；单 kernel 场景不必引入跨进程一致性成本。

构建（可选；不构建则自动使用 Python 回退）：

```bash
python scripts/build_rust.py
# 等价于
cd rust/veynod_core
maturin build --release
pip install --force-reinstall --no-deps target/wheels/veynod_core-*.whl
```

`/healthz` 会返回 `core.available / core.version`；`VEYNOD_DISABLE_RUST=1` 可强制回退用于排障。

**Rust vs Python 微基准**（本机，`python scripts/bench_rust.py`）：

| 操作 | Python | Rust | 说明 |
| :--- | ---: | ---: | :--- |
| 20,000 条锁路径展开 | ~98 ms | ~38 ms | ~2.6× |
| 300 次 AST 签名提取 | ~1.82 s | ~38 ms | ~48×；解析树按文件缓存 |
| claim planner（只读诊断） | 1,000 READY ~199 ms | 100,000 READY ~555 ms | 每任务约 34× |
| **权威 SchedulerCore** | — | **1M upsert ~4.43 s；1,000 claims ~5.1 ms** | `cargo test --release million_node_claim -- --ignored` |

1M 测试结果：`(1,000,000 tasks, 999,000 ready, 1,000 active)`，1,000 次 claim 总计约 5 ms（单次约 5 µs），证明内存图/索引路径不是 O(N) 扫描。

数字只用于量级参考，不是跨机器承诺。

---

## 2. 目录结构

```text
veynod/
├── app/
│   ├── main.py                     # 9 行装配
│   ├── runtime.py                  # lifespan: DDL、token 引导、HEAD 对账、租约回收
│   ├── config.py                   # 所有旋钮（环境变量）
│   ├── database.py                 # async engine/session、创世 Schema 应用
│   ├── models.py                   # 7 张表 ORM + 枚举
│   ├── auth.py                     # vnk_ Runner token（只存 SHA-256）
│   ├── engine/
│   │   ├── rust_bridge.py          # Rust 扩展可选桥接 + 纯 Python 回退
│   │   ├── lock_algebra.py         # :: 规范化、write/ancestor locks、子集/冲突
│   │   ├── scheduler_core.py       # Rust 内存图加载/增量同步/版本防覆盖
│   │   ├── scheduler.py            # Rust claim 决策 + 单行 GIN fence + 单 Kernel 快路径 + SQL 回退
│   │   ├── mutator.py              # SPLIT / ESCALATE / INJECT
│   │   └── kernel_service_impl.py  # PostgresKernelEngineService.from_session_dep
│   ├── services/
│   │   ├── ast_slicer.py           # Python 参考 / Rust 切片器、签名提取、stub 扫描、单次解析 file_snapshot
│   │   ├── verification_gate.py    # 四阶段门禁 + Git CAS 合入 + 锁外快照预计算 + merge 细分计时
│   │   ├── artifact_snapshot.py    # (commit,file) 不可变内容 + FileASTSnapshot LRU
│   │   ├── artifact_sync.py        # 提交后签名/状态同步、符号边重建
│   │   ├── edge_rebuild.py         # symbol_edges 后台队列（merge 临界区外）
│   │   ├── artifact_store.py       # 制品 upsert/签名补齐
│   │   ├── task_factory.py         # 任务插入 + 依赖边 + in_degree
│   │   ├── event_bus.py            # kernel_events + pg_notify + SSE 帧
│   │   ├── event_hub.py            # 单轮询 + 环形缓冲 + Condition fan-out
│   │   ├── worktree_pool.py        # 常驻 gate worktree 池
│   │   └── git_ops.py              # worktree/apply/commit/update-ref + 共享文件缓存 + Rust 命令监督/回退
│   ├── mcp_server/syscalls.py      # 6 原语 + MCP JSON-RPC（initialize/tools 握手）
│   └── api/
│       ├── admin_routes.py         # workspace/token/artifact/task/scan-stubs/edges
│       ├── runner_routes.py        # SSE 抢占流、heartbeat、release、task status
│       └── dag_routes.py           # /v1/dag、/v1/dag/plan、/v1/dag/events、/dashboard、/healthz
├── rust/
│   └── veynod_core/
│       ├── Cargo.toml
│       ├── pyproject.toml
│       └── src/
│           ├── lib.rs               # PyO3 导出
│           ├── locks.rs             # 锁代数 + 只读快照 planner
│           ├── scheduler_core.rs    # 权威内存图/锁索引/READY 堆/claim
│           ├── ast.rs               # tree-sitter 切片 + file_snapshot + 8 文件解析树缓存
│           └── sandbox.rs           # 超时、进程树终止、输出上限
├── sql/v4_001_genesis.sql          # 幂等创世 DDL
├── sql/v4_002…v4_008_*.sql         # 异步提交/工作租约/合并协调/准入·outbox/准入许可迁移
├── scripts/
│   ├── build_rust.py               # maturin build + 安装 veynod_core
│   ├── bench_rust.py               # Rust vs Python 微基准
│   ├── bench_swarm.py              # 容量验收：隔离库 + 客户端/DB 双口径 + JSON 指标 + 非零退出码
│   └── smoke_e2e.py                # 独立 API 端到端冒烟
├── tests/                          # Python 单元 + Rust 对拍 + 集成测试
├── docs/
│   ├── ARCHITECTURE.md             # 架构评审文档
│   └── STRESS_TEST.md              # 100 Runner 压测数据与第二轮结论
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

---

## 3. 快速开始

### 3.1 依赖

- Python 3.10+（EDD 目标 3.12；当前实现兼容 3.10）
- PostgreSQL 16+
- Node.js 22+（Runner/CLI）
- Git 2.30+
- `pyright`、`ruff` 与 `pytest` 在服务端 PATH（门禁 Stage 3/4；可用 `VN_SKIP_TYPECHECK=true` / `VN_SKIP_LINT=true` 临时跳过，但不建议生产使用）

```bash
pip install -e ".[dev]"
python scripts/build_rust.py  # 可选但推荐；没有 Rust 工具链也能启动（自动回退）
docker compose up -d          # PostgreSQL 16 @ 127.0.0.1:5432
cp .env.example .env
# 编辑 .env：VN_ADMIN_API_KEY、DATABASE_URL、VN_AUTH_MODE=prod
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动时 `lifespan` 会：

1. 应用 `sql/v4_001_genesis.sql`（幂等）
2. 可选注册 `VN_BOOTSTRAP_RUNNER_TOKEN`
3. 用 Git 实际 HEAD 对账 `workspaces.head_commit_sha`（恢复 update-ref 后崩溃）
4. 启动租约回收循环（过期 LEASED / 卡死 VERIFYING）

健康检查：`curl localhost:8000/healthz`。

### 3.2 准备被开发仓库（重要：bare mirror）

门禁通过 `git update-ref` 推进主干，因此工作区仓库应当是 **bare mirror**，或至少 `main` 没有在其他 worktree 检出。否则普通仓库的工作区会落后于 HEAD。创建方式：

```bash
git clone --bare /path/to/project /srv/demo.git
```

Runner 也直接使用这个 bare 仓库创建 detached worktree；多机 Runner 可以先 `git fetch` 本地镜像。

### 3.3 创建 Workspace 与 Runner Token

```bash
export VN_ADMIN_API_KEY=change-me
export API=http://127.0.0.1:8000

# Workspace
curl -s -X POST $API/v1/admin/workspaces \
  -H "X-Admin-Key: $VN_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"demo","repo_path":"/srv/demo.git","main_branch":"main"}'
# -> {"workspace_id":"...","head_commit_sha":"..."}
export WS=<上一步返回的 workspace_id>

# Runner token（只返回一次明文，数据库只存 SHA-256）
curl -s -X POST $API/v1/admin/tokens \
  -H "X-Admin-Key: $VN_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"label":"runner-01"}'
# -> {"token":"vnk_...","worker_hub_id":"hub_..."}
```

单 workspace 部署下，6 个 syscall 会自动使用"最早创建的 workspace"；多 workspace 时请设置 `VN_DEFAULT_WORKSPACE_ID`（syscall 协议本身不带 workspace id）。

### 3.4 三种播种方式

**A. 手工创建契约与任务**

```bash
# 冻结一个 stub 契约
curl -s -X POST $API/v1/admin/artifacts \
  -H "X-Admin-Key: $VN_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"workspace_id":"'$WS'","symbol_path":"src/calc.py::add","kind":"stub","status":"FROZEN"}'

# 创建 IMPLEMENT_BODY 任务
curl -s -X POST $API/v1/admin/tasks \
  -H "X-Admin-Key: $VN_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{
    "workspace_id":"'$WS'",
    "title":"Implement add",
    "spec_prompt":"Implement add(a,b) so the frozen unit test passes.",
    "type":"IMPLEMENT_BODY",
    "target_symbols":["src/calc.py::add"],
    "test_symbols":["tests/test_calc.py::test_add"],
    "write_set_symbols":["src/calc.py::add"]
  }'
```

**B. 冷启动扫描：自动生成 2,000 对任务**

```bash
# 先 dry-run 看看会生成多少
curl -s -X POST $API/v1/admin/workspaces/$WS/scan-stubs \
  -H "X-Admin-Key: $VN_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"dry_run":true}'

# 真实生成：每个 `...`/`pass` stub -> WRITE_TEST READY + IMPLEMENT_BODY BLOCKED
curl -s -X POST $API/v1/admin/workspaces/$WS/scan-stubs \
  -H "X-Admin-Key: $VN_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"dry_run":false}'
```

扫描器会：

- 把 stub 制品置为 `FROZEN`
- 为每个符号生成独立的测试文件 `tests/test_<module>__<symbol>__<hash>.py`
- 新文件自动提升为**文件级写锁**，避免多个 WRITE_TEST 同时创建同一测试文件
- 第二阶段完成后，IMPLEMENT_BODY 自动解除 BLOCKED 并进入 READY

**C. 顶层架构拆分**

`DESIGN_ARCHITECTURE` 任务与手写 `DESIGN_CONTRACT` 任务可以通过 `POST /v1/admin/tasks` 创建，L1 Agent 再通过 `request_task_split` 或管理员 API 展开为契约节点。

### 3.5 谁来执行任务

Kernel 不再自带 Runner CLI（ADR-017，2026-11-04 删除）。执行者只有两种，都走同一套
lease → `submit_patch`（v2 异步）→ gate → merge：

- 产品面的电脑（`npx veynod up --run-tasks`）：通过产品 API 的 `/v1/tasks/{key}/kernel/*`；
- Agent Runtime：通过 `agent_runtime/tools/kernel.py`。

### 3.6 监控

- 画布：<http://127.0.0.1:8000/dashboard>
- 只读拓扑：`GET /v1/dag?workspace_id=...`
- 事件游标：`GET /v1/dag/events?after_seq=...`
- OpenAPI：`/docs`

Dashboard 与 DAG 路由是只读且未鉴权的，生产环境应在反向代理层限制访问。

---

## 4. 6 个系统调用

| 原语 | 作用 |
| :--- | :--- |
| `pull_task_bundle` | 在写集正交约束下认领任务，返回 < `max_context_tokens` 的 AST 切片包（只读上游签名、可变 stub、冻结测试、历史门禁报错） |
| `submit_patch` | **v2**：校验租约/写集 + 持久化不可变补丁，立即返回 202；独立 Worker（`python -m app.worker`）运行 4 阶段门禁并原子合入，`GET /v2/submissions/{id}` 查询终态。旧 `/v1` 默认 410+迁移指引 |
| `request_task_split` | 当前任务过大时原地裂变为 2–12 个有向无环子任务；校验写集守恒、无环、并发兄弟写集正交、下游重连到汇点 |
| `escalate_contract_defect` | 上游冻结契约有缺陷时挂起当前节点；沿 `symbol_edges` 递归标记 STALE，阻塞/抢占受影响任务，插入 priority=999 的 `REVISE_CONTRACT` |
| `inject_dependency_task` | 缺少工具函数时在当前节点前插入 `DESIGN_CONTRACT` 前置任务，当前节点转为 BLOCKED 并释放租约 |
| `query_symbol_signature` | 按精确/前缀符号路径查询 AST 签名与 docstring，永不返回实现体 |

REST 路径：前 5 个原语仍为 `/v1/syscalls/*`；提交为 `/v2/syscalls/submit_patch` + `/v2/submissions/*`。`POST /mcp` 由同一注册表自动生成 MCP 工具，支持 `initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call` 握手。

### 拉取与提交示例

```bash
TOKEN=vnk_...

curl -s -X POST $API/v1/syscalls/pull_task_bundle \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"worker_hub_id":"runner-01","supported_task_types":["IMPLEMENT_BODY"],"max_context_tokens":6000}'

# 用 jq 安全构造包含 diff 的 JSON；使用确定性幂等键
PATCH_SHA=$(printf '%s' "$(cat patch.diff)" | sha256sum | cut -d' ' -f1)
KEY=$(printf '%s' "$TASK:$LEASE:$PATCH_SHA" | sha256sum | cut -d' ' -f1)
jq -n --arg d "$(cat patch.diff)" \
  '{task_id:"'$TASK'",lease_token:"'$LEASE'",git_diff:$d,
    touched_symbols:["src/calc.py::add"],local_test_passed:true}' \
  | curl -s -X POST $API/v2/syscalls/submit_patch \
      -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $KEY" \
      -H 'Content-Type: application/json' -d @-
# -> 202 {"submission_id":..., "status_url":"/v2/submissions/..."}

# Gate 在独立进程运行：
# python -m app.worker --workers 8 --database-url "$DATABASE_URL"

# 轮询终态；响应丢失时重复同一 POST 或用 idempotency_key 查询
curl -s $API/v2/submissions/<submission_id> -H "Authorization: Bearer $TOKEN"
curl -s "$API/v2/submissions/find?idempotency_key=$KEY" -H "Authorization: Bearer $TOKEN"
```

202 只表示补丁已持久化。终态响应包含 `status`（`COMMITTED/FAILED`）、`task_status`、`result_commit_sha`、`gate_results`、`error_code`；同一幂等键永远返回同一 `submission_id`。详细契约见 [docs/ASYNC_SUBMISSIONS.md](docs/ASYNC_SUBMISSIONS.md)。
---

## 5. 核心算法与不变量

### 5.1 层级写集锁

规范符号路径使用 `::`：

```text
src/order/pricing.py::PricingEngine::calc_discount
```

任务锁：

```text
write_locks    = {src/order/pricing.py::PricingEngine::calc_discount}
ancestor_locks = {src, src/order, src/order/pricing.py,
                  src/order/pricing.py::PricingEngine}
```

冲突判定在一条 SQL 中完成：

```text
W_a ∩ W_b ≠ ∅  ∨  W_a ∩ A_b ≠ ∅  ∨  A_a ∩ W_b ≠ ∅
```

PostgreSQL GIN 索引 + `&&` 数组重叠 + `SKIP LOCKED`，配合 per-workspace advisory lock 让"认领 + 活跃锁快照"原子化，避免两个并发认领互相看不到对方刚写入的 LEASED 行。

**新文件提升规则**：如果符号所在文件在 HEAD 中不存在，写锁提升为文件级。否则两个不同符号的任务会同时创建同一文件，产生 Git 冲突。

### 5.2 契约守恒

`ast_signature` 是剥离实现体后的骨架（保留装饰器、函数/类签名、类属性、docstring），`signature_hash` 是空白归一化后的 SHA-256。填充函数体不会改变哈希；修改签名会改变哈希，Stage 2 直接拒绝并提示改用 `escalate_contract_defect`。

### 5.3 四阶段门禁

| 阶段 | 检查 | 失败结果 |
| :--- | :--- | :--- |
| 1. WRITE_SET_BOUNDARY | Git apply、空补丁、符号链接、文件级白名单、越权文件 | 拒绝 + 精准越权列表 |
| 2. CONTRACT_HASH_GUARD | 所有 FROZEN/COMMITTED 制品的目标符号必须存在，签名哈希不变（`REVISE_CONTRACT`/`DESIGN_CONTRACT`/`WRITE_TEST` 的目标可改签名） | 拒绝 + 契约篡改说明 |
| 3. STATIC_TYPECHECK | `VN_TYPECHECK_CMD`（默认 pyright）+ `VN_LINT_CMD`（默认 `ruff check`）对变更的 `.py` 文件执行，底层由 Rust supervisor 调度 | 拒绝 + 类型/Lint 错误 |
| 4. UNIT_TEST_SUITE | `IMPLEMENT_BODY`/`FIX_INTEGRATION` 跑 `VN_TEST_CMD`（若命令是 pytest 形式，自动追加 `test_artifact_ids` 的文件）；`WRITE_TEST` 只跑 `--collect-only` | 拒绝 + 测试/收集输出 |

合入：

1. 只 stage 本任务变更的文件（测试缓存不会进入提交）
2. 在临时 worktree 提交；锁外用 Rust `file_snapshot` 预计算新提交每个变更文件的骨架/符号哈希，登记到进程级不可变缓存
3. 获取 per-workspace merge advisory lock
4. 再次读取 HEAD；若已推进：
   - 默认丢弃本次尝试，在新 HEAD 上重跑完整门禁（`VN_MAX_MERGE_ATTEMPTS`）
   - 设置 `VN_FAST_FORWARD_MERGE=true` 时，若 `c0..HEAD` 只改了同文件的其他符号（文件骨架哈希不变，且 read/test/target 每个符号的完整源码哈希不变），用 `git merge-tree --write-tree` + `git commit-tree` 直接合并已验证补丁（`merge_mode=tree_merge`），跳过重复 Stage 3/4；否则回退完整门禁。脏检查走内存快照，锁内不再 `git diff/show` 或重解析 AST
5. `git update-ref refs/heads/<main> <new> <old>` CAS 推进
6. 同一事务内同步制品签名/状态、`mark_done`、Kahn 递减下游 `in_degree`、唤醒 READY
7. `symbol_edges` 重建投递到后台队列（按提交顺序，最终一致；不在合并临界区）
8. 若 DB 事务失败，尽力把 Git ref 回滚到旧值
9. 提交后发布 `GATE_PASSED` / `TASK_READY` / `CONTRACT_STALE`（事件投递尽力而为，不影响已提交事实）；响应带 `merge_timing`（gate_pool_wait / lock_wait / dirty_check / git / DB / core sync）

### 5.4 SPLIT / ESCALATE / INJECT

- **SPLIT**：只允许 LEASED 任务；校验 `Union(child.write_set) ⊆ parent.write_set`、子图无环、**无依赖关系的子任务写集必须正交**；父节点下游边重连到所有汇点；父节点 `SPLIT_ARCHIVED`。
- **ESCALATE**：递归 CTE 沿 `symbol_edges` 标记 STALE（最多 6 跳，可按 `referenced_fields` 过滤）；读取/目标/测试命中受影响制品的任务被 BLOCKED 并清租约；所有曾经 LEASED 的受害者通过 SSE `TASK_ABORT` 被 Runner 抢占；报告任务 `SUSPENDED_ESCALATED`；插入 `REVISE_CONTRACT` READY（priority 999），并把它设为所有受影响任务的上游。
- **INJECT**：当前任务 LEASED→BLOCKED、`in_degree+1`、释放租约；插入 `DESIGN_CONTRACT` READY 前置节点并建立依赖边。

---

## 6. 测试与验证

### 单元测试

```bash
python -m pytest -q
```

覆盖：锁代数规范化/祖先/冲突/子集/新文件提升、diff 守卫、AST 骨架与哈希稳定性、stub 发现、split DAG 无环与并发正交、上下文裁剪、MCP 握手/工具面、`main.py` 的 15 行约束。
另有 `tests/test_rust_core.py`（扩展未构建时自动 skip）：Rust↔Python 锁/AST/planner 对拍、权威 `SchedulerCore` claim/revert/陈旧同步、命令超时、输出上限、进程树终止。

```bash
# 默认使用已安装的 Rust 扩展
python -m pytest -q

# 强制纯 Python 回退（同样应全绿）
VEYNOD_DISABLE_RUST=1 python -m pytest -q

# Rust 单元测试 + 1M 内存图规模测试
cd rust/veynod_core
cargo test --release
cargo test --release million_node_claim -- --ignored --nocapture
```

### 100 Runner 压测基线

见 [docs/STRESS_TEST.md](docs/STRESS_TEST.md)。当前 100 Runner / 100 task 基线：

- cross-file：54.2s，100/100 COMMITTED，`tree_merge` 99%，claim p50 30ms/p99 5.0s，submit p50 26.2s
- hot-file：58.0s，100/100 COMMITTED，`tree_merge` 99%，claim p50 42ms/p99 4.7s，submit p50 29.2s
- **merge 临界区 p50 218–281ms**（上一版 550–620ms）；`gate_pool_wait p50 20.2s` 才是 submit 的主瓶颈
- **claim 决策尾部**去掉 advisory lock 后 p95 3.17s → 358ms；剩余大头是 bundle 读 Git/AST
- `config.lock` 错误、QueuePool 错误、客户端超时均为 0

### 集成测试（真实 Postgres + Git + pytest）

```bash
docker run -d --rm --name veynod-test-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=veynod_test \
  -p 55432:5432 postgres:16-alpine

TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test \
  python -m pytest tests/test_integration.py -q
```

7 个场景：

1. 完整 happy path：建 workspace → 冻结契约/测试 → 创建任务 → 拉取 bundle → 真实 diff → 四阶段门禁 → 合入 → 下游解锁 → 制品 COMMITTED
2. 写集正交调度：`f`/`g`/`h` 可并发，整文件写锁被阻塞
3. 冻结签名篡改：Stage 2 拒绝，Git HEAD 不动，retry_count+1
4. stub 扫描：dry-run、生成 2×WRITE_TEST + 2×IMPLEMENT_BODY、二次扫描幂等
5. SPLIT：父节点归档、两个子节点 READY
6. INJECT：当前 BLOCKED/in_degree+1、前置 DESIGN_CONTRACT READY
7. ESCALATE：报告任务挂起、受害者阻塞、契约 STALE、REVISE_CONTRACT priority=999 READY

独立冒烟脚本（需要先启动测试 Postgres）：

```bash
# API 层：建 workspace -> 冻结契约 -> 租任务 -> 真实 diff -> 门禁 -> 合并
python scripts/smoke_e2e.py
```

两个 Runner 冒烟会分别启动 uvicorn（:8011 / :8012），并断言任务状态、主干内容与 worktree 清理。

---

## 7. 配置项

见 [`.env.example`](.env.example)。关键项：

| 环境变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `DATABASE_URL` | `postgresql+asyncpg://veynod:veynod@127.0.0.1:5432/veynod` | 自动把 `postgresql://` 转为 asyncpg |
| `VN_DB_POOL_SIZE` | 20 | 每个 API/worker 进程的 SQLAlchemy 连接池大小；多实例部署按总连接预算下调 |
| `VN_DB_MAX_OVERFLOW` | 40 | 每个进程的溢出连接数 |
| `VN_AUTH_MODE` | `dev` | `dev` 接受任意格式正确的 `vnk_`；生产必须 `prod` |
| `VN_ADMIN_API_KEY` | 空（禁用管理面） | `X-Admin-Key` |
| `VN_DEFAULT_WORKSPACE_ID` | 空 | 多 workspace 时固定 syscall 使用的工作区 |
| `VN_TYPECHECK_CMD` | `pyright` | Stage 3 类型检查 |
| `VN_LINT_CMD` | `ruff check` | Stage 3 Linter |
| `VN_SKIP_LINT` | `false` | 仅本地演示；生产保持 false |
| `VN_TEST_CMD` | `pytest -q --tb=short` | Stage 4 |
| `VN_COLLECT_CMD` | `pytest --collect-only -q` | WRITE_TEST 阶段 |
| `VN_SKIP_TYPECHECK` | `false` | 仅本地演示；生产保持 false |
| `VN_LEASE_SECONDS` | 600 | 认领租约 |
| `VN_MAX_MERGE_ATTEMPTS` | 4 | HEAD 推进时的门禁重跑次数 |
| `VN_FAST_FORWARD_MERGE` | `false` | opt-in：文件骨架不变且 read/test/target 符号源码不变时用 Git tree merge 免重跑；同名多符号允许，import/模块级改动回退完整门禁 |
| `VN_SINGLE_KERNEL_CLAIM` | `true` | 允许使用进程内 Rust core；多进程时由 `scheduler_authorities` 选出唯一权威，其他实例自动走 SQL/advisory。设为 false 强制所有实例走 SQL 路径 |
| `VN_ASYNC_EDGE_REBUILD` | `true` | `symbol_edges` 在后台按提交顺序重建，移出 merge 临界区；设为 false 回到同步重建 |
| `VN_GATE_WORKTREE_POOL_SIZE` | 8 | 门禁常驻 worktree 池大小；避免每 submit 争抢 `git worktree/ config.lock` |
| `VN_REAP_VERIFYING_AFTER_SECONDS` | 3600 | 卡死 VERIFYING 回收阈值 |
| `VN_SUBMISSION_WORKERS` | 8 | 独立 gate worker 进程的默认并发数（`python -m app.worker --workers`） |
| `VN_WORK_LEASE_SECONDS` | 300 | worker 工作租约 TTL，心跳续租；崩溃后过期可重领 |
| `VN_WORK_HEARTBEAT_SECONDS` | 30 | 工作租约续租间隔 |
| `VN_WORK_MAX_ATTEMPTS` | 5 | 临时失败的最大工作尝试次数 |
| `VN_GATE_SANDBOX` | `local` | `docker` 为生产隔离模式；`local` 仅开发/CI |
| `VN_GATE_SANDBOX_IMAGE` | 空 | Docker gate 镜像（必须含 pyright/ruff/pytest） |
| `VN_GATE_CPU_LIMIT` / `VN_GATE_MEMORY_MB` / `VN_GATE_PIDS_LIMIT` | 1.0 / 512 / 128 | 沙箱资源上限 |
| `VN_SUBMISSION_POLL_SECONDS` | 0.25 | worker 无任务时的轮询间隔 |
| `VN_LEGACY_SUBMIT_MODE` | `reject` | 旧 `/v1/syscalls/submit_patch`：`reject` 返回 410；`sync` 仅迁移窗口 |
| `VN_ADMISSION_GATE_QUEUE_LIMIT` | 256 | 每仓库流水线（LEASED/VERIFYING/待门禁/待合并，去重）与准入许可的上限；达到后暂停发放新任务 |
| `VN_ADMISSION_MERGE_QUEUE_LIMIT` | 128 | 合并队列（PENDING/RUNNING intent）达到该深度时暂停发放 |
| `VN_ADMISSION_MODE` | `reject` | `reject` 返回 429 + `Retry-After`；`defer` 返回 200 `leased=false` + `retry_after_seconds`（旧客户端过渡） |
| `VN_ADMISSION_RETRY_AFTER_SECONDS` | 5 | 建议的重试间隔 |
| `VN_ADMISSION_CLAIM_SLOTS` | 32 | 并发保留槽位；全部占用时立即 429，避免等待锁占满 DB 连接池 |
| `VN_SCHEDULER_AUTHORITY_LEASE_SECONDS` | 30 | 单仓库 Rust 调度权威租约；仅持有者使用进程内 core，其余实例走 SQL/advisory 路径，DB 条件 UPDATE 仍是最终围栏 |
| `VN_EDGE_REBUILD_LEASE_SECONDS` | 120 | 持久化 edge rebuild job 租约；崩溃后过期可被任意进程重领 |
| `VN_EDGE_REBUILD_MAX_ATTEMPTS` | 5 | edge rebuild 最大尝试次数（指数退避） |
| `VN_OUTBOX_POLL_SECONDS` | 0.1 | 事务 outbox dispatcher 轮询间隔 |
| `VN_ALLOW_NONBARE_REPO` | `false` | 允许非 bare 仓库（危险） |

---

## 8. 与提供的 `syscalls.py` 的必要差异

为保证端到端一致，本实现对交付的 `syscalls.py` 做了以下最小改动（接口原语不变）：

1. **枚举补齐**：`TaskType` 增加 `DESIGN_ARCHITECTURE`、`REVISE_CONTRACT`；`TaskStatus` 增加 `FAILED`、`SUSPENDED`；`ArtifactStatus` 增加 `COMMITTED`。否则内部生成的修订任务无法被租约、已实现的制品无法出现在 bundle 中。
2. **锁路径规范化**：`LockAlgebra.normalize` 把 `file.py::Class.method` 统一为 `file.py::Class::method`，并拒绝 `..` 路径。引擎与 API 使用同一规范，避免"提交的符号路径不在租约写集"的假阴性。
3. **Runner 鉴权**：`get_authenticated_worker_id` 改为查询 `agent_tokens`（`VN_AUTH_MODE=prod`）；dev 模式保留原哈希派生行为。原实现只检查 `Bearer vnk_` 前缀。
4. **Bundle 增补**：`base_commit_sha` 与 `last_error_traceback`。没有 base SHA，Runner 无法创建 worktree；没有历史报错，自愈重试没有输入。
5. **SPLIT 校验增强**：在 Kahn 无环检查之外，要求**无依赖关系的兄弟任务**写集正交；有依赖的兄弟允许重叠（调度器保证串行）。
6. **MCP 握手**：增加 `initialize`、`notifications/initialized`、`ping`，并放宽 `method` 校验；否则标准 MCP 客户端（Claude Code/Codex）无法完成握手。
7. **提交协议**：v2 立即返回 202，门禁在持久化 worker 中执行；幂等键绑定 task/租约/patch SHA；旧 `/v1/syscalls/submit_patch` 默认 410 并提供 `VN_LEGACY_SUBMIT_MODE=sync` 迁移开关。

---

## 9. 安全与已知限制（务必阅读）

1. **门禁会在服务端执行 `pytest`**，即执行 Agent 提交的任意代码。Git worktree 只隔离 Git 状态，不是安全沙箱。对不可信 Agent 请把 kernel + runner 放在容器/VM 中，限制网络、文件系统与凭证；或把门禁部署到专用执行机上。
2. **仓库必须是 bare mirror**，或 `main` 未被检出。普通仓库上执行 `update-ref` 会让其工作区与 HEAD 脱节。启动/创建工作区时会校验并拒绝危险配置。
3. **管理面必须网络隔离**。`/v1/admin/*` 只靠 `X-Admin-Key`；`/v1/dag`、`/dashboard`、`/healthz` 只读但未鉴权。
4. **单 workspace 假设**：6 个 syscall 协议不带 workspace_id，引擎默认使用最早创建的 workspace（或 `VN_DEFAULT_WORKSPACE_ID`）。多项目请部署多个 kernel 实例，或在协议层扩展 workspace_id。
5. **仅 Python AST 切片**：tree-sitter 目前只接 `tree-sitter-python`；其他语言可以接入同构 slicer，但需要增加 parser 与符号解析。
6. **文件级共享风险**：符号级写锁允许两个任务并发修改同一文件的导入区或模块级代码；门禁 Stage 1 只校验文件边界，Git CAS/重试兜底合并。需要修改 import/模块级代码的任务应申请文件级写锁。
7. **符号图是启发式**：基于标识符/属性引用构建，可能存在漏边或误边。ESCALATE 的直接读取/目标/测试命中由 `read_artifact_ids` / `target_artifact_ids` / `test_artifact_ids` 保证，符号图负责间接传播。
8. **`symbol_edges` 最终一致**：默认在提交后由后台队列按提交顺序重建，不阻塞合并；6 个 syscall 热路径不读这张表。若需要“提交返回即可看到最新边”，设 `VN_ASYNC_EDGE_REBUILD=false`（会重新进入 merge 临界区）。
9. **SSE 已改为进程内单轮询 Fan-out**：一个后台协程每 1s 查询 `kernel_events`，写入按 workspace 的有界环形缓冲，`asyncio.Condition` 唤醒所有 SSE 客户端；慢客户端落后超过缓冲时回源 DB 一次。不再每连接每秒查库。
10. **VERIFYING 期间上游契约变更**的极端竞态未做版本快照锁定；写集正交 + 合并锁 + CAS 保证不会产生 Git 冲突，但建议升级契约时避开正在验证的任务。
11. **Dashboard 渲染上限**：默认读取 5,000 个节点；超大图请按 workspace/状态分片查看。

---

## 10. 三阶段冷启动工作流（对应 EDD §6）

1. **注入种子架构**：人类创建 `DESIGN_ARCHITECTURE` 任务。L0 Agent 把顶层领域拆成 10–20 个子系统，产出 50 个 `DESIGN_CONTRACT` 节点（可用 `request_task_split` 或管理面批量创建）。
2. **全量契约与空桩冻结**：50 个 L1 Agent 并发编写空桩并通过类型检查；门禁把目标制品置为 `FROZEN`。然后调用 `scan-stubs`，每个 `...` 函数自动生成 `WRITE_TEST` + `IMPLEMENT_BODY` 对。
3. **千级工蜂正交填充**：L2 Worker 并发 `pull_task_bundle`。写集正交调度器保证同一时刻 LEASED 任务的 AST 符号互不相交；测试通过后秒级推进 `main`。复杂函数触发 SPLIT，契约缺陷触发 ESCALATE，人类只在画布上审阅极少数上诉到顶层的契约变更。

---

## 11. 常用管理 API

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| POST | `/v1/admin/workspaces` | 校验并创建工作区（记录 Git HEAD） |
| GET | `/v1/admin/workspaces` | 列出工作区 |
| POST | `/v1/admin/tokens` | 签发 Runner token（明文只返回一次） |
| POST/GET | `/v1/admin/artifacts` | 注册/冻结制品、按前缀查询 |
| POST/GET | `/v1/admin/tasks` | 创建/列出任务 |
| GET | `/v1/admin/tasks/{id}` | 任务详情（含门禁报错） |
| POST | `/v1/admin/workspaces/{id}/scan-stubs` | 扫描 `...` stub 生成任务对 |
| POST | `/v1/admin/workspaces/{id}/edges/rebuild` | 重建符号调用图 |
| GET | `/v1/events/stream` | Runner SSE 游标流（含 `TASK_ABORT`） |
| POST | `/v1/runner/heartbeat` | 续租 |
| POST | `/v1/runner/release` | 主动释放/失败上报 |
| GET | `/v1/runner/tasks/{id}` | Runner 查询任务状态（检测 MCP 侧已完成） |
| GET | `/v1/dag` | 只读 DAG 拓扑与统计 |
| GET | `/v1/dag/plan` | Rust/Python 快照 claim planner（只读冲突诊断） |
| GET | `/healthz` | 数据库探活 + Rust core 可用性/版本 |
| GET | `/dashboard` | 单文件监控画布 |

---

## 12. 代码规模与后续扩展点

核心 Python 约 8,400 行（含交付的 `syscalls.py`、管理面与中文注释），Rust 约 2,350 行；不含测试/CLI/画布。EDD 的 5,000 行是精简目标，本实现优先保证可运行性与安全性，未为凑行数删减校验。可扩展方向：

- 接入 TypeScript/Go 的 tree-sitter slicer 与对应类型检查命令（Rust/Python 已支持）
- 把 `scheduler_core.rs` 扩展为长驻 Rust sidecar（共享内存图 + 复制日志），用于多 kernel/federated 部署；单 kernel 已由进程内核心权威调度
- 把 gate 的每任务 `pytest` 改为常驻测试 worker / 按 workspace 批量测试，降低 `gate_pool_wait`
- 用 `git cat-file --batch` 常驻进程替换 bundle 中的逐文件 `git show`，进一步压低 Claim P99
- 在 `submit_patch` 中增加 read-set 签名快照，关闭 VERIFYING 期间上游变更的竞态
- 将门禁执行迁移到一次性容器/Firecracker，形成真正的安全沙箱
- 为 Dashboard 增加按写集/符号的过滤与时间线回放
