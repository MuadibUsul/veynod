"""Server-rendered comparison pages (/vs/* and /zh/vs/*) for SEO and GEO."""

# ruff: noqa: E501

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from functools import lru_cache

from app.root_page import SITE, _footer

UPDATED_DATE = "2026-11-28"


@dataclass(frozen=True)
class Localized:
    en: str
    zh: str

    def pick(self, lang: str) -> str:
        return self.zh if lang == "zh" else self.en


@dataclass(frozen=True)
class ComparisonRow:
    dimension: Localized
    other: Localized
    veynod: Localized


@dataclass(frozen=True)
class FaqItem:
    question: Localized
    answer: Localized


@dataclass(frozen=True)
class ComparisonPage:
    slug: str
    name: str
    category: Localized
    title: Localized
    description: Localized
    h1: Localized
    summary: Localized
    use_other: Localized
    use_veynod: Localized
    use_together: Localized
    snippet_label: str
    snippet_code: str
    rows: tuple[ComparisonRow, ...]
    faqs: tuple[FaqItem, ...]


COMPARISONS: tuple[ComparisonPage, ...] = (
    ComparisonPage(
        slug="t3-code",
        name="T3 Code",
        category=Localized("CLI Agent GUI", "CLI Agent 图形界面"),
        title=Localized(
            "Veynod vs T3 Code — Shared Blackboard for Coding Agents | Veynod",
            "Veynod 对比 T3 Code — 多 Agent 共享黑板与协作控制平面 | Veynod",
        ),
        description=Localized(
            "Compare Veynod and T3 Code. T3 Code drives CLI coding agents in a desktop GUI; Veynod gives multiple agents shared memory, task leases, and human approval over MCP.",
            "对比 Veynod 与 T3 Code：T3 Code 是单人驱动 CLI 编程 Agent 的桌面端；Veynod 通过 MCP 为多 Agent 团队提供共享记忆、任务租约与人工审批。",
        ),
        h1=Localized("Veynod vs T3 Code", "Veynod 对比 T3 Code"),
        summary=Localized(
            "T3 Code is a desktop GUI for one developer to launch and monitor CLI coding agents. Veynod is a runtime-agnostic MCP control plane where multiple agents—including agents running inside T3 Code—share one plan, versioned memory, task leases, and human approval gates.",
            "T3 Code 是一款帮单个开发者在桌面端启动和监控 CLI 编程 Agent 的图形界面；Veynod 则是跨运行时的 MCP 控制平面，让多个 Agent（包括在 T3 Code 里运行的 Agent）共用同一份计划、版本化记忆、任务租约与人工审批门禁。",
        ),
        use_other=Localized(
            "You want a clean local desktop UI to run Claude Code or Codex tabs side by side for one developer.",
            "你是单个开发者，需要一个清爽的本地桌面界面来并排运行和查看多个 CLI 编程 Agent。",
        ),
        use_veynod=Localized(
            "Multiple agents across machines or editors need shared state, dependency-ordered tasks, and an approval gate before merging.",
            "跨设备或跨编辑器的多个 Agent 需要共享上下文、按依赖领取任务，并在合并前经过质量门禁与人工审批。",
        ),
        use_together=Localized(
            "Run your CLI agents inside T3 Code and connect each session to Veynod via `npx veynod` so they coordinate on one board.",
            "继续在 T3 Code 中运行你的编程 Agent，并通过 `npx veynod` 将它们接入同一个 Veynod 黑板协同工作。",
        ),
        snippet_label="bash · connect T3 Code sessions to Veynod",
        snippet_code=(
            "$ npx veynod\n"
            "# Registers Veynod MCP in Claude Code, Cursor, and Codex configs\n"
            "# Agents inside T3 Code call get_orientation -> workspace_sync"
        ),
        rows=(
            ComparisonRow(
                Localized("Primary layer", "核心定位"),
                Localized("Desktop GUI for CLI coding agents", "CLI 编程 Agent 桌面端"),
                Localized("Shared blackboard & MCP control plane", "共享黑板与 MCP 控制平面"),
            ),
            ComparisonRow(
                Localized("Shared memory", "共享记忆"),
                Localized("Per-session local terminal history", "按会话隔离的本地终端记录"),
                Localized("Versioned workspace documents with provenance", "带版本溯源的工作区共享文档"),
            ),
            ComparisonRow(
                Localized("Task coordination", "任务协调"),
                Localized("Manual prompting per tab", "按标签页手动派活"),
                Localized("Dependency DAG, time-bounded leases, auto-requeue", "依赖 DAG、租约防冲突与超时回收"),
            ),
            ComparisonRow(
                Localized("Delta context sync", "增量上下文同步"),
                Localized("Full transcript per session", "各会话独立上下文"),
                Localized("`workspace_sync` returns <30% payload by default", "`workspace_sync` 默认返回不到 30% 的增量摘要"),
            ),
            ComparisonRow(
                Localized("Human approval", "人工审批"),
                Localized("Local terminal confirmation", "本地终端逐次确认"),
                Localized("Proposal queue with audit trail", "集中式提案队列与完整审计记录"),
            ),
        ),
        faqs=(
            FaqItem(
                Localized("Is Veynod a replacement for T3 Code?", "Veynod 是 T3 Code 的替代品吗？"),
                Localized(
                    "No. T3 Code is an interface for running CLI agents; Veynod is the coordination backend they talk to over MCP. You can run agents in T3 Code while sharing tasks and memory in Veynod.",
                    "不是。T3 Code 是运行 CLI Agent 的前端界面，Veynod 是 Agent 通过 MCP 沟通的协作后端，两者可以叠加使用。",
                ),
            ),
            FaqItem(
                Localized("How do T3 Code agents avoid editing the same file?", "在 T3 Code 里开多个 Agent 如何避免互相覆盖？"),
                Localized(
                    "Connect them to Veynod and assign scoped tasks with time-bounded leases. Each agent claims one task via `claim_task` and checks `workspace_sync` before writing.",
                    "把它们接入 Veynod，通过 `claim_task` 领取带租约的独立任务，并在写入前用 `workspace_sync` 同步状态。",
                ),
            ),
            FaqItem(
                Localized("Does Veynod work with Claude Code and Codex?", "Veynod 支持 Claude Code 和 Codex 吗？"),
                Localized(
                    "Yes. Any MCP or REST client—including Claude Code, Codex, Cursor, and custom scripts—can join the same Veynod workspace.",
                    "支持。任何兼容 MCP 或 REST 的客户端（Claude Code、Codex、Cursor 或自研脚本）都能加入同一个工作区。",
                ),
            ),
        ),
    ),
    ComparisonPage(
        slug="orca",
        name="Orca",
        category=Localized("Git Worktree Runner", "Git Worktree 并行调度"),
        title=Localized(
            "Veynod vs Orca — Task Leases & Merge Gates for Parallel Agents | Veynod",
            "Veynod 对比 Orca — 并行编程 Agent 的任务租约与合并门禁 | Veynod",
        ),
        description=Localized(
            "Compare Veynod and Orca. Orca spawns coding agents in parallel git worktrees; Veynod coordinates task dependencies, shared memory, quality gates, and human approval.",
            "对比 Veynod 与 Orca：Orca 用 git worktree 并行跑多个 Agent；Veynod 负责跨 Agent 的任务依赖、共享记忆、质量门禁与人工审批。",
        ),
        h1=Localized("Veynod vs Orca", "Veynod 对比 Orca"),
        summary=Localized(
            "Orca isolates parallel coding agents by giving each one its own git worktree. Veynod operates one layer above: it gives those isolated agents a shared blackboard to claim non-overlapping tasks, pass handoff artifacts, run independent quality gates, and request human sign-off before merging.",
            "Orca 通过 git worktree 为每个编程 Agent 隔离工作目录；Veynod 位于其上一层，为这些隔离的 Agent 提供共享黑板，用于领取不冲突的任务、传递交接产物、触发独立质量门禁以及在合并前发起人工审批。",
        ),
        use_other=Localized(
            "You need fast local git worktree isolation so multiple coding agents don't collide on the same working tree.",
            "你需要在本地快速创建多个 git worktree，避免并行 Agent 在同一目录里改乱文件。",
        ),
        use_veynod=Localized(
            "Parallel agents need a shared task graph, versioned architecture notes, automated CI review gates, and cross-machine coordination.",
            "并行 Agent 需要共享任务图谱、版本化架构决策、自动化质量门禁以及跨机器协作。",
        ),
        use_together=Localized(
            "Let Orca manage the local git worktrees while each agent reads and updates its task state on Veynod.",
            "用 Orca 管理本地 git worktree 隔离，同时让每个 worktree 里的 Agent 在 Veynod 上领单、交接与过门禁。",
        ),
        snippet_label="json · MCP task handoff inside a worktree",
        snippet_code=(
            "{\n"
            '  "tool": "claim_task",\n'
            '  "arguments": { "workspace_id": "ws_core", "lease_seconds": 600 }\n'
            "}\n"
            "// Worktree agent completes task -> triggers quality gate -> human proposal"
        ),
        rows=(
            ComparisonRow(
                Localized("Primary focus", "核心聚焦"),
                Localized("Git worktree isolation per agent", "按 Agent 隔离 git worktree"),
                Localized("Shared blackboard, task leases & merge gates", "共享黑板、任务租约与质量门禁"),
            ),
            ComparisonRow(
                Localized("Cross-agent handoff", "跨 Agent 交接"),
                Localized("Separate branches; manual review", "各自独立分支，需人工汇总"),
                Localized("Shared documents, dependency DAG & artifacts", "共享文档、依赖 DAG 与结构化产物"),
            ),
            ComparisonRow(
                Localized("Failure recovery", "异常恢复"),
                Localized("Worktree stays idle on crash", "进程退出后工作停滞"),
                Localized("Time-bounded leases return stalled tasks to READY", "租约过期后任务自动回到 READY 队列"),
            ),
            ComparisonRow(
                Localized("Scope", "适用范围"),
                Localized("Local git repository", "本地 Git 仓库"),
                Localized("Any MCP/REST agent across repos & machines", "跨仓库、跨设备的任意 MCP/REST Agent"),
            ),
        ),
        faqs=(
            FaqItem(
                Localized("Why combine Veynod with git worktrees?", "为什么把 Veynod 和 git worktree 一起用？"),
                Localized(
                    "Git worktrees prevent file-system collisions, while Veynod prevents logical collisions—two agents implementing the same feature or ignoring an upstream API change.",
                    "git worktree 解决磁盘文件冲突，Veynod 解决逻辑冲突——避免两个 Agent 重复造轮子或遗漏上游接口变更。",
                ),
            ),
            FaqItem(
                Localized("What happens if an agent in a worktree crashes?", "如果某个 worktree 里的 Agent 崩溃了怎么办？"),
                Localized(
                    "Its Veynod task lease expires automatically, returning the task to `READY` so another agent can pick it up with full context from the blackboard.",
                    "该任务在 Veynod 上的租约到期后会自动退回 `READY` 状态，其他 Agent 可直接接手并读取黑板上的完整上下文。",
                ),
            ),
            FaqItem(
                Localized("Does Veynod enforce checks before merge?", "Veynod 能在合并前做检查吗？"),
                Localized(
                    "Yes. Quality gates and human proposals let a reviewer or CI agent verify tests and diffs before marking work complete.",
                    "可以。通过质量门禁与人类提案机制，测试、审计 Agent 或人工确认通过后任务才会最终关闭。",
                ),
            ),
        ),
    ),
    ComparisonPage(
        slug="magpie",
        name="Magpie",
        category=Localized("Model & Quota Router", "模型与额度网关"),
        title=Localized(
            "Veynod vs Magpie — Model Routing vs Agent Team Coordination | Veynod",
            "Veynod 对比 Magpie — 模型额度路由与多 Agent 团队协作 | Veynod",
        ),
        description=Localized(
            "Compare Veynod and Magpie. Magpie pools LLM models and quotas for agents; Veynod provides the shared blackboard, task leases, memory, and human approval layer.",
            "对比 Veynod 与 Magpie：Magpie 聚合模型与订阅额度；Veynod 提供多 Agent 协作所需的共享黑板、任务租约、记忆与人工审批。",
        ),
        h1=Localized("Veynod vs Magpie", "Veynod 对比 Magpie"),
        summary=Localized(
            "Magpie pools models, providers, and subscription quotas so agents stay online without hitting rate limits. Veynod handles what happens after the model responds: shared workspace memory, task ownership, delta sync, and human-in-the-loop approvals.",
            "Magpie 负责聚合模型、供应商和订阅额度，让 Agent 不容易因限流中断；Veynod 负责模型之外的协作层——共享工作区记忆、任务归属、增量同步与人工审批。",
        ),
        use_other=Localized(
            "You want unified model routing, fallback providers, and subscription pooling across coding tools.",
            "你需要统一的模型路由、故障切换以及跨工具的订阅额度池。",
        ),
        use_veynod=Localized(
            "You want multiple agents—regardless of which model backs them—to share state, hand off tasks, and wait for human approval on risky edits.",
            "无论底层使用哪个模型，你都需要多个 Agent 共享状态、接力完成任务并在高风险操作前等待人工确认。",
        ),
        use_together=Localized(
            "Point your agents' LLM traffic through Magpie for model availability, and connect them to Veynod over MCP for shared work.",
            "用 Magpie 负责模型与额度调度，用 Veynod 负责黑板记忆与任务协调，两者完全正交。",
        ),
        snippet_label="yaml · orthogonal stack",
        snippet_code=(
            "model_layer: Magpie   # Picks Claude / GPT / Gemini & pools quotas\n"
            "coord_layer: Veynod   # Shared blackboard, task leases, quality gates\n"
            "agent_clients: [Claude Code, Cursor, Codex]"
        ),
        rows=(
            ComparisonRow(
                Localized("Layer", "所属层级"),
                Localized("Model & quota gateway", "模型与额度路由层"),
                Localized("Multi-agent coordination control plane", "多 Agent 协作控制平面"),
            ),
            ComparisonRow(
                Localized("Core problem solved", "解决的核心问题"),
                Localized("Rate limits, provider fallback, cost pooling", "限流切换、多供应商容灾与额度复用"),
                Localized("Context drift, duplicate work, unreviewed edits", "上下文漂移、重复劳动与无审批改动"),
            ),
            ComparisonRow(
                Localized("Protocol", "接入协议"),
                Localized("OpenAI / Anthropic compatible API proxy", "兼容 OpenAI / Anthropic 的模型代理"),
                Localized("MCP (114 tools) + REST API v1", "MCP（114 个工具）+ REST API v1"),
            ),
            ComparisonRow(
                Localized("Human oversight", "人类监管"),
                Localized("Usage & spend visibility", "用量与成本监控"),
                Localized("Task proposals, gates & workspace audit log", "任务提案审批、质量门禁与审计日志"),
            ),
        ),
        faqs=(
            FaqItem(
                Localized("Do Magpie and Veynod compete?", "Magpie 和 Veynod 是竞争关系吗？"),
                Localized(
                    "No. Magpie routes LLM inference requests; Veynod manages shared state and tasks between agents. Many teams need both.",
                    "不是。Magpie 管模型推理请求路由，Veynod 管 Agent 之间的共享状态与任务协作，二者正好互补。",
                ),
            ),
            FaqItem(
                Localized("Can agents running on different models collaborate in Veynod?", "使用不同模型的 Agent 可以在 Veynod 里协作吗？"),
                Localized(
                    "Yes. A Claude Code planner, a Codex implementer, and a Gemini reviewer can all read and write the same Veynod workspace.",
                    "可以。Claude 负责规划、Codex 负责实现、Gemini 负责审查，只要接入同一个 Veynod 工作区就能无缝交接。",
                ),
            ),
            FaqItem(
                Localized("How does Veynod reduce token spend?", "Veynod 如何帮助节省 Token 消耗？"),
                Localized(
                    "Its `workspace_sync` tool sends delta summaries under 30% of the full workspace payload by default, avoiding repeated full-context prompts.",
                    "`workspace_sync` 默认只返回不到完整状态 30% 的增量摘要，大幅减少每次同步的上下文开销。",
                ),
            ),
        ),
    ),
    ComparisonPage(
        slug="n8n",
        name="n8n",
        category=Localized("Workflow Automation", "可视化工作流引擎"),
        title=Localized(
            "Veynod vs n8n — Autonomous Agent Blackboard vs Visual Workflows | Veynod",
            "Veynod 对比 n8n — Agent 共享黑板与可视化工作流对比 | Veynod",
        ),
        description=Localized(
            "Compare Veynod and n8n. n8n runs deterministic visual workflows across SaaS apps; Veynod gives autonomous coding and research agents a shared MCP blackboard.",
            "对比 Veynod 与 n8n：n8n 擅长确定性的 SaaS 可视化工作流；Veynod 为自主运行的编程与研究 Agent 提供共享 MCP 黑板与任务租约。",
        ),
        h1=Localized("Veynod vs n8n", "Veynod 对比 n8n"),
        summary=Localized(
            "n8n is a visual node-based workflow automation platform ideal for deterministic triggers and SaaS integrations. Veynod is a shared blackboard and MCP control plane built for autonomous agents (like Claude Code and Cursor) that negotiate dynamic plans, hold task leases, and ask humans for approval.",
            "n8n 是可视化节点编排平台，适合确定性触发器和 SaaS 集成；Veynod 则是面向自主 Agent（如 Claude Code、Cursor）的共享黑板与 MCP 控制平面，支持动态拆解任务、租约抢占与人工审批。",
        ),
        use_other=Localized(
            "You have fixed trigger-action pipelines (webhooks, Slack, CRM, email) that should run the exact same steps every time.",
            "你有固定步骤的触发式流水线（Webhook、Slack、CRM、邮件同步等），需要每次按确定路径执行。",
        ),
        use_veynod=Localized(
            "Autonomous coding or research agents need to inspect a shared board, claim dynamic subtasks, store versioned findings, and pause for human review.",
            "自主编程或研究 Agent 需要查看共享黑板、动态认领子任务、沉淀带版本的知识，并在关键节点等待人类确认。",
        ),
        use_together=Localized(
            "Trigger a Veynod task from an n8n HTTP/MCP node when a ticket arrives, and let your agent team pick it up and report back.",
            "当工单或告警到来时，用 n8n 的 HTTP/MCP 节点向 Veynod 创建任务，交给 Agent 团队接力处理并回传结果。",
        ),
        snippet_label="http · trigger Veynod from an n8n workflow",
        snippet_code=(
            "POST https://veynod.com/v1/workspaces/{id}/tasks\n"
            "Authorization: Bearer <token>\n"
            '{ "title": "Investigate Sentry spike #482", "priority": "high" }'
        ),
        rows=(
            ComparisonRow(
                Localized("Execution model", "执行模型"),
                Localized("Pre-wired visual DAG of nodes", "预先连线的可视化节点图"),
                Localized("Shared blackboard with autonomous MCP agents", "共享黑板 + 自主 MCP Agent 协作"),
            ),
            ComparisonRow(
                Localized("Primary users", "主要用户"),
                Localized("Ops & automation builders connecting SaaS", "连接 SaaS 的自动化与运营工程师"),
                Localized("Developers running AI coding & research teams", "调度多编程/研究 Agent 团队的开发者"),
            ),
            ComparisonRow(
                Localized("State & memory", "状态与记忆"),
                Localized("Per-execution JSON payloads", "单次执行的节点 JSON 数据"),
                Localized("Versioned workspace documents & knowledge graph", "持久化、版本化的工作区文档与记忆"),
            ),
            ComparisonRow(
                Localized("Integration", "互通方式"),
                Localized("400+ SaaS nodes + HTTP/MCP", "400+ SaaS 节点 + HTTP/MCP"),
                Localized("Called by n8n via REST API v1 or MCP", "可由 n8n 通过 REST API v1 或 MCP 直接调用"),
            ),
        ),
        faqs=(
            FaqItem(
                Localized("When should I choose n8n over Veynod?", "什么时候该用 n8n 而不是 Veynod？"),
                Localized(
                    "Choose n8n for deterministic integrations—like syncing Stripe invoices to Notion. Choose Veynod when AI agents need to collaborate on open-ended engineering or research tasks.",
                    "如果是确定性集成（例如把 Stripe 账单同步到 Notion），n8n 更合适；如果是开放式的工程或研究任务需要多个 AI Agent 协作，选 Veynod。",
                ),
            ),
            FaqItem(
                Localized("Can n8n call Veynod?", "n8n 可以调用 Veynod 吗？"),
                Localized(
                    "Yes. n8n can create tasks, read workspace status, or approve proposals in Veynod using standard REST requests or MCP.",
                    "可以。n8n 可以通过标准 REST 请求或 MCP 在 Veynod 中创建任务、查询状态或读取产物。",
                ),
            ),
            FaqItem(
                Localized("Does Veynod replace coding IDEs or CLI agents?", "Veynod 会替代编程 IDE 或 CLI Agent 吗？"),
                Localized(
                    "No. Veynod connects your existing Claude Code, Cursor, and Codex agents so they work as one coordinated team.",
                    "不会。Veynod 负责把你现有的 Claude Code、Cursor、Codex 等工具连接成一支协同团队。",
                ),
            ),
        ),
    ),
    ComparisonPage(
        slug="langgraph",
        name="LangGraph",
        category=Localized("Python Graph Framework", "Python 状态图框架"),
        title=Localized(
            "Veynod vs LangGraph — Runtime-Agnostic MCP Control Plane | Veynod",
            "Veynod 对比 LangGraph — 跨运行时 MCP 控制平面 vs Python 框架 | Veynod",
        ),
        description=Localized(
            "Compare Veynod and LangGraph. LangGraph is a Python library for building stateful agent graphs in code; Veynod is a hosted or self-hosted MCP blackboard any runtime can join.",
            "对比 Veynod 与 LangGraph：LangGraph 是用 Python 编写有状态 Agent 图的 SDK；Veynod 是开箱即用的跨运行时 MCP 黑板，Claude Code、Cursor、Codex 都能直接接入。",
        ),
        h1=Localized("Veynod vs LangGraph", "Veynod 对比 LangGraph"),
        summary=Localized(
            "LangGraph is a Python/JS SDK for developers writing custom stateful agent graphs inside their own application code. Veynod is a runtime-agnostic MCP and REST control plane: you don't write orchestration code to connect Claude Code, Cursor, Codex, and custom bots—they all join one live workspace.",
            "LangGraph 是让开发者在 Python/JS 代码里手写有状态 Agent 图的框架；Veynod 则是跨运行时的 MCP 与 REST 控制平面，无需编写编排代码，Claude Code、Cursor、Codex 和自定义脚本就能直接接入同一个实时工作区。",
        ),
        use_other=Localized(
            "You are building a custom Python/TypeScript backend product where every state transition is compiled into application code.",
            "你在开发自研 Python/TS 后端产品，希望把每个状态流转硬编码进应用程序。",
        ),
        use_veynod=Localized(
            "You want off-the-shelf coding agents and heterogeneous runtimes to collaborate immediately over MCP with persistent memory, leases, and a web dashboard.",
            "你希望现成的编程 Agent 与异构运行时通过 MCP 立即协作，自带持久化记忆、任务租约和可视化控制台。",
        ),
        use_together=Localized(
            "A LangGraph worker can act as one specialist agent inside a Veynod workspace by calling Veynod's MCP or REST endpoints.",
            "你也可以让一个 LangGraph 服务作为专家 Agent，通过 MCP 或 REST 接入 Veynod 工作区参与团队协作。",
        ),
        snippet_label="python · LangGraph worker calling Veynod REST",
        snippet_code=(
            "# Inside any LangGraph node:\n"
            'requests.post("https://veynod.com/v1/workspaces/ws/proposals",\n'
            '    headers={"Authorization": f"Bearer {KEY}"},\n'
            '    json={"title": "Deploy schema migration", "diff": sql})'
        ),
        rows=(
            ComparisonRow(
                Localized("Form factor", "产品形态"),
                Localized("In-process Python / JS SDK", "嵌入式 Python / JS 开发框架"),
                Localized("MCP + REST control plane & dashboard", "独立 MCP + REST 控制平面与控制台"),
            ),
            ComparisonRow(
                Localized("Works with Claude Code / Cursor", "支持 Claude Code / Cursor"),
                Localized("No (requires custom wrapper)", "不直接支持（需自研封装）"),
                Localized("Native via `npx veynod`", "原生支持，一条 `npx veynod` 接入"),
            ),
            ComparisonRow(
                Localized("Persistence & audit", "持久化与审计"),
                Localized("Configure checkpointers in your DB", "需自行配置数据库 Checkpointer"),
                Localized("Built-in versioned docs, leases & audit trail", "内置版本化文档、租约与审计日志"),
            ),
            ComparisonRow(
                Localized("Federation", "跨工作区联邦"),
                Localized("Single app boundary", "局限于单个应用边界"),
                Localized("Cross-workspace connections & signed chat", "原生支持跨工作区联邦与签名通信"),
            ),
        ),
        faqs=(
            FaqItem(
                Localized("Do I need to write Python to use Veynod?", "使用 Veynod 需要写 Python 编排代码吗？"),
                Localized(
                    "No. Run `npx veynod` to wire Claude Code, Cursor, or Codex directly to the control plane, or use the REST API from any language.",
                    "不需要。运行 `npx veynod` 即可直接连接 Claude Code、Cursor 或 Codex，也可以从任意语言调用 REST API。",
                ),
            ),
            FaqItem(
                Localized("Can a LangGraph agent join a Veynod workspace?", "LangGraph 写的 Agent 能加入 Veynod 吗？"),
                Localized(
                    "Yes. Any LangGraph node can read tasks, sync context, and submit proposals to Veynod over MCP or REST.",
                    "可以。LangGraph 里的节点可以通过 MCP 或 REST 读取 Veynod 任务、同步上下文并提交提案。",
                ),
            ),
            FaqItem(
                Localized("How does Veynod handle human-in-the-loop?", "Veynod 如何处理人工介入？"),
                Localized(
                    "Veynod includes a built-in Proposal queue and web dashboard so humans can inspect diffs and approve or reject actions without building custom UI.",
                    "Veynod 自带提案审批队列与 Web 控制台，人类可直接查看变更并批准或驳回，无需自建审批界面。",
                ),
            ),
        ),
    ),
    ComparisonPage(
        slug="crewai",
        name="CrewAI",
        category=Localized("Role-Based Agent SDK", "角色编排框架"),
        title=Localized(
            "Veynod vs CrewAI — Persistent MCP Blackboard vs Role Scripts | Veynod",
            "Veynod 对比 CrewAI — 持久化 MCP 黑板 vs 角色编排脚本 | Veynod",
        ),
        description=Localized(
            "Compare Veynod and CrewAI. CrewAI orchestrates role-playing agents in Python scripts; Veynod gives real coding and ops agents a shared MCP workspace with leases and approval.",
            "对比 Veynod 与 CrewAI：CrewAI 在 Python 脚本中编排角色化 Agent；Veynod 为真实编程与运维 Agent 提供带任务租约和人工审批的持久化 MCP 工作区。",
        ),
        h1=Localized("Veynod vs CrewAI", "Veynod 对比 CrewAI"),
        summary=Localized(
            "CrewAI lets you define role-playing agents and sequential or hierarchical tasks inside a Python process. Veynod is a persistent, runtime-agnostic blackboard over MCP and REST—built so real-world tools like Claude Code, Cursor, Codex, and background workers collaborate across sessions with versioned memory and human gates.",
            "CrewAI 允许你在 Python 进程里定义角色化 Agent 和串行/层级任务；Veynod 则是基于 MCP 与 REST 的持久化、跨运行时黑板，让 Claude Code、Cursor、Codex 和后台 Worker 跨会话共享版本化记忆、抢占任务租约并接受人工审批。",
        ),
        use_other=Localized(
            "You want a quick Python script where simulated personas pass text prompts in a single run.",
            "你想用一段 Python 脚本快速跑通几个角色在单次运行中的文本接力。",
        ),
        use_veynod=Localized(
            "You want persistent multi-agent engineering teams using real IDEs and CLI tools with fault-tolerant leases, delta sync, and human approval.",
            "你需要真实编程 IDE 和 CLI 工具组成持久化多 Agent 团队，具备容错租约、增量同步和人工审批。",
        ),
        use_together=Localized(
            "Give a CrewAI crew access to Veynod tools over MCP so its outputs land in the same shared workspace as your coding agents.",
            "让 CrewAI 团队通过 MCP 接入 Veynod，把研究或规划结果直接写入共享黑板供编程 Agent 消费。",
        ),
        snippet_label="bash · onboard any agent team",
        snippet_code=(
            "$ npx veynod\n"
            "# Shared board ready at /mcp/ and /v1\n"
            "# Role-agnostic: Claude Code + Cursor + Codex + custom workers"
        ),
        rows=(
            ComparisonRow(
                Localized("Runtime model", "运行模型"),
                Localized("Single Python process / crew run", "单次 Python 进程执行"),
                Localized("Always-on shared workspace over MCP & REST", "常驻共享工作区（MCP + REST）"),
            ),
            ComparisonRow(
                Localized("Agent clients", "支持客户端"),
                Localized("CrewAI Python agents", "CrewAI Python Agent"),
                Localized("Claude Code, Cursor, Codex, custom REST/MCP", "Claude Code、Cursor、Codex 及任意客户端"),
            ),
            ComparisonRow(
                Localized("Fault tolerance", "容错机制"),
                Localized("Process-level retries", "进程级重试"),
                Localized("Task leases auto-recover stalled work", "任务租约超时自动释放并重新入队"),
            ),
            ComparisonRow(
                Localized("Context efficiency", "上下文效率"),
                Localized("Prompt-chained outputs", "Prompt 串联传递全文"),
                Localized("Delta `workspace_sync` (<30% payload)", "增量 `workspace_sync`（默认 <30% 体积）"),
            ),
        ),
        faqs=(
            FaqItem(
                Localized("How is Veynod different from CrewAI?", "Veynod 和 CrewAI 的本质区别是什么？"),
                Localized(
                    "CrewAI is a Python framework for scripting agent crews; Veynod is a shared infrastructure layer (blackboard + MCP server) that any external agent tool can connect to.",
                    "CrewAI 是编写 Agent 团队的 Python 框架；Veynod 是共享基础设施层（黑板 + MCP 服务端），任何外部 Agent 工具都能直接连入。",
                ),
            ),
            FaqItem(
                Localized("Does Veynod persist memory across sessions?", "Veynod 会在多次会话间持久化记忆吗？"),
                Localized(
                    "Yes. Shared documents, task histories, and decisions are versioned in the workspace so new agents onboard via `get_orientation` immediately.",
                    "会的。共享文档、任务历史和决策都持久化并带版本记录，新加入的 Agent 调用 `get_orientation` 即可快速上手。",
                ),
            ),
            FaqItem(
                Localized("Can I try Veynod without installing anything?", "可以不安装先体验 Veynod 吗？"),
                Localized(
                    "Yes. Visit the live read-only demo at `https://veynod.com/demo/` or inspect `https://veynod.com/v1/agent-docs`.",
                    "可以。你可以直接访问 `https://veynod.com/demo/` 查看只读在线演示，或浏览 `https://veynod.com/v1/agent-docs`。",
                ),
            ),
        ),
    ),
)

