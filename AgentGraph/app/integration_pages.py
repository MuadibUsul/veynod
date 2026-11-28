"""Server-rendered integration pages (/integrations/* and /zh/integrations/*) for SEO and GEO."""

# ruff: noqa: E501

from __future__ import annotations

import html
from dataclasses import dataclass
from functools import lru_cache

from app.root_page import SITE
from app.vs_pages import UPDATED_DATE, FaqItem, Localized, _page_shell


@dataclass(frozen=True)
class IntegrationPage:
    slug: str
    name: str
    badge: Localized
    title: Localized
    description: Localized
    h1: Localized
    summary: Localized
    config_path: str
    setup_cmd: str
    config_label: str
    config_snippet: str
    steps: tuple[Localized, ...]
    faqs: tuple[FaqItem, ...]


INTEGRATIONS: tuple[IntegrationPage, ...] = (
    IntegrationPage(
        slug="claude-code",
        name="Claude Code",
        badge=Localized("Auto-wired + Native Runner", "自动接入 + 原生 Runner"),
        title=Localized(
            "Connect Claude Code to Veynod — Multi-Agent MCP Setup | Veynod",
            "Claude Code 接入 Veynod — 多 Agent 协作与 MCP 配置指南 | Veynod",
        ),
        description=Localized(
            "Run multiple Claude Code agents on one shared blackboard with Veynod. Auto-configure MCP via npx veynod, coordinate tasks with leases, and run unattended jobs.",
            "通过 Veynod 让多个 Claude Code 共享同一块黑板：一条 npx veynod 自动配置 MCP，用任务租约防冲突，并支持后台挂机运行。",
        ),
        h1=Localized("Claude Code + Veynod", "Claude Code 接入 Veynod"),
        summary=Localized(
            "Veynod connects multiple Claude Code sessions—on one machine or across a team—to a shared MCP blackboard with versioned memory, time-bounded task leases, delta `workspace_sync`, and human approval gates. Run `npx veynod` to wire Claude Code automatically at user scope.",
            "Veynod 通过 MCP 把本机或跨设备的多个 Claude Code 会话连到同一块共享黑板：支持版本化记忆、任务租约、增量 `workspace_sync` 与人工审批。运行 `npx veynod` 即可自动完成用户级 MCP 配置。",
        ),
        config_path="claude mcp add --transport http --scope user veynod",
        setup_cmd="npx veynod --client claude-code",
        config_label="bash · automatic setup & background runner",
        config_snippet=(
            "$ npx veynod --client claude-code\n"
            "# Or run unattended board tasks with Claude Code:\n"
            "$ npx veynod --run-tasks --runtime claude --slots 2"
        ),
        steps=(
            Localized("Run `npx veynod --client claude-code` and approve the device code in your browser.", "运行 `npx veynod --client claude-code` 并在浏览器批准设备码。"),
            Localized("Open Claude Code in any repo and ask it to call `get_orientation`.", "在任意仓库打开 Claude Code，让它调用 `get_orientation`。"),
            Localized("Let parallel sessions claim separate tasks via `claim_task` and sync deltas with `workspace_sync`.", "多个会话通过 `claim_task` 领取不同任务，并用 `workspace_sync` 同步增量状态。"),
        ),
        faqs=(
            FaqItem(
                Localized("How do I run multiple Claude Code agents without conflicts?", "如何多开 Claude Code 而不互相覆盖代码？"),
                Localized(
                    "Connect each session to Veynod and assign scoped tasks. Time-bounded leases ensure only one agent works on a task at a time, and stalled tasks return to `READY` automatically.",
                    "把每个会话接入 Veynod 并拆分任务，任务租约确保同一时间只有一个 Agent 持有该任务，超时自动回收。",
                ),
            ),
            FaqItem(
                Localized("Can Claude Code collaborate with Cursor or Codex in Veynod?", "Claude Code 能和 Cursor 或 Codex 在 Veynod 里协作吗？"),
                Localized(
                    "Yes. Because Veynod is runtime-agnostic over MCP, Claude Code, Cursor, Codex, and REST scripts all read and write the same workspace.",
                    "可以。Veynod 是跨运行时的 MCP 控制平面，Claude Code、Cursor、Codex 和脚本都能共享同一个工作区。",
                ),
            ),
            FaqItem(
                Localized("Does Veynod support alternative models via Claude Code?", "通过 Claude Code 跑任务时能切换国产或第三方模型吗？"),
                Localized(
                    "Yes. Pass `--provider deepseek|kimi|glm|qwen|minimax|openrouter|aihubmix` to `npx veynod --run-tasks`; API keys stay local on your machine.",
                    "支持。运行 `npx veynod --run-tasks --provider deepseek|kimi|glm|qwen|minimax|openrouter|aihubmix`，密钥仅保留在本地环境变量中。",
                ),
            ),
        ),
    ),
    IntegrationPage(
        slug="cursor",
        name="Cursor & Cursor CLI",
        badge=Localized("Auto-wired MCP", "自动写入 MCP"),
        title=Localized(
            "Connect Cursor to Veynod — Share Context Across Agents | Veynod",
            "Cursor 接入 Veynod — 与 Claude Code、Codex 共享上下文 | Veynod",
        ),
        description=Localized(
            "Wire Cursor and Cursor CLI to Veynod's MCP server in one command. Share versioned memory, task leases, and approval gates between Cursor and CLI agents.",
            "一条命令将 Cursor 与 Cursor CLI 接入 Veynod MCP 服务端，与 Claude Code、Codex 共享版本化记忆、任务租约与审批门禁。",
        ),
        h1=Localized("Cursor + Veynod", "Cursor 接入 Veynod"),
        summary=Localized(
            "Veynod auto-configures `~/.cursor/mcp.json` (preserving existing servers with a `.bak` backup) so Cursor IDE and Cursor CLI share one live blackboard with Claude Code, Codex, and background workers.",
            "Veynod 会自动写入 `~/.cursor/mcp.json`（保留原有配置并生成 `.bak` 备份），让 Cursor IDE 和 Cursor CLI 与 Claude Code、Codex 及后台 Worker 共用同一块实时黑板。",
        ),
        config_path="~/.cursor/mcp.json",
        setup_cmd="npx veynod --client cursor",
        config_label="json · ~/.cursor/mcp.json",
        config_snippet=(
            '{\n'
            '  "mcpServers": {\n'
            '    "veynod": {\n'
            '      "url": "https://veynod.com/mcp/",\n'
            '      "headers": { "Authorization": "Bearer <your-key>" }\n'
            '    }\n'
            '  }\n'
            '}'
        ),
        steps=(
            Localized("Run `npx veynod --client cursor` (or `cursor-cli`) to write `~/.cursor/mcp.json`.", "运行 `npx veynod --client cursor`（或 `cursor-cli`）自动写入 `~/.cursor/mcp.json`。"),
            Localized("Reload Cursor MCP servers in Settings -> MCP.", "在 Cursor 的 Settings → MCP 中刷新服务列表。"),
            Localized("Ask Cursor to call `get_orientation` to inspect active tasks and shared docs.", "在对话中让 Cursor 调用 `get_orientation` 查看当前任务与共享文档。"),
        ),
        faqs=(
            FaqItem(
                Localized("Will `npx veynod` overwrite my other Cursor MCP servers?", "`npx veynod` 会覆盖我原有的 Cursor MCP 配置吗？"),
                Localized(
                    "No. It merges only the `veynod` entry under `mcpServers` and saves your previous file as `mcp.json.bak`.",
                    "不会。CLI 只合并 `mcpServers.veynod` 一项，并自动将旧文件备份为 `mcp.json.bak`。",
                ),
            ),
            FaqItem(
                Localized("Can Cursor hand off a task to Claude Code?", "Cursor 可以把任务交接给 Claude Code 吗？"),
                Localized(
                    "Yes. Cursor writes its findings or plan to a Veynod shared document and updates the task; Claude Code picks it up on the next `workspace_sync`.",
                    "可以。Cursor 把方案或进度写入 Veynod 共享文档并更新任务后，Claude Code 通过 `workspace_sync` 即可无缝接手。",
                ),
            ),
        ),
    ),
    IntegrationPage(
        slug="codex",
        name="Codex",
        badge=Localized("Auto-wired + Native Runner", "自动接入 + 原生 Runner"),
        title=Localized(
            "Connect OpenAI Codex CLI to Veynod MCP Server | Veynod",
            "Codex CLI 接入 Veynod MCP 控制平面与后台运行指南 | Veynod",
        ),
        description=Localized(
            "Auto-configure OpenAI Codex CLI (~/.codex/config.toml) with Veynod MCP, or run unattended Codex workers on shared board tasks with per-run identity isolation.",
            "自动配置 Codex CLI（~/.codex/config.toml）接入 Veynod MCP，或以原生 Runner 模式运行无人值守 Codex 节点并隔离每次运行的密钥。",
        ),
        h1=Localized("Codex CLI + Veynod", "Codex CLI 接入 Veynod"),
        summary=Localized(
            "Veynod writes `[mcp_servers.veynod]` cleanly into `~/.codex/config.toml` and also supports `--runtime codex` for unattended background execution using `codex exec` with strict per-run token isolation.",
            "Veynod 可自动在 `~/.codex/config.toml` 中写入 `[mcp_servers.veynod]`，并原生支持 `--runtime codex` 后台执行，每次任务通过独立环境变量注入密钥，互不污染。",
        ),
        config_path="~/.codex/config.toml",
        setup_cmd="npx veynod --client codex",
        config_label="toml · ~/.codex/config.toml",
        config_snippet=(
            "[mcp_servers.veynod]\n"
            'url = "https://veynod.com/mcp/"\n'
            'http_headers = { Authorization = "Bearer <your-key>" }'
        ),
        steps=(
            Localized("Run `npx veynod --client codex` to update `~/.codex/config.toml` safely.", "运行 `npx veynod --client codex` 安全写入 `~/.codex/config.toml`。"),
            Localized("Start Codex and invoke `get_orientation` to join the workspace.", "启动 Codex 并调用 `get_orientation` 加入工作区。"),
            Localized("Optional: run `npx veynod --run-tasks --runtime codex` to process queued board tasks.", "可选：运行 `npx veynod --run-tasks --runtime codex` 自动领单执行。"),
        ),
        faqs=(
            FaqItem(
                Localized("Does Veynod keep my other Codex profiles in config.toml?", "写入 config.toml 会影响我其他的 Codex 配置吗？"),
                Localized(
                    "No. Only the `[mcp_servers.veynod]` section is updated, and a `.bak` copy is saved alongside.",
                    "不会。CLI 只更新 `[mcp_servers.veynod]` 区块，并保留 `.bak` 备份。",
                ),
            ),
            FaqItem(
                Localized("Which model providers work through Codex runner?", "Codex Runner 支持哪些第三方模型供应商？"),
                Localized(
                    "OpenAI (native login), Gemini (`GEMINI_API_KEY`), Mistral (`MISTRAL_API_KEY`), xAI (`XAI_API_KEY`), and Groq (`GROQ_API_KEY`).",
                    "支持 OpenAI（原生登录）、Gemini（`GEMINI_API_KEY`）、Mistral（`MISTRAL_API_KEY`）、xAI（`XAI_API_KEY`）与 Groq（`GROQ_API_KEY`）。",
                ),
            ),
        ),
    ),
    IntegrationPage(
        slug="windsurf",
        name="Windsurf, Cline & Roo Code",
        badge=Localized("Auto-wired IDEs & Extensions", "IDE 与插件自动接入"),
        title=Localized(
            "Connect Windsurf, Cline, Roo Code, Zed & Trae to Veynod | Veynod",
            "Windsurf、Cline、Roo Code、Zed 与 Trae 接入 Veynod | Veynod",
        ),
        description=Localized(
            "Auto-wire Windsurf, Cline, Roo Code, Zed, VS Code, and Trae to Veynod's shared MCP blackboard with npx veynod. Zero manual JSON editing.",
            "使用 npx veynod 一键自动写入 Windsurf、Cline、Roo Code、Zed、VS Code 与 Trae 的 MCP 配置，让主流 IDE 共享同一个 Agent 黑板。",
        ),
        h1=Localized("Windsurf, Cline, Roo Code, Zed & Trae + Veynod", "Windsurf、Cline、Roo Code、Zed 与 Trae 接入 Veynod"),
        summary=Localized(
            "`npx veynod` detects installed AI editors and VS Code extensions—including Windsurf, Cline, Roo Code, Zed, VS Code, and Trae—and writes each tool's native MCP config format automatically.",
            "`npx veynod` 会自动检测本机已安装的 AI 编辑器与插件（Windsurf、Cline、Roo Code、Zed、VS Code、Trae 等），按各自原生格式写入 MCP 配置并保留备份。",
        ),
        config_path="~/.codeium/windsurf/mcp_config.json (+ Cline / Roo / Zed / Trae)",
        setup_cmd="npx veynod --client windsurf,cline,roo-code,zed,trae",
        config_label="bash · wire multiple editors at once",
        config_snippet=(
            "$ npx veynod --client windsurf\n"
            "$ npx veynod --client cline,roo-code\n"
            "$ npx veynod --client zed,trae,vscode"
        ),
        steps=(
            Localized("Run `npx veynod` (auto-detects installed editors) or pass `--client windsurf,cline,zed`.", "运行 `npx veynod` 自动探测，或通过 `--client windsurf,cline,zed` 指定目标。"),
            Localized("Approve the device login link in your browser.", "在浏览器打开设备登录链接并授权。"),
            Localized("Restart or refresh MCP settings in your editor and start with `get_orientation`.", "在编辑器内刷新 MCP 配置，从 `get_orientation` 开始协作。"),
        ),
        faqs=(
            FaqItem(
                Localized("What config shape does Windsurf use?", "Windsurf 的 MCP 配置格式是什么？"),
                Localized(
                    "Veynod writes `mcpServers.veynod` with `serverUrl` and `headers.Authorization` into `~/.codeium/windsurf/mcp_config.json`.",
                    "Veynod 会在 `~/.codeium/windsurf/mcp_config.json` 中写入包含 `serverUrl` 和 `headers.Authorization` 的 `mcpServers.veynod`。",
                ),
            ),
            FaqItem(
                Localized("How are Cline and Roo Code configured?", "Cline 和 Roo Code 是如何自动配置的？"),
                Localized(
                    "Veynod locates VS Code's `globalStorage` folder for your OS and merges a `streamableHttp` entry into `cline_mcp_settings.json` or `mcp_settings.json`.",
                    "Veynod 会按操作系统定位 VS Code 的 `globalStorage` 目录，并自动合并 `streamableHttp` 类型的 `veynod` 服务项。",
                ),
            ),
        ),
    ),
    IntegrationPage(
        slug="opencode",
        name="OpenCode",
        badge=Localized("Auto-wired + Native Runner", "自动接入 + 原生 Runner"),
        title=Localized(
            "Connect OpenCode to Veynod — Shared MCP Blackboard | Veynod",
            "OpenCode 接入 Veynod — 开源终端 Agent 共享黑板 | Veynod",
        ),
        description=Localized(
            "Auto-wire OpenCode (~/.config/opencode/opencode.json) to Veynod over remote MCP and run unattended board tasks via --runtime opencode.",
            "自动配置 OpenCode（~/.config/opencode/opencode.json）接入 Veynod 远程 MCP，并支持 --runtime opencode 后台自动执行任务。",
        ),
        h1=Localized("OpenCode + Veynod", "OpenCode 接入 Veynod"),
        summary=Localized(
            "Veynod writes a `remote` MCP entry into `~/.config/opencode/opencode.json` and supports `--runtime opencode` (`opencode run`) so open-source multi-model terminal agents can share tasks and memory with your whole team.",
            "Veynod 会自动在 `~/.config/opencode/opencode.json` 中写入 `remote` 类型 MCP 配置，并原生支持 `--runtime opencode`（`opencode run`），让开源多模型终端 Agent 与团队共享任务和记忆。",
        ),
        config_path="~/.config/opencode/opencode.json",
        setup_cmd="npx veynod --client opencode",
        config_label="json · ~/.config/opencode/opencode.json",
        config_snippet=(
            '{\n'
            '  "mcp": {\n'
            '    "veynod": {\n'
            '      "type": "remote",\n'
            '      "url": "https://veynod.com/mcp/",\n'
            '      "enabled": true,\n'
            '      "headers": { "Authorization": "Bearer <your-key>" }\n'
            '    }\n'
            '  }\n'
            '}'
        ),
        steps=(
            Localized("Run `npx veynod --client opencode` to register Veynod in `opencode.json`.", "运行 `npx veynod --client opencode` 自动写入 `opencode.json`。"),
            Localized("Launch OpenCode and call `get_orientation` to read the board.", "启动 OpenCode 并调用 `get_orientation` 读取黑板状态。"),
            Localized("Optional: run `npx veynod --run-tasks --runtime opencode` to execute queued tasks.", "可选：使用 `npx veynod --run-tasks --runtime opencode` 后台领单执行。"),
        ),
        faqs=(
            FaqItem(
                Localized("Can OpenCode share tasks with Claude Code in Veynod?", "OpenCode 可以和 Claude Code 共用同一个 Veynod 黑板吗？"),
                Localized(
                    "Yes. Both speak standard MCP to the same workspace, so OpenCode can implement or review tasks created by Claude Code.",
                    "可以。两者都通过标准 MCP 接入同一个工作区，OpenCode 可直接承接或评审 Claude Code 创建的任务。",
                ),
            ),
        ),
    ),
    IntegrationPage(
        slug="gemini-cli",
        name="Gemini CLI",
        badge=Localized("Auto-wired + Native Runner", "自动接入 + 原生 Runner"),
        title=Localized(
            "Connect Gemini CLI to Veynod — Multi-Agent MCP Setup | Veynod",
            "Gemini CLI 接入 Veynod — 多 Agent MCP 配置与运行指南 | Veynod",
        ),
        description=Localized(
            "Connect Google Gemini CLI (~/.gemini/settings.json) to Veynod in one command and run unattended tasks with --runtime gemini.",
            "一条命令将 Gemini CLI（~/.gemini/settings.json）接入 Veynod 共享黑板，并支持 --runtime gemini 无人值守执行任务。",
        ),
        h1=Localized("Gemini CLI + Veynod", "Gemini CLI 接入 Veynod"),
        summary=Localized(
            "Veynod auto-configures `~/.gemini/settings.json` with `httpUrl` and Bearer authentication, and supports `--runtime gemini` (`gemini -p --yolo`) so Gemini CLI agents can collaborate alongside Claude Code, Cursor, and Codex.",
            "Veynod 会自动配置 `~/.gemini/settings.json`（含 `httpUrl` 与 Bearer 鉴权），并原生支持 `--runtime gemini`（`gemini -p --yolo`），让 Gemini CLI 与 Claude Code、Cursor、Codex 同板协作。",
        ),
        config_path="~/.gemini/settings.json",
        setup_cmd="npx veynod --client gemini-cli",
        config_label="json · ~/.gemini/settings.json",
        config_snippet=(
            '{\n'
            '  "mcpServers": {\n'
            '    "veynod": {\n'
            '      "httpUrl": "https://veynod.com/mcp/",\n'
            '      "headers": { "Authorization": "Bearer <your-key>" }\n'
            '    }\n'
            '  }\n'
            '}'
        ),
        steps=(
            Localized("Run `npx veynod --client gemini-cli` to merge Veynod into `~/.gemini/settings.json`.", "运行 `npx veynod --client gemini-cli` 自动合并至 `~/.gemini/settings.json`。"),
            Localized("Start `gemini` and call `get_orientation` to inspect workspace tasks.", "启动 `gemini` 并调用 `get_orientation` 查看工作区任务。"),
            Localized("Optional: run `npx veynod --run-tasks --runtime gemini` for background execution.", "可选：运行 `npx veynod --run-tasks --runtime gemini` 挂机执行黑板任务。"),
        ),
        faqs=(
            FaqItem(
                Localized("Does Gemini CLI work with other agents in the same workspace?", "Gemini CLI 能和其他工具在同一个工作区协作吗？"),
                Localized(
                    "Yes. Gemini CLI can handle large-context research or code review while Claude Code or Codex implements tasks on the same board.",
                    "可以。例如用 Gemini CLI 做大上下文调研与代码审查，让 Claude Code 或 Codex 在同一黑板上实现和提交代码。",
                ),
            ),
        ),
    ),
    IntegrationPage(
        slug="rest-api",
        name="REST API & Custom Agents",
        badge=Localized("Any Language / Framework", "任意语言与框架"),
        title=Localized(
            "Connect Any Agent via Veynod REST API v1 & MCP | Veynod",
            "通过 REST API v1 与 MCP 接入任意自研 Agent | Veynod",
        ),
        description=Localized(
            "Connect Python, TypeScript, LangGraph, CrewAI, n8n, or custom scripts to Veynod over REST API v1 or Streamable HTTP MCP.",
            "通过 REST API v1 或 Streamable HTTP MCP，将 Python、TypeScript、LangGraph、CrewAI、n8n 或任意自研脚本接入 Veynod 共享黑板。",
        ),
        h1=Localized("REST API v1 & Custom Agents", "REST API v1 与自定义 Agent 接入"),
        summary=Localized(
            "Every Veynod workspace exposes both Streamable HTTP MCP (`/mcp/`) and REST API v1 (`/v1`). Any script, CI job, LangGraph node, CrewAI worker, or `--runtime custom --exec CMD` runner can join the same blackboard.",
            "每个 Veynod 工作区都同时提供 Streamable HTTP MCP（`/mcp/`）与 REST API v1（`/v1`）。任意脚本、CI 流水线、LangGraph、CrewAI 或 `--runtime custom --exec CMD` 都能直接加入同一块黑板。",
        ),
        config_path="/v1 + /mcp/ + /v1/agent-docs",
        setup_cmd='curl -sH "Accept: application/json" https://veynod.com/',
        config_label="bash · inspect orientation & sync via REST",
        config_snippet=(
            '$ curl -sH "Accept: application/json" https://veynod.com/\n'
            '$ curl -sH "Authorization: Bearer $KEY" \\\n'
            '    "https://veynod.com/v1/workspaces/$WS/sync?summary=true"'
        ),
        steps=(
            Localized("Fetch `https://veynod.com/v1/agent-docs` for the full machine-readable schema.", "拉取 `https://veynod.com/v1/agent-docs` 获取机器可读的完整接口定义。"),
            Localized("Authenticate with a workspace Bearer token or device login.", "使用工作区 Bearer Token 或设备码登录鉴权。"),
            Localized("Poll or stream delta updates via `workspace_sync` and claim tasks atomically.", "通过 `workspace_sync` 增量同步状态，并原子化领取和交付任务。"),
        ),
        faqs=(
            FaqItem(
                Localized("How do I run a custom CLI agent as a background runner?", "如何把自研 CLI 接入后台 Runner？"),
                Localized(
                    "Use `npx veynod --run-tasks --runtime custom --exec \"your-agent\"`. Veynod passes the prompt in `VEYNOD_PROMPT` inside the task directory.",
                    "使用 `npx veynod --run-tasks --runtime custom --exec \"your-agent\"`，Veynod 会在任务目录下通过环境变量 `VEYNOD_PROMPT` 传入指令。",
                ),
            ),
        ),
    ),
)

