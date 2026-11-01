# PHASE2F_REPORT — Context Engine

> 日期：2026-10-31
> 范围：Phase 2F 第一批：ContextBuilder + LoopController 接入
> 状态：库 + 真实 PostgreSQL 测试通过；未接产品文档/代码 Snapshot

---

## 1. 本次完成

### 1.1 ContextBuilder

`agent_runtime/context.py`：

- 分层 section：
  - system policy（最高优先级）
  - product task / budget / usage
  - 最近 checkpoint
  - 最近 steps（最多 20）
  - extra refs（文档/代码/人工决策等）
- 按 priority 排序；
- 估算 token（JSON 字符数 / 4）；
- 预算内保留；超预算时：
  - 高优先级 section 截断（保留 preview + truncated 标记）；
  - 低优先级 section 进入 `omitted`；
- 返回 `sections/token_estimate/token_budget/omitted`，保留 ref。

### 1.2 Store 最近步骤

`RunStore.list_steps(run_id, limit)`：按 step_no 倒序取最近步骤，返回升序。

### 1.3 LoopController 接入

- 可选 `context_builder`；
- 提供时组装 `run + checkpoint + recent steps`，传给 ModelGateway；
- 未提供时保持原 checkpoint_state 行为（兼容测试/旧调用）。

---

## 2. 测试结果

| 套件 | 结果 |
|---|---|
| agent_runtime | **21 passed**（真实 PostgreSQL + MockTransport） |

新增覆盖：

- 预算 128 tokens 下，system policy/product task 保留，checkpoint 截断/低优先级文档 omitted，总估算不超预算；
- LoopController 把 ContextBuilder 结果传给模型，包含 product_task section；
- 现有 store/loop/API/worker/gateway 测试继续通过。

---

## 3. 限制

1. Context 来源目前是 run/checkpoint/steps + 调用者传入的 extra；没有自动读取 AgentGraph 文档、代码 Snapshot、符号上下文。
2. token 估算是字符近似，不是真实 tokenizer。
3. 截断是字符串 preview，结构化内容可能被截断。
4. 没有 Context 版本/引用解析服务。
5. 没有子 Agent 委派、Provider 配额、Worker supervisor。
6. 没有接 AgentGraph。
7. 未提交 git。

---

## 4. 下一步（Phase 2G）

1. 子 Agent 委派（child run + 等待/回收）；
2. Provider 路由/配额/成本；
3. Worker supervisor/metrics；
4. AgentGraph 文档/快照接入 Context；
5. AgentGraph 创建 Agent Run + 投影；
6. 真实模型端到端测试。

---

## 5. 结论

Runtime 现在有按优先级和 token 预算组装的 Context，并已接入模型决策调用。**但 Context 还没有接产品文档/代码快照，也没有委派和配额。**
