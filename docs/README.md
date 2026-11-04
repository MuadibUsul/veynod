# Veynod 文档地图

一个仓库、一套文档。先读上面的，再按需往下。

## 1. 现在是什么样（先读）

| 文档 | 内容 |
|---|---|
| [../README.md](../README.md) | 产品定位、三类进程、一次任务的完整链路、启动、测试 |
| [UNIFIED_ARCHITECTURE.md](UNIFIED_ARCHITECTURE.md) | 统一架构：产品面 → Agent Runtime → Kernel 的职责与边界 |
| [refactor/HANDOFF.md](refactor/HANDOFF.md) | 最近一轮改了什么、还剩什么（交接从这里开始） |
| [adr/ADR_INDEX.md](adr/ADR_INDEX.md) | 架构决策记录，决定为什么是现在这样 |

## 2. 契约（跨服务的约定）

| 文档 / 代码 | 内容 |
|---|---|
| [protocols/API_CONTRACTS.md](protocols/API_CONTRACTS.md)、[protocols/EVENT_CONTRACTS.md](protocols/EVENT_CONTRACTS.md) | HTTP 与事件契约 |
| [`contracts/`](../contracts/README.md) | 共享代码：ID、事件包、WorkScope、nerve 协议、**唯一的 Kernel HTTP 传输**（`kernel_http.py`） |
| [refactor/10_EXECUTION_CONTRACTS.md](refactor/10_EXECUTION_CONTRACTS.md) | 执行契约（WorkScope、所有权、投影） |

## 3. 运维

| 文档 | 内容 |
|---|---|
| [UNIFIED_OPERATIONS.md](UNIFIED_OPERATIONS.md) | 部署形态与参数 |
| [PRODUCTION_OPERATIONS.md](PRODUCTION_OPERATIONS.md) | 已部署实例、诊断一个任务、恢复演练、部署禁忌 |
| [RELEASE_0.6.0.md](RELEASE_0.6.0.md) | **下一次上线的清单**：服务端 + CLI 0.6.0 同步切换（nerve 唯一派发、只走 Kernel） |
| [DEPLOYS.md](DEPLOYS.md) | 部署门 v2：任何项目打开自动部署（每次合并 / 时间窗口 / 手动），增量构建与部署，Veynod VPS 作为第一个目标，v1 退役计划 |
| [CHAOS_TEST_MATRIX.md](CHAOS_TEST_MATRIX.md)、[CAPACITY_BASELINE.md](CAPACITY_BASELINE.md) | 故障与容量实测 |

## 4. 各组件自己的文档

| 组件 | 文档 |
|---|---|
| 产品面 `AgentGraph/` | [AgentGraph/docs/README.md](../AgentGraph/docs/README.md)（Agent 指南、错误码、唤醒与派发） |
| CLI / 公开桥 `AgentGraph/cli/` | [README](../AgentGraph/cli/README.md)、[USAGE](../AgentGraph/cli/USAGE.md) |
| Kernel `veynod_kernel/` | [veynod_kernel/docs/ARCHITECTURE.md](../veynod_kernel/docs/ARCHITECTURE.md) |
| Runtime `agent_runtime/` | [agent_runtime/README.md](../agent_runtime/README.md) |

## 5. 架构细节与审计（参考）

[architecture/](architecture/)：项目架构全文（`PROJECT_ARCHITECTURE*.md`）、数据归属、状态机清单、
集成图、风险登记、目标架构。[refactor/](refactor/)：Phase 0 核验、重构规范 `SPEC_v1.0.md`、
Phase 1/2 计划与结果。审计与资格报告：`UNIFIED_ARCHITECTURE_AUDIT.md`、
`PRODUCTION_QUALIFICATION_*.md`、`SECURITY_QUALIFICATION_REPORT.md`。

## 6. 历史

[archive/phases/](archive/phases/)：Phase 0–8 的逐阶段报告。只记录当时做了什么，不代表现状；
现状以第 1 节为准。

## 维护约定

- 一个仓库：CI 只有根目录 `.github/workflows/ci.yml`，一条命令 `scripts/test_all.sh` 跑全部套件；
  三个镜像都从根目录用 `deploy/Dockerfile.*` 构建；依赖三处精确且一致。这些由
  `contracts/tests/test_repository.py` 检查。
- 移动文档时保持相对链接有效（仓库内当前没有失效的相对链接）。
