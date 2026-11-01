# Veynod v4.0 — 5 Agent 协作实测体验报告

> 日期：2026-10-31  
> 环境：`http://127.0.0.1:8001`，独立数据库 `veynod_dev`，Rust core 0.1.0  
> 场景：5 个 ZCode 子 agent 同时修改同一个文件 `src/calc.py` 的 5 个不同函数  
> 结论：**内核协作能力通过；dashboard 目前是“只读监控”，还不是完整操作台。**

---

## 1. 实验设计

### 仓库

```text
src/calc.py          # add / sub / mul / div / mod 五个 `...` stub
tests/test_add.py    # 五个独立测试文件
tests/test_sub.py
tests/test_mul.py
tests/test_div.py
tests/test_mod.py
```

五个任务写集分别是：

```text
src/calc.py::add
src/calc.py::sub
src/calc.py::mul
src/calc.py::div
src/calc.py::mod
```

关键点：**同一文件、不同符号**。这是 Veynod 符号级并发的最小验证。

### Agent

5 个 ZCode `general-purpose` 子 agent，各自：

1. 读取自己的 worktree；
2. 只改自己的函数体；
3. 跑自己的冻结测试；
4. 通过 `submit_demo.py` 调 `submit_patch`。

任务在 agent 启动前已由内核全部 lease（5 个互不冲突的写集同时活跃）；agent 不负责 claim，只负责实现和提交。

### 门禁配置

```text
VN_FAST_FORWARD_MERGE=true
VN_SKIP_TYPECHECK=true
VN_SKIP_LINT=true
VN_GATE_WORKTREE_POOL_SIZE=8
```

说明：本次为演示速度跳过了 Stage 3（pyright/ruff）；Stage 1 写集边界、Stage 2 契约哈希、Stage 4 冻结测试都真实执行。

---

## 2. 内核侧最终结果

```json
{
  "workspace": "Veynod 5-Agent Demo",
  "head_commit_sha": "743f131011f0522877c39f79e12d2c20cd48cf20",
  "stats": {
    "tasks": 5,
    "artifacts": 10,
    "by_status": {"COMMITTED": 5}
  }
}
```

5 个任务全部 `COMMITTED`，`retry_count=0`。

| Agent | 函数 | 写集 | 合入方式 | 结果 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `add` | `src/calc.py::add` | `tree_merge` | COMMITTED |
| 2 | `sub` | `src/calc.py::sub` | `gate_rerun` | COMMITTED |
| 3 | `mul` | `src/calc.py::mul` | `tree_merge` | COMMITTED |
| 4 | `div` | `src/calc.py::div` | `tree_merge` | COMMITTED |
| 5 | `mod` | `src/calc.py::mod` | `tree_merge` | COMMITTED |

最终 Git 历史：

```text
743f131 veynod(task): Implement mod (5-agent demo)
ceea789 veynod(task): Implement mul (5-agent demo)
21d6755 veynod(task): Implement add (5-agent demo)
229d731 veynod(task): Implement div (5-agent demo)
2ee3b37 veynod(task): Implement sub (5-agent demo)
4a929b2 seed 5-agent demo
```

最终源码五个函数全部实现，且我在最终 HEAD 上重新跑了一遍全部测试：

```text
.....  [100%]
```

**5 个测试全部通过。**

---

## 3. 体验好的地方

### 3.1 同一文件并发真的成立

这是最直观的“wow”：

- 5 个 agent 都从同一个 seed commit 建 worktree；
- 都修改 `src/calc.py`；
- 各自只改自己的函数；
- 内核没有让它们互相覆盖，也没有产生 Git 冲突；
- 最终 `main` 上五个函数同时存在，测试全绿。

符号级写集锁 + 契约哈希 + tree merge 的组合，在这个场景里是成立的。

### 3.2 合并路径有快有稳

5 次合入里：

- 4 次 `tree_merge`：HEAD 已推进，但只改了别的符号，直接快进；
- 1 次 `gate_rerun`：走了完整门禁路径；
- 两种路径都成功，没有 retry。

这正好展示了 opt-in 快路径的价值：**不必为同文件的其他函数重跑整条门禁**，但结构变化时仍有完整门禁兜底。

### 3.3 Dashboard 的状态表达清楚

画布上：

- 5 个节点从 READY → LEASED → VERIFYING → COMMITTED；
- 颜色区分状态，LEASED/VERIFYING 会脉冲；
- 点击节点能看到 task id、runner、retries、in-degree、write locks；
- Event tail 能看到 `TASK_LEASED` 和 `GATE_PASSED`；
- 页面每 4 秒自动刷新，不需要手动点 refresh。

对“看任务流向”这个目标，够用且直观。

### 3.4 失败会留痕

虽然这次 5 个任务都一次过，但门禁失败时：

- 任务会回到 LEASED/FAILED；
- `last_error_traceback` 写回任务；
- 下一轮 bundle 会带诊断；
- Agent 可以自愈，不需要人类转述。

这是比“跑完就丢日志”更好的工作流。

---

## 4. 体验不好的地方

### 4.1 Dashboard 是只读监控，不是操作台

当前页面只能看，不能：

- 创建 workspace / 任务；
- 触发 `scan-stubs`；
- 启动/停止 Runner；
- 查看完整门禁诊断；
- 重试/取消任务；
- 切换 workspace。

