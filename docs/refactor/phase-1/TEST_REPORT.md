# Phase 1 测试报告

环境：Windows 11 / Python 3.10 / Docker Engine 29.9.2；代码基准 `88626e3`。以下结果按层级区分，不把单元测试称为跨服务 E2E。

| 层级 | 命令或场景 | 结果 |
|---|---|---|
| 契约与 Runtime 单元 | `python -m pytest -q contracts/tests/test_contracts.py contracts/tests/test_work_scope.py agent_runtime/tests/test_work_scope_handoff.py` | **19 passed**；含路径穿越、排除优先、hash、防止模型扩大写集 |
| Kernel 提交边界与原有协议/Worktree 单元 | `python -m pytest -q veynod_kernel/tests/test_work_scope_boundary.py veynod_kernel/tests/test_submission_protocol_unit.py veynod_kernel/tests/test_lease_worktree.py` | **11 passed**；含范围内提交、排除路径拒绝、hash 不符、真实 AST 变更未授权拒绝、祖先锁下新文件通过 |
| AgentGraph 兼容适配单元 | `python -m pytest -q AgentGraph/tests/test_kernel_delivery_scope.py` | **4 passed**；含 Diagram 优先、旧任务退路、旧无范围 Lease 拒绝、实际创建请求内容 |
| Kernel 真实集成 | `python -m pytest -q veynod_kernel/tests/test_work_scope_e2e.py`，使用专用临时 `veynod_phase1_test_*` PostgreSQL 数据库并在结束后删除 | **1 passed**。实际创建 Task、发 Lease、返回范围/hash；同键扩权 409；排除路径 Diff 403；允许的 Patch 进入独立 Gate Worker 并最终 COMMITTED。使用真实 Git Worktree、数据库和 Gate；测试中的源代码改动只发生在临时仓库。 |
| 编译/静态 | `python -m compileall -q`（变更的 Python 模块）；`git diff --check` | 通过。 |
| 更广非集成回归 | `python -m pytest -q contracts/tests agent_runtime/tests veynod_kernel/tests -m "not integration" --disable-warnings --maxfail=1` | **134 passed, 84 deselected**。 |
| 含集成测试的更广回归 | `python -m pytest -q contracts/tests agent_runtime/tests veynod_kernel/tests --disable-warnings --maxfail=1`；隔离临时数据库 | **132 passed, 1 failed**，首个失败后停止，未跑完其余测试。失败为现存 `test_async_submission_fails_durably_when_lease_expires_before_processing`：期望过期 Lease 对应 Task 为 READY，实际为 LEASED。同一用例在新临时数据库单独重跑 **1 passed**；符合时序性失败特征，尚未定位根因。 |
| AgentGraph 全套非集成测试收集 | `python -m pytest -q AgentGraph/tests -m "not integration" --disable-warnings --maxfail=1` | **未能收集**；本机缺少 `pgvector`，导入 `AgentGraph/app/models.py` 报 `ModuleNotFoundError`。独立兼容适配单元已通过，不代表全套。 |

首次真实集成测试因 Windows 子进程的 `PYTHONPATH` 未包含仓库根目录，Gate Worker 无法导入新共享包而失败。随后在隔离测试命令中设置仓库根 `PYTHONPATH`，重新运行通过；生产 Kernel Dockerfile 已显式复制 `contracts`。第一次失败不是提交校验失败，已记录而未计为通过。

未运行生产部署验收、完整 AgentGraph→Runtime/Host→Kernel→Diagram E2E、1000 Runner 容量测试。更广的含集成测试回归没有全绿，AgentGraph 全套未完成收集；本阶段不声称它们通过。全部 `veynod_phase1_test_*` 临时数据库在运行后已删除。
