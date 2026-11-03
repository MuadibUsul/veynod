# Phase 1 数据迁移与回滚

新增 `veynod_kernel/sql/v4_017_work_scope.sql`。Kernel 启动时按文件名顺序在 schema advisory lock 下执行版本化 SQL（`veynod_kernel/app/config.py:450-465`；`app/database.py:104-135`）。本次真实集成测试在新建临时 PostgreSQL 数据库上执行了该迁移并完成 Task/Lease/提交链。

新增列均可空：`dag_tasks.work_scope JSONB`、`scope_hash CHAR(64)`、`lease_scope JSONB`、`lease_scope_hash CHAR(64)`。两条 CHECK 保证 scope/hash 成对出现；触发器拒绝对非空 WorkScope 的原地修改。旧行保留 `NULL`，不自动推断已发 Lease 的权限。新 scoped Task 创建时同步写入范围/hash，领取时写 Lease 快照/hash。

兼容：未带 WorkScope 的旧 Runtime/Admin 创建请求继续使用旧写集；旧 Product Task `write_set_symbols` 保持原行为；带 Diagram `allowed_write_paths` 的请求经适配器生成新范围。若已有 `runtime_request_key` 对应无范围任务，重试携范围返回 409；旧无范围 Lease 不可用于 scoped Product Task 提交，必须创建新 Task/Attempt。没有批量回填或静默扩权。

回滚：先停止发放新 scoped Lease，等待或取消活动 scoped Task；部署旧应用后可保留新增可空列（推荐，数据不丢失）。若必须删除列，须先导出 scoped 任务的范围/hash/提交证据，再显式删除触发器、CHECK 和四列；该破坏性 SQL 未自动执行。本阶段没有修改生产数据库。