所有操作都要回到 curl / `cli/bin/veynod.mjs`。对第一次用的人，画布看起来像“产品”，实际是“仪表盘”。

### 4.2 多 workspace 没有选择器

Dashboard 的 JS 调 `/v1/dag` 不带 `workspace_id`，所以只显示默认 workspace（或最早创建的）。  
这次为了演示，我重启了服务并设置 `VN_DEFAULT_WORKSPACE_ID`，否则你看到的还是旧 demo。

用户视角：无法在页面上切换项目，容易误以为数据丢了。

### 4.3 Inspector 信息不够排障

Inspector 有：

- title、status、type、task id、runner、retries、in-degree、updated_at、write_locks。

缺少：

- 最近一次门禁的 stage 和 exit code；
- `last_error_traceback`；
- `merge_mode` / `merge_timing`；
- 目标/测试 artifact；
- 依赖边和上游/下游。

排障时仍然要去 `GET /v1/admin/tasks/{id}` 或看 Event tail 的截断 JSON。

### 4.4 上手成本偏高

要看到第一次“动起来”，需要：

1. bare Git 仓库；
2. workspace；
3. token；
4. FROZEN 契约；
5. FROZEN 测试；
6. 任务；
7. 5 个 worktree / Runner；
8. 理解 READY/LEASED/VERIFYING/COMMITTED。

对架构师演示没问题；对第一次接触的用户，门槛不低。

### 4.5 8000 / 8001 容易混淆

你最早打开的是 Docker 里的 `agentgraph-api-1`（Veynod Console 3.24.0），它有自己的 `/dashboard/login/`。  
v4.0 的 dashboard 在 8001 的 `/dashboard`，不需要登录。两个产品页面在同一台机器上，端口不同，非常容易误判。

### 4.6 演示环境残留

为了让你看到数据，数据库里现在有多个 demo workspace：

- `Veynod v4.0 Demo`（旧，1 个 READY 任务）
- `Veynod v4.0 Live Demo`（旧，1 个 READY 任务）
- `Veynod 5-Agent Demo`（当前，5 个 COMMITTED）

Dashboard 只显示默认 workspace，旧数据还在库里；长期使用需要清理/重置机制。

---

## 5. 性能与成本

### 5.1 Agent 侧

5 个子 agent 并行执行，单个体感约 **15–16 秒**（工具执行时长），5 个同时完成。

每个子 agent 的 token 使用约 **246k**，对于 5 个只改一行的函数来说，成本明显偏高——这是 **Agent 编排/上下文成本**，不是 Veynod 内核成本。

### 5.2 内核侧

子 agent 回报的 merge total：

- `add` tree_merge：约 250 ms
- `mul` tree_merge：约 281 ms
- `div` tree_merge：约 437 ms
- `mod` tree_merge：约 281 ms
- `sub` gate_rerun：约 125 ms（另一轮计时里 gate pool wait 141 ms）

口径是单次 submit 的 merge 计时，不是 100 Runner 压测；量级参考即可。

### 5.3 结论

这个 demo 证明的是**协调与合入链路**，不是模型写代码的质量。  
5 个函数都是确定性实现；真实 Claude/Codex 的思考时间和代码质量会显著改变体验。

---

## 6. 如果要把体验做好，优先级建议

1. **Dashboard 加 workspace 选择器**  
   顶部下拉切换 workspace，或者 URL 带 `?workspace_id=`。

2. **Inspector 增加排障信息**  
   最近门禁 stage/exit、`last_error_traceback`、`merge_mode`、`merge_timing`、目标/测试 artifact。

3. **加“操作入口”**  
   至少三个按钮：`scan-stubs`、`New task`、`Start runner`；否则画布永远只是看。

4. **任务详情抽屉/页面**  
   展示完整 gate results、事件时间线、commit SHA、重试历史。

5. **Runner 一键初始化**  
   `veynod init` 生成 workspace/token/runner 配置；现在需要 4–5 条 curl。

6. **演示数据重置**  
   `--reset-demo` 或一个清理按钮，避免多个 demo workspace 混在一起。

7. **端口/服务标识**  
   v4.0 页面明确显示 `Kernel v4.0 · :8001`，避免和 8000 的 Veynod Console 3.24.0 混淆。

---

## 7. 体验评分（主观）

| 维度 | 评分 | 说明 |
| :--- | :--- | :--- |
| 并发协调 | 5 / 5 | 同文件 5 符号并发，无冲突，全部合入 |
| 合入正确性 | 5 / 5 | 最终 HEAD 5 个测试全绿 |
| 可观测性 | 3 / 5 | 画布状态清楚，但排障信息不足 |
| 上手成本 | 2 / 5 | 需要 bare repo、token、artifacts、runner 多层配置 |
| 产品完整度 | 2.5 / 5 | 只读监控，缺操作与 workspace 切换 |
| 演示说服力 | 5 / 5 | 5 个真实子 agent + 真实 Git 历史 + 真实 pytest |

---

## 8. 一句话结论

**内核已经能支撑“5 个 agent 同时改同一个文件”的协作，而且结果可验证；但 dashboard 还只是监控面板，离“产品化的操作台”还有一段路。**  
如果你要向架构师/团队演示，这次数据足够有说服力；如果要给普通用户使用，下一步应该优先补 workspace 切换、任务详情和操作入口。