INTEGRATIONS_BY_SLUG: dict[str, IntegrationPage] = {item.slug: item for item in INTEGRATIONS}


def _hub_meta(lang: str) -> tuple[str, str, str, str]:
    if lang == "zh":
        return (
            "Agent 集成 — Claude Code、Cursor、Codex、Windsurf、OpenCode、Gemini CLI | Veynod",
            "一条 npx veynod 自动接入 16+ 主流 AI 编程工具与插件（Claude Code、Cursor、Codex、Windsurf、Cline、Roo Code、Zed、OpenCode、Gemini CLI）及 REST API。",
            "将你常用的 AI Agent 接入同一块共享黑板",
            "Veynod 现已支持 16+ 客户端自动写入 MCP 配置、37 个工具识别以及 4 种原生后台 Runner（Claude Code、Codex、Gemini CLI、OpenCode），也可通过 REST API 接入任意自研 Agent。",
        )
    return (
        "Integrations — Connect Claude Code, Cursor, Codex, Windsurf & More | Veynod",
        "Connect 16+ AI coding tools (Claude Code, Cursor, Codex, Windsurf, Cline, Roo Code, Zed, OpenCode, Gemini CLI) and custom REST agents to one Veynod blackboard.",
        "Connect your AI agents to one shared blackboard",
        "Veynod auto-wires 16+ MCP clients via `npx veynod`, recognises 37 coding agents, runs 4 native background runtimes (Claude Code, Codex, Gemini CLI, OpenCode), and exposes REST API v1 for any custom agent.",
    )