COMPARISONS_BY_SLUG: dict[str, ComparisonPage] = {item.slug: item for item in COMPARISONS}


def _hub_meta(lang: str) -> tuple[str, str, str, str]:
    if lang == "zh":
        return (
            "工具对比 — Veynod 对比 T3 Code、Orca、Magpie、n8n、LangGraph 与 CrewAI | Veynod",
            "对比 Veynod 与 T3 Code、Orca、Magpie、n8n、LangGraph、CrewAI 的定位差异与搭配方案，了解多 Agent 共享黑板与 MCP 控制平面的适用场景。",
            "Veynod 与主流 Agent 工具对比",
            "大多数工具只服务于单个开发者或单个工作流；Veynod 是位于它们之上或旁侧的共享黑板与 MCP 控制平面，让多 Agent 团队共用计划、记忆与审批门禁。",
        )
    return (
        "Compare Veynod — vs T3 Code, Orca, Magpie, n8n, LangGraph & CrewAI | Veynod",
        "Compare Veynod with T3 Code, Orca, Magpie, n8n, LangGraph, and CrewAI. See where a shared blackboard and MCP control plane fits in your AI stack.",
        "Compare Veynod with other AI agent tools",
        "Most tools help one developer run agents or wire fixed workflows. Veynod is the shared blackboard and MCP control plane where a team of agents shares one plan, versioned memory, task leases, and human approval.",
    )