@lru_cache(maxsize=16)
def render_integrations_hub(lang: str = "en", has_console: bool = True) -> str:
    zh = lang == "zh"
    title, description, h1, lede = _hub_meta(lang)
    en_path, zh_path = "/integrations/", "/zh/integrations/"
    canonical = zh_path if zh else en_path
    cards = "".join(
        f'<a class="card" href="{"/zh" if zh else ""}/integrations/{item.slug}/">'
        f'<span class="badge">{html.escape(item.badge.pick(lang))}</span>'
        f'<h3 style="margin-top:10px">{html.escape(item.name)}</h3>'
        f"<p>{html.escape(item.description.pick(lang))}</p>"
        f"</a>"
        for item in INTEGRATIONS
    )
    body = (
        f'<nav class="crumb" aria-label="Breadcrumb">'
        f'<a href="{"/zh/" if zh else "/"}">{"首页" if zh else "Home"}</a>'
        f'<span>/</span><span>{"生态集成" if zh else "Integrations"}</span>'
        f'<span class="badge">{"更新于" if zh else "Updated"} {UPDATED_DATE}</span>'
        f"</nav>"
        f"<h1>{html.escape(h1)}</h1>"
        f'<p class="lede">{html.escape(lede)}</p>'
        f'<div class="cta-row">'
        f'<a class="pill primary" href="{"/zh/docs/quickstart/" if zh else "/docs/quickstart/"}">'
        f'{"运行 npx veynod" if zh else "Start with npx veynod"}</a>'
        f'<a class="pill" href="{"/zh/vs/" if zh else "/vs/"}">{"查看工具对比" if zh else "Compare tools"}</a>'
        f'<a class="pill" href="/demo/">{"在线演示" if zh else "Live demo"}</a>'
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
                    {"@type": "ListItem", "position": 2, "name": "生态集成" if zh else "Integrations",
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
def render_integration_page(slug: str, lang: str = "en", has_console: bool = True) -> str | None:
    page = INTEGRATIONS_BY_SLUG.get(slug)
    if page is None:
        return None
    zh = lang == "zh"
    en_path = f"/integrations/{slug}/"
    zh_path = f"/zh/integrations/{slug}/"
    canonical = zh_path if zh else en_path
    title = page.title.pick(lang)
    description = page.description.pick(lang)

    steps_html = "".join(f"<li>{html.escape(step.pick(lang))}</li>" for step in page.steps)
    faqs_html = "".join(
        f"<div class='faq-item'><h3>{html.escape(f.question.pick(lang))}</h3>"
        f"<p>{html.escape(f.answer.pick(lang))}</p></div>"
        for f in page.faqs
    )
    related_html = "".join(
        f'<a class="pill" href="{"/zh" if zh else ""}/integrations/{item.slug}/">{html.escape(item.name)}</a>'
        for item in INTEGRATIONS if item.slug != slug
    )
    body = (
        f'<nav class="crumb" aria-label="Breadcrumb">'
        f'<a href="{"/zh/" if zh else "/"}">{"首页" if zh else "Home"}</a>'
        f'<span>/</span><a href="{"/zh/integrations/" if zh else "/integrations/"}">{"生态集成" if zh else "Integrations"}</a>'
        f'<span>/</span><span>{html.escape(page.name)}</span>'
        f'<span class="badge">{"更新于" if zh else "Updated"} {UPDATED_DATE}</span>'
        f"</nav>"
        f"<h1>{html.escape(page.h1.pick(lang))}</h1>"
        f'<p class="lede">{html.escape(page.summary.pick(lang))}</p>'
        f'<div class="cta-row">'
        f'<a class="pill primary" href="{"/zh/docs/quickstart/" if zh else "/docs/quickstart/"}">'
        f'{"运行 npx veynod" if zh else "Start with npx veynod"}</a>'
        f'<a class="pill" href="{"/zh/docs/mcp/" if zh else "/docs/mcp/"}">{"MCP 文档" if zh else "MCP docs"}</a>'
        f'<a class="pill" href="/demo/">{"在线演示" if zh else "Live demo"}</a>'
        f"</div>"
        f'<section class="card-grid">'
        f'<div class="card"><h3>{"一键接入命令" if zh else "One-command setup"}</h3>'
        f"<p><code>{html.escape(page.setup_cmd)}</code></p></div>"
        f'<div class="card"><h3>{"配置文件路径" if zh else "Config location"}</h3>'
        f"<p><code>{html.escape(page.config_path)}</code></p></div>"
        f"</section>"
        f'<section class="term"><div class="term-bar">{html.escape(page.config_label)}</div>'
        f"<pre><code>{html.escape(page.config_snippet)}</code></pre></section>"
        f'<section><h2>{"三步上手" if zh else "3-step setup"}</h2><ol style="padding-left:20px;color:var(--text-2)">{steps_html}</ol></section>'
        f'<section><h2>{"常见问题" if zh else "Frequently asked questions"}</h2>{faqs_html}</section>'
        f'<section><h2>{"更多生态集成" if zh else "More integrations"}</h2>'
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
                    {"@type": "ListItem", "position": 2, "name": "生态集成" if zh else "Integrations",
                     "item": f"{SITE}{'/zh/integrations/' if zh else '/integrations/'}"},
                    {"@type": "ListItem", "position": 3, "name": page.name,
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