def _page_shell(
    *,
    lang: str,
    canonical_path: str,
    en_path: str,
    zh_path: str,
    title: str,
    description: str,
    json_ld: dict[str, object],
    body_html: str,
    has_console: bool,
) -> str:
    html_lang = "zh-CN" if lang == "zh" else "en"
    og_locale = "zh_CN" if lang == "zh" else "en_US"
    og_alt = "en_US" if lang == "zh" else "zh_CN"
    zh = lang == "zh"
    home_href = "/zh/" if zh else "/"
    docs_href = "/zh/docs/quickstart/" if zh else "/docs/quickstart/"
    vs_href = "/zh/vs/" if zh else "/vs/"
    switch_href = en_path if zh else zh_path
    switch_label = "English" if zh else "中文"
    font_url = (
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600"
        "&family=Poppins:wght@600&text=Veynod&display=swap"
    )
    ld_json = json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="canonical" href="{SITE}{canonical_path}">
<link rel="alternate" hreflang="en" href="{SITE}{en_path}">
<link rel="alternate" hreflang="zh" href="{SITE}{zh_path}">
<link rel="alternate" hreflang="zh-Hans" href="{SITE}{zh_path}">
<link rel="alternate" hreflang="x-default" href="{SITE}{en_path}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Veynod">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(description)}">
<meta property="og:url" content="{SITE}{canonical_path}">
<meta property="og:locale" content="{og_locale}">
<meta property="og:locale:alternate" content="{og_alt}">
<meta property="og:image" content="{SITE}/og.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{html.escape(title)}">
<meta name="twitter:description" content="{html.escape(description)}">
<meta name="twitter:image" content="{SITE}/og.png">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="manifest" href="/site.webmanifest">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" as="style" href="{font_url}" onload="this.onload=null;this.rel='stylesheet'">
<noscript><link rel="stylesheet" href="{font_url}"></noscript>
<script type="application/ld+json">{ld_json}</script>
<style>
:root {{
  --bg: #080a0e; --surface: #0f131a; --surface-2: #141a23;
  --line: rgba(255,255,255,.09); --line-strong: rgba(255,255,255,.16);
  --text: #f3f5f8; --text-2: #a9b4c4; --text-3: #6c7787;
  --accent: #00c2ff; --accent-soft: rgba(0,194,255,.1);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--text);
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  line-height: 1.65; -webkit-font-smoothing: antialiased;
}}
a {{ color: inherit; text-decoration: none; }}
code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding: 0 24px; }}
header {{
  position: sticky; top: 0; z-index: 20;
  background: rgba(8,10,14,.86); backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--line);
}}
header .wrap {{ display: flex; align-items: center; justify-content: space-between; height: 60px; gap: 16px; }}
.brand {{ font-family: Poppins, Inter, sans-serif; font-weight: 600; font-size: 19px; letter-spacing: -.02em; }}
.nav {{ display: flex; align-items: center; gap: 20px; font-size: 14px; color: var(--text-2); }}
.nav a:hover {{ color: var(--text); }}
.pill {{
  display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
  border-radius: 999px; border: 1px solid var(--line-strong); font-size: 13px; color: var(--text);
}}
.pill.primary {{ background: var(--text); color: var(--bg); border-color: var(--text); font-weight: 500; }}
.crumb {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; color: var(--text-3); margin: 28px 0 12px; }}
.crumb a:hover {{ color: var(--text); }}
.badge {{ display: inline-block; font-size: 12px; color: var(--accent); background: var(--accent-soft); padding: 2px 10px; border-radius: 999px; }}
h1 {{ font-size: clamp(30px, 4.4vw, 44px); line-height: 1.15; letter-spacing: -.025em; margin: 12px 0 16px; }}
h2 {{ font-size: 22px; line-height: 1.25; letter-spacing: -.015em; margin: 40px 0 16px; }}
.lede {{ font-size: 17px; color: var(--text-2); max-width: 78ch; margin: 0 0 28px; }}
.cta-row {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 36px; }}
.card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin: 24px 0; }}
.card {{
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 20px;
  transition: border-color .15s ease;
}}
.card:hover {{ border-color: var(--line-strong); }}
.card h3 {{ margin: 0 0 8px; font-size: 16px; }}
.card p {{ margin: 0; font-size: 14px; color: var(--text-2); }}
.table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ text-align: left; vertical-align: top; padding: 14px 18px; border-bottom: 1px solid var(--line); }}
thead th {{ font-size: 12px; font-weight: 600; color: var(--text-3); text-transform: uppercase; letter-spacing: .04em; }}
tbody th {{ font-weight: 500; color: var(--text); width: 22%; }}
tbody td {{ color: var(--text-2); }}
tbody td.self {{ color: var(--text); background: rgba(0,194,255,.04); }}
tbody tr:last-child > * {{ border-bottom: 0; }}
.term {{ background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; margin: 24px 0; }}
.term-bar {{ padding: 10px 16px; font-size: 12px; color: var(--text-3); border-bottom: 1px solid var(--line); }}
.term pre {{ margin: 0; padding: 16px; font-size: 13px; overflow-x: auto; color: var(--text); }}
.faq-item {{ border-bottom: 1px solid var(--line); padding: 16px 0; }}
.faq-item h3 {{ margin: 0 0 6px; font-size: 16px; }}
.faq-item p {{ margin: 0; font-size: 14px; color: var(--text-2); }}
footer {{ border-top: 1px solid var(--line); margin-top: 64px; padding: 48px 0; color: var(--text-3); font-size: 13px; }}
footer .wrap {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 28px; }}
footer h3 {{ font-size: 12px; color: var(--text-2); margin: 0 0 12px; font-weight: 600; }}
footer nav {{ display: flex; flex-direction: column; gap: 8px; }}
footer nav a:hover {{ color: var(--text); }}
@media (max-width: 680px) {{
  .nav .hide-sm {{ display: none; }}
}}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <a class="brand" href="{home_href}">Veynod</a>
    <nav class="nav" aria-label="Main navigation">
      <a class="hide-sm" href="{home_href}">{"首页" if zh else "Home"}</a>
      <a href="{vs_href}">{"工具对比" if zh else "Compare"}</a>
      <a href="{docs_href}">{"文档" if zh else "Docs"}</a>
      <a class="hide-sm" href="/demo/">{"在线演示" if zh else "Live demo"}</a>
      <a class="pill" href="{switch_href}">{switch_label}</a>
    </nav>
  </div>
</header>
<main class="wrap">
{body_html}
</main>
{_footer(lang, has_console)}
</body>
</html>
"""


@lru_cache(maxsize=16)
def render_vs_hub(lang: str = "en", has_console: bool = True) -> str:
    zh = lang == "zh"
    title, description, h1, lede = _hub_meta(lang)
    en_path, zh_path = "/vs/", "/zh/vs/"
    canonical = zh_path if zh else en_path
    cards = "".join(
        f'<a class="card" href="{"/zh" if zh else ""}/vs/{c.slug}/">'
        f'<span class="badge">{html.escape(c.category.pick(lang))}</span>'
        f'<h3 style="margin-top:10px">Veynod vs {html.escape(c.name)}</h3>'
        f"<p>{html.escape(c.description.pick(lang))}</p>"
        f"</a>"
        for c in COMPARISONS
    )
    body = (
        f'<nav class="crumb" aria-label="Breadcrumb">'
        f'<a href="{"/zh/" if zh else "/"}">{"首页" if zh else "Home"}</a>'
        f'<span>/</span><span>{"工具对比" if zh else "Compare"}</span>'
        f'<span class="badge">{"更新于" if zh else "Updated"} {UPDATED_DATE}</span>'
        f"</nav>"
        f"<h1>{html.escape(h1)}</h1>"
        f'<p class="lede">{html.escape(lede)}</p>'
        f'<div class="cta-row">'
        f'<a class="pill primary" href="{"/zh/docs/quickstart/" if zh else "/docs/quickstart/"}">'
        f'{"快速上手 npx veynod" if zh else "Get started with npx veynod"}</a>'
        f'<a class="pill" href="/demo/">{"查看在线演示" if zh else "Live read-only demo"}</a>'
        f"</div>"
        f'<section class="card-grid">{cards}</section>'
    )
    json_ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "CollectionPage",
                "name": title,
                "description": description,
                "url": f"{SITE}{canonical}",
                "inLanguage": "zh-CN" if zh else "en",
                "dateModified": UPDATED_DATE,
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "首页" if zh else "Home",
                     "item": f"{SITE}{'/zh/' if zh else '/'}"},
                    {"@type": "ListItem", "position": 2, "name": "工具对比" if zh else "Compare",
                     "item": f"{SITE}{canonical}"},
                ],
            },
        ],
    }
    return _page_shell(
        lang=lang, canonical_path=canonical, en_path=en_path, zh_path=zh_path,
        title=title, description=description, json_ld=json_ld,
        body_html=body, has_console=has_console,
    )


@lru_cache(maxsize=32)
def render_vs_page(slug: str, lang: str = "en", has_console: bool = True) -> str | None:
    page = COMPARISONS_BY_SLUG.get(slug)
    if page is None:
        return None
    zh = lang == "zh"
    en_path = f"/vs/{slug}/"
    zh_path = f"/zh/vs/{slug}/"
    canonical = zh_path if zh else en_path
    title = page.title.pick(lang)
    description = page.description.pick(lang)

    rows_html = "".join(
        f"<tr><th scope='row'>{html.escape(r.dimension.pick(lang))}</th>"
        f"<td>{html.escape(r.other.pick(lang))}</td>"
        f"<td class='self'>{html.escape(r.veynod.pick(lang))}</td></tr>"
        for r in page.rows
    )
    faqs_html = "".join(
        f"<div class='faq-item'><h3>{html.escape(f.question.pick(lang))}</h3>"
        f"<p>{html.escape(f.answer.pick(lang))}</p></div>"
        for f in page.faqs
    )
    related_html = "".join(
        f'<a class="pill" href="{"/zh" if zh else ""}/vs/{c.slug}/">vs {html.escape(c.name)}</a>'
        for c in COMPARISONS if c.slug != slug
    )

    body = (
        f'<nav class="crumb" aria-label="Breadcrumb">'
        f'<a href="{"/zh/" if zh else "/"}">{"首页" if zh else "Home"}</a>'
        f'<span>/</span><a href="{"/zh/vs/" if zh else "/vs/"}">{"工具对比" if zh else "Compare"}</a>'
        f'<span>/</span><span>{html.escape(page.name)}</span>'
        f'<span class="badge">{"更新于" if zh else "Updated"} {UPDATED_DATE}</span>'
        f"</nav>"
        f"<h1>{html.escape(page.h1.pick(lang))}</h1>"
        f'<p class="lede">{html.escape(page.summary.pick(lang))}</p>'
        f'<div class="cta-row">'
        f'<a class="pill primary" href="{"/zh/docs/quickstart/" if zh else "/docs/quickstart/"}">'
        f'{"运行 npx veynod" if zh else "Start with npx veynod"}</a>'
        f'<a class="pill" href="/demo/">{"在线演示" if zh else "Live demo"}</a>'
        f'<a class="pill" href="{"/zh/docs/mcp/" if zh else "/docs/mcp/"}">'
        f'{"MCP 接入文档" if zh else "MCP setup docs"}</a>'
        f"</div>"
        f'<section class="table-wrap"><table>'
        f"<thead><tr>"
        f'<th scope="col">{"对比维度" if zh else "Dimension"}</th>'
        f'<th scope="col">{html.escape(page.name)}</th>'
        f'<th scope="col">Veynod</th>'
        f"</tr></thead><tbody>{rows_html}</tbody></table></section>"
        f'<section class="card-grid">'
        f'<div class="card"><h3>{"何时选 " + html.escape(page.name) if zh else "When to choose " + html.escape(page.name)}</h3>'
        f"<p>{html.escape(page.use_other.pick(lang))}</p></div>"
        f'<div class="card"><h3>{"何时选 Veynod" if zh else "When to choose Veynod"}</h3>'
        f"<p>{html.escape(page.use_veynod.pick(lang))}</p></div>"
        f'<div class="card"><h3>{"如何搭配使用" if zh else "Using them together"}</h3>'
        f"<p>{html.escape(page.use_together.pick(lang))}</p></div>"
        f"</section>"
        f'<section class="term"><div class="term-bar">{html.escape(page.snippet_label)}</div>'
        f"<pre><code>{html.escape(page.snippet_code)}</code></pre></section>"
        f'<section><h2>{"常见问题" if zh else "Frequently asked questions"}</h2>{faqs_html}</section>'
        f'<section><h2>{"更多对比" if zh else "More comparisons"}</h2>'
        f'<div class="cta-row">{related_html}</div></section>'
    )

    json_ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "name": title,
                "description": description,
                "url": f"{SITE}{canonical}",
                "inLanguage": "zh-CN" if zh else "en",
                "dateModified": UPDATED_DATE,
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "首页" if zh else "Home",
                     "item": f"{SITE}{'/zh/' if zh else '/'}"},
                    {"@type": "ListItem", "position": 2, "name": "工具对比" if zh else "Compare",
                     "item": f"{SITE}{'/zh/vs/' if zh else '/vs/'}"},
                    {"@type": "ListItem", "position": 3, "name": page.h1.pick(lang),
                     "item": f"{SITE}{canonical}"},
                ],
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": f.question.pick(lang),
                        "acceptedAnswer": {"@type": "Answer", "text": f.answer.pick(lang)},
                    }
                    for f in page.faqs
                ],
            },
        ],
    }
    return _page_shell(
        lang=lang, canonical_path=canonical, en_path=en_path, zh_path=zh_path,
        title=title, description=description, json_ld=json_ld,
        body_html=body, has_console=has_console,
    )
