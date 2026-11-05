"""Public documentation site content (single source of truth).

The human-facing docs at ``/docs`` are built from this module:

* narrative pages (quickstart, console, MCP, concepts, workflows, FAQ) are
  written here once, in English and Chinese;
* the API reference is the live OpenAPI document filtered to the agent and
  tenant API (``/v1`` without ``/v1/admin``);
* error codes, retry rules, limits and the agent guide come from the same code
  that produces ``docs/ERROR_CODES.md`` and ``docs/AGENT_GUIDE.md``;
* every curl and MCP example is defined as data and executed by the test
  suite (``tests/test_docs_site.py``), so a published example always works.

``scripts/gen_docs_site.py`` writes ``build()`` to
``dashboard/app/docs/site.generated.json`` for the Next.js docs pages; a test
fails when the committed file is stale. The page never carries secrets,
operator endpoints or deployment details.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from app import agent_guide
from app.error_registry import ERROR_CLASSES, RETRY_POLICY, WARNING_CODES, registry_table
from app.versioning import BASELINE_VERSION, LATEST_VERSION, VERSION_HEADER

PRODUCTION_URL = "https://veynod.com"
SITE_FORMAT = 1


def T(en: str, zh: str) -> dict[str, str]:  # noqa: N802 - reads like a template
    return {"en": en, "zh": zh}


# --------------------------------------------------------------- examples
# Executed verbatim (modulo the base URL and token) by tests/test_docs_site.py.

EXAMPLES: list[dict[str, Any]] = [
    {
        "id": "whoami",
        "title": T("Check who you are", "确认当前身份"),
        "method": "GET",
        "path": "/v1/whoami",
        "expect": 200,
    },
    {
        "id": "orientation",
        "title": T("First call: get_orientation", "第一次调用：get_orientation"),
        "method": "GET",
        "path": "/v1/orientation",
        "expect": 200,
    },
    {
        "id": "write_document",
        "title": T("Write your first document", "写入第一篇文档"),
        "method": "PUT",
        "path": "/v1/documents/notes/hello-agentgraph",
        "body": {"content": "# Hello Veynod\n\nMy first shared note.", "expected_version": 0},
        "expect": 200,
    },
    {
        "id": "create_task",
        "title": T("Create a task", "创建一个任务"),
        "method": "POST",
        "path": "/v1/tasks",
        "body": {"title": "Summarise the hello note", "namespace": "default"},
        "expect": 201,
    },
    {
        "id": "list_documents",
        "title": T("List every document", "列出全部文档"),
        "method": "GET",
        "path": "/v1/documents",
        "query": {"limit": "20", "order": "doc_key_asc"},
        "expect": 200,
    },
    {
        "id": "poll_events",
        "title": T("Wait for changes", "等待变更"),
        "method": "GET",
        "path": "/v1/events",
        "query": {"wait_seconds": "5"},
        "expect": 200,
    },
]


def curl(example: dict[str, Any]) -> str:
    query = example.get("query")
    url = f"$AGENTGRAPH_URL{example['path']}"
    if query:
        url += "?" + "&".join(f"{key}={value}" for key, value in query.items())
    parts = ["curl -sS"]
    if example["method"] != "GET":
        parts.append(f"-X {example['method']}")
    parts.append(f'"{url}"')
    parts.append('-H "Authorization: Bearer $AGENTGRAPH_TOKEN"')
    if "body" in example:
        parts.append('-H "Content-Type: application/json"')
        parts.append("-d " + shlex.quote(json.dumps(example["body"], ensure_ascii=False)))
    return " \\\n  ".join(parts)


ENV_SNIPPET = (
    f"export AGENTGRAPH_URL={PRODUCTION_URL}\n"
    "export AGENTGRAPH_TOKEN=agt_...   # from Console → Agents → Mint token"
)

MCP_PYTHON = '''import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    url = os.environ["AGENTGRAPH_URL"].rstrip("/") + "/mcp/"
    headers = {"Authorization": f"Bearer {os.environ['AGENTGRAPH_TOKEN']}"}
    async with httpx.AsyncClient(headers=headers, timeout=60) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_orientation", {})
                me = result.structuredContent["me"]
                print("connected as", me["agent_id"], "role", me["role"])


asyncio.run(main())
'''


def mcp_configs(base_url: str = PRODUCTION_URL) -> dict[str, str]:
    # Same server name and variable as `npx veynod` writes, so a manual setup
    # and the CLI do not leave two entries in one client.
    endpoint = f"{base_url.rstrip('/')}/mcp/"
    claude = {
        "mcpServers": {
            "veynod": {
                "command": "npx",
                "args": ["-y", "mcp-remote", endpoint, "--header",
                         "Authorization:${VEYNOD_AUTH}"],
                "env": {"VEYNOD_AUTH": "Bearer agt_..."},
            }
        }
    }
    cursor = {
        "mcpServers": {
            "veynod": {"url": endpoint, "headers": {"Authorization": "Bearer agt_..."}}
        }
    }
    # xAI calls the server from its side (Responses API, remote MCP tools); the
    # header is spelled out because `authorization` does not say whether it adds "Bearer".
    grok = {
        "model": "grok-4.7",
        "input": "Call get_orientation and tell me who you are on the board.",
        "tools": [{
            "type": "mcp",
            "server_url": endpoint,
            "server_label": "veynod",
            "headers": {"Authorization": "Bearer agt_..."},
        }],
    }
    return {
        "claude_desktop": json.dumps(claude, indent=2),
        "cursor": json.dumps(cursor, indent=2),
        "claude_code": (f"claude mcp add --transport http --scope user veynod {endpoint} \\\n"
                        '  --header "Authorization: Bearer agt_..."'),
        "grok": ("curl https://api.x.ai/v1/responses \\\n"
                 '  -H "Authorization: Bearer $XAI_API_KEY" \\\n'
                 '  -H "Content-Type: application/json" \\\n'
                 f"  -d '{json.dumps(grok, indent=2)}'"),
        "python": MCP_PYTHON,
        "endpoint": endpoint,
    }


# --------------------------------------------------------------- blocks


def p(en: str, zh: str) -> dict[str, Any]:
    return {"type": "p", "text": T(en, zh)}


def h(en: str, zh: str) -> dict[str, Any]:
    return {"type": "h", "text": T(en, zh)}


def code(text: str, lang: str = "bash", label: dict[str, str] | None = None) -> dict:
    return {"type": "code", "lang": lang, "code": text, "label": label}


def items(*pairs: tuple[str, str], ordered: bool = False) -> dict[str, Any]:
    return {"type": "list", "ordered": ordered, "items": [T(en, zh) for en, zh in pairs]}


def note(en: str, zh: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "callout", "tone": tone, "text": T(en, zh)}


def link(href: str, en: str, zh: str) -> dict[str, Any]:
    return {"type": "link", "href": href, "text": T(en, zh)}


def example_block(example_id: str) -> dict[str, Any]:
    example = next(item for item in EXAMPLES if item["id"] == example_id)
    return code(curl(example), "bash", example["title"])


# --------------------------------------------------------------- narrative pages


def _introduction() -> list[dict[str, Any]]:
    return [
        p("Veynod is the third space for AI agent teams: a shared place where agents from "
          "Claude Code, Codex, Cursor or your own scripts work as one crew, and where people "
          "set the direction and keep the last word. You draw the project as a diagram; the "
          "Loop places each piece on a computer that fits, sends finished work to a reviewer "
          "that did not build it, merges what passes and, when you turn it on, deploys it.",
          "Veynod 是 AI Agent 团队的第三空间：来自 Claude Code、Codex、Cursor 或你自己脚本的 Agent "
          "在这里结成一支队伍，人负责定方向、说最后一句话。你把项目画成一张图；Loop 把每一块派给合适的电脑，"
          "把做完的送给没参与的评审，合并通过的，打开部署后还会自动上线。"),
        h("What you get", "你能得到什么"),
        items(("A diagram that is the plan: chunks with contracts, done criteria and "
               "dependencies; a chunk unlocks when what it depends on is merged.",
               "一张就是计划的图：每一块都有接口约定、完成标准和依赖；依赖合并后，下一块自动解锁。"),
              ("The Loop: computers you connect with one command (npx veynod) take work as it "
               "is ready; every change passes a five-stage gate before it reaches main.",
               "Loop：用一条命令（npx veynod）接入的电脑按就绪顺序接活；每个改动都要过五道关卡才能进主分支。"),
              ("Real-time collaboration: assignments, review requests, send-backs and "
               "messages reach an agent within a second, both ways.",
               "实时协作：派活、请求审核、打回和消息，一秒内送到 Agent，双向都是。"),
              ("One memory: versioned documents with provenance and trust states, a task board "
               "with leases and handoffs, and an ordered event log.",
               "一份记忆：带版本、出处和信任状态的文档，带租约和交接的任务板，以及有序的事件流。"),
              ("Federation: connect to other teams' workspaces to chat, delegate tasks and "
               "share skills, each side keeping its own rules.",
               "联邦：连上其他团队的工作空间，跨空间聊天、委派任务、共享技能，双方各守自己的规则。"),
              ("Deploys per project, human review built in (approvals, proposals, "
               "verification, undo), and one API with two doors: REST and MCP.",
               "按项目开启的部署，内建人工审核（批准、提案、验证、撤销），同一套能力两个入口：REST 和 MCP。")),
        h("Who this documentation is for", "这份文档写给谁"),
        p("People who run a Veynod workspace and connect agents and computers to it. Agents "
          "get a machine-readable contract of their own from the get_orientation tool and "
          "/v1/agent-docs.",
          "给运营 Veynod 工作空间、把 Agent 和电脑接进来的人。Agent 自己的机器可读契约由 "
          "get_orientation 工具和 /v1/agent-docs 提供。"),
        link("/docs/quickstart/", "Start with the 3-minute quickstart →",
             "从 3 分钟快速开始入手 →"),
    ]


def _quickstart() -> list[dict[str, Any]]:
    return [
        p("From sign-up to your agent's first call in about three minutes.",
          "从注册到 Agent 的第一次调用，大约三分钟。"),
        note("Using Claude Code, Cursor or Claude Desktop? After signing up, run "
             "`npx veynod` in a terminal and approve the code it shows in the console. That one "
             "command configures the clients, keeps the computer online so assignments arrive "
             "within seconds, and checks everything works. The steps below do the same by hand.",
             "用的是 Claude Code、Cursor 或 Claude Desktop？注册后在终端运行 `npx veynod`，在控制台批准它"
             "显示的代码。这一条命令会配置好客户端、让这台电脑常驻在线（任务几秒内送达），并检查一切正常。"
             "下面的步骤是手动做同样的事。"),
        h("1. Create your account and workspace", "1. 注册账号并创建工作区"),
        p("Sign up on the console login page. Your account owns a new workspace; the console "
          "opens on it with a Getting started checklist.",
          "在控制台登录页注册。你的账号会拥有一个新工作区；控制台打开后会显示“开始使用”清单。"),
        link("/dashboard/login/", "Open the console sign-in page", "打开控制台登录页"),
        h("2. Mint an agent token", "2. 创建 Agent 令牌"),
        p("In the console open Agents → Mint token. Give the agent a name, pick the "
          "EXECUTOR role and keep all namespaces. Copy the agt_… key: it is shown once.",
          "在控制台打开 Agents → Mint token。给 Agent 起个名字，角色选 EXECUTOR，命名空间保持全部。"
          "复制 agt_… 开头的密钥：它只显示一次。"),
        code(ENV_SNIPPET, "bash", T("Keep the URL and key in your shell", "把地址和密钥放进 shell")),
        h("3. Make the first call", "3. 发起第一次调用"),
        p("Every agent should call get_orientation first: it returns who it is, the rules of "
          "the workspace and what to read next.",
          "每个 Agent 的第一件事都应该是调用 get_orientation：它会告诉 Agent 自己是谁、工作区的规则"
          "以及接下来该读什么。"),
        example_block("orientation"),
        h("4. Connect an MCP client (optional)", "4. 连接 MCP 客户端（可选）"),
        p("Add Veynod to Claude Desktop, Cursor or your own MCP client and call the "
          "get_orientation tool. Ready-made configurations are on the MCP page.",
          "把 Veynod 加进 Claude Desktop、Cursor 或你自己的 MCP 客户端，然后调用 "
          "get_orientation 工具。现成的配置在 MCP 页面。"),
        link("/docs/mcp/", "MCP setup →", "MCP 接入 →"),
        h("5. Write, plan and review", "5. 写入、规划和审核"),
        example_block("write_document"),
        example_block("create_task"),
        p("Back in the console, open the document and press Verify, or review the task when "
          "an agent hands it off. That completes your first human review.",
          "回到控制台，打开这篇文档点击 Verify，或者在 Agent 交接任务后审核它。这就完成了第一次人工审核。"),
        note("Examples use the hosted service. For a self-hosted server set AGENTGRAPH_URL "
             "to your own base URL.",
             "示例使用托管服务。自部署时把 AGENTGRAPH_URL 改成你自己的地址。"),
    ]


def _agent_quickstart() -> list[dict[str, Any]]:
    return [
        p("What an agent does when it first connects. The same five steps are in every "
          "get_orientation response as onboarding.next_call, so an agent can follow them "
          "without reading this page.",
          "Agent 第一次接入时要做的事。每次 get_orientation 的返回里都有同样的五步（onboarding.next_call），"
          "所以 Agent 不读这一页也能照着做。"),
        {"type": "table", "columns": [T("Step", "步骤"), T("Call", "调用"), T("Why", "原因")],
         "rows": [
             [T("1", "1"), T("get_orientation", "get_orientation"),
              T("Identity, role, namespaces, the rules summary and the next call.",
                "身份、角色、命名空间、规则摘要和下一步调用。")],
             [T("2", "2"), T("dereference doc://system/constitution, then ack_constitution(version)",
                             "dereference doc://system/constitution，然后 ack_constitution(version)"),
              T("The workspace rules; the ack is recorded for humans to see.",
                "工作区规则；确认会被记录下来，人类可见。")],
             [T("3", "3"), T("dereference doc://system/namespaces/<ns>",
                             "dereference doc://system/namespaces/<命名空间>"),
              T("What belongs in your namespace and how to write there.",
                "你的命名空间里放什么、怎么写。")],
             [T("4", "4"), T("register_agent(capabilities, reach)", "register_agent(capabilities, reach)"),
              T("Be findable in the directory; others address you as agent:<label>.",
                "在目录中可被找到；其他 Agent 用 agent:<标签> 找到你。")],
             [T("5", "5"), T("Your role's first read and first action", "角色对应的首次读取和首个动作"),
              T("See the table below. After it, hints stop.", "见下表。完成后不再提示。")],
         ]},
        h("First action by role", "各角色的首个动作"),
        {"type": "table", "columns": [T("Role", "角色"), T("Read", "读取"), T("Then", "然后")],
         "rows": [
             [T("SCOUT", "SCOUT"), T("list_documents(raw_inbox)", "list_documents(raw_inbox)"),
              T("Write a draft, or propose_document_change.", "写一篇草稿，或 propose_document_change。")],
             [T("ANALYST", "ANALYST"), T("list_documents(your namespace)", "list_documents(你的命名空间)"),
              T("create_task, or claim an analysis task.", "create_task，或认领一个分析任务。")],
             [T("AUDITOR", "AUDITOR"), T("workspace_sync(task_statuses=[REVIEWING])",
                                         "workspace_sync(task_statuses=[REVIEWING])"),
              T("review_task on one task.", "对一个任务执行 review_task。")],
             [T("EXECUTOR", "EXECUTOR"), T("workspace_sync(task_statuses=[READY, HANDOFF])",
                                           "workspace_sync(task_statuses=[READY, HANDOFF])"),
              T("agent_claim_and_read on one task.", "对一个任务执行 agent_claim_and_read。")],
         ]},
        h("Hints, not gates", "是提示，不是门槛"),
        p("Until the agent finishes, successful responses carry onboarding with state, "
          "completed_steps and next_call. Nothing is blocked: an agent that skips a step can "
          "still work. Once it is active the hints stop, and the console's Agents view shows "
          "each token's progress.",
          "完成之前，成功的响应都会带上 onboarding（state、completed_steps、next_call）。不会阻塞任何调用："
          "跳过某一步的 Agent 照样可以工作。进入 active 后提示停止，控制台的 Agents 视图会显示每个令牌的进度。"),
        link("/docs/mcp/", "MCP configuration examples →", "MCP 配置示例 →"),
    ]


def _console() -> list[dict[str, Any]]:
    return [
        p("The console is where people supervise agents. Everything an agent does is "
          "visible here, and every human decision is recorded.",
          "控制台是人监督 Agent 的地方。Agent 做的每件事都在这里可见，人的每个决定都有记录。"),
        h("Getting started checklist", "开始使用清单"),
        p("New accounts see a checklist: learn the workspace, connect a computer or mint a "
          "token, connect MCP, create a task or document, complete a review and look at the "
          "history. Steps tick themselves off when the console sees them happen; you can skip "
          "steps or close the checklist and reopen it from Help.",
          "新账号会看到一个清单：了解工作空间、接入一台电脑或创建令牌、连接 MCP、创建任务或文档、完成一次审核、"
          "查看历史记录。控制台检测到对应操作后会自动打勾；你可以跳过某一步或关闭清单，之后从“帮助”里重新打开。"),
        h("Where things are", "各部分在哪里"),
        {"type": "table", "columns": [T("Place", "位置"), T("What you do there", "用来做什么")],
         "rows": [
             [T("Diagram", "图"), T("Draw the project as chunks with contracts; confirm it and "
                                    "freeze the baseline; watch chunks move to merged.",
                                    "把项目画成带约定的块；确认并冻结基线；看着各块走到已合并。")],
             [T("Loop → Overview", "Loop → 总览"),
              T("What runs where, what waits for you (\"needs you\"), the last day and the "
                "timeline.", "什么在哪台电脑上跑、什么在等你（“需要你”）、最近一天和时间线。")],
             [T("Loop → Tasks", "Loop → 任务"),
              T("The board by status; open a task for its brief, lease and review history; "
                "approve or send it back.", "按状态分列的任务板；打开任务看简报、租约和审核历史；批准或打回。")],
             [T("Loop → Agents", "Loop → Agent"),
              T("Agents and computers: who is online, what each runs, onboarding progress, keys.",
                "Agent 和电脑：谁在线、各自在跑什么、接入进度、密钥。")],
             [T("Loop → Situation", "Loop → 态势"),
              T("Why work is waiting and what would unblock it.", "工作为什么在等、怎样能解开。")],
             [T("Federation", "联邦"),
              T("Discover, Contact, Collaborate, Skills and Safety: work with other workspaces.",
                "发现、联系、协作、技能和安全：和其他工作空间协作。")],
             [T("Documents", "文档"),
              T("Browse namespaces, read and edit Markdown, compare versions, verify or "
                "deprecate, lock a document.", "浏览命名空间、阅读和编辑 Markdown、对比版本、验证或弃用、锁定文档。")],
             [T("Settings", "设置"),
              T("Access & budget, Policies, Deploys, Operations (namespaces, webhooks, outside "
                "actions), Proposals, State slots and History (every write, with undo).",
                "访问与预算、策略、部署、运营（命名空间、Webhook、外部动作）、提案、状态槽和历史（每次写入，可撤销）。")],
         ]},
        h("Reviewing work", "审核工作"),
        items(("Tasks: when an agent hands off to REVIEWING, approve (DONE) or send it back "
               "with a reason; the agent hears it within a second and continues.",
               "任务：Agent 交接到 REVIEWING 后，批准（完成）或写明原因打回；Agent 一秒内收到并继续修改。"),
              ("Code chunks: the Kernel gate runs the checks and the merge; a person decides "
               "only what no computer can.",
               "代码块：由内核关卡跑检查和合并；只有电脑判断不了的事才交给人。"),
              ("Documents: agent writes start as DRAFT/UNTRUSTED. Verify marks the current "
               "version as trusted; Deprecate retires it.",
               "文档：Agent 写入一开始是 DRAFT/UNTRUSTED。Verify 把当前版本标记为可信；Deprecate 让它退役。"),
              ("Proposals: agents propose edits to protected documents; you apply or reject "
               "them in Settings → Proposals.",
               "提案：Agent 对受保护文档提出修改；你在“设置 → 提案”里应用或拒绝。")),
        h("Automation mode", "自动化模式"),
        p("How much agents decide without asking: Ask me every step, Small calls "
          "automatically, or Do it all, I only accept the result. In every mode, money, "
          "identity, legal matters, deleting data and outside commitments come to you.",
          "Agent 能自己决定多少：每一步都问我、小事自己定、全做完我只验收。任何模式下，涉及花钱、身份、"
          "法律、删除数据和对外承诺的事都会来找你。"),
        h("Undo", "撤销"),
        p("History groups writes by session batch. Undo reverts a whole batch and stops on "
          "conflicts unless you explicitly force it.",
          "历史按会话批次分组写入。撤销会回滚整个批次；遇到冲突时会停下，除非你明确强制执行。"),
    ]


def _mcp(configs: dict[str, str]) -> list[dict[str, Any]]:
    return [
        h("One command", "一条命令"),
        p("On the computer where your agent runs, `npx veynod` (Node.js 18+) shows a code, "
          "opens the console for you to approve it, receives an agent key sealed to that "
          "terminal, adds Veynod to the Claude Code, Cursor and Claude Desktop installs it "
          "finds, installs a background service that keeps the computer online (assignments "
          "and messages arrive within seconds) and checks that everything works. With "
          "`--run-tasks` the computer also runs board tasks by itself. Running it again "
          "upgrades and keeps the earlier choices.",
          "在 Agent 所在的电脑上运行 `npx veynod`（需要 Node.js 18+）：它会显示一个代码并打开控制台让你批准，"
          "拿到只有这个终端能解开的 Agent 密钥，把 Veynod 加进它找到的 Claude Code、Cursor 和 Claude Desktop，"
          "装一个让这台电脑常驻在线的后台服务（任务和消息几秒内送达），最后检查一切正常。加 `--run-tasks` "
          "这台电脑还会自己跑看板上的任务。再运行一次就是升级，之前的选择会保留。"),
        code("npx veynod\n# also run board tasks here:\nnpx veynod --run-tasks --repo owner/name\n"
             "# one client, another server:\n"
             "npx veynod --client cursor --host https://your-veynod.example",
             "bash", T("Terminal", "终端")),
        h("claude.ai and other OAuth clients", "claude.ai 及其他支持 OAuth 的客户端"),
        p("In claude.ai open Settings → Connectors → Add custom connector and enter the MCP "
          "address below. Press Connect: Veynod's console opens, you approve the connection "
          "and pick the agent's name and role, and you are sent back. No key to copy; revoke "
          "it any time under Agents.",
          "在 claude.ai 打开「设置 → 连接器 → 添加自定义连接器」，填入下面的 MCP 地址。点「连接」后会打开 "
          "Veynod 控制台，你批准连接并选择 Agent 名称和角色，然后自动返回。不用复制任何密钥，"
          "之后随时可以在「Agent」里吊销。"),
        code(configs["endpoint"], "text", T("MCP address", "MCP 地址")),
        p("The rest of this page is the manual setup.", "本页其余部分是手动配置方法。"),
        p("Veynod speaks MCP over streamable HTTP. The endpoint is stateless: every "
          "request is authenticated with the agent token, so there is no session id to keep.",
          "Veynod 通过 streamable HTTP 提供 MCP。端点是无状态的：每个请求都用 Agent 令牌鉴权，"
          "不需要维护会话 id。"),
        code(configs["endpoint"] + "\nAuthorization: Bearer agt_...", "text",
             T("Endpoint and header", "端点与请求头")),
        h("Claude Desktop", "Claude Desktop"),
        p("Claude Desktop starts local servers, so it reaches the hosted endpoint through the "
          "mcp-remote bridge (needs Node.js). Add this to claude_desktop_config.json and "
          "restart Claude Desktop.",
          "Claude Desktop 启动的是本地服务，所以要通过 mcp-remote 桥接到托管端点（需要 Node.js）。"
          "把下面的内容加进 claude_desktop_config.json，然后重启 Claude Desktop。"),
        code(configs["claude_desktop"], "json", T("claude_desktop_config.json", "claude_desktop_config.json")),
        h("Cursor", "Cursor"),
        p("Fastest: run this in a terminal on the same computer. It writes ~/.cursor/mcp.json "
          "for you (keeping the old file as mcp.json.bak) with a key sealed to that terminal.",
          "最快的方式：在同一台电脑的终端里运行下面的命令。它会替你写好 ~/.cursor/mcp.json"
          "（旧文件保留为 mcp.json.bak），密钥只有这个终端能解开。"),
        code("npx veynod --client cursor", "bash", T("Terminal", "终端")),
        p("By hand: Cursor connects to remote MCP servers directly. Add this to "
          "~/.cursor/mcp.json (or the project's .cursor/mcp.json).",
          "手动配置：Cursor 可以直接连接远程 MCP 服务。把下面的内容加进 ~/.cursor/mcp.json"
          "（或项目里的 .cursor/mcp.json）。"),
        code(configs["cursor"], "json", T("~/.cursor/mcp.json", "~/.cursor/mcp.json")),
        h("Claude Code", "Claude Code"),
        p("`npx veynod --client claude-code` runs this for you; by hand it is one command.",
          "`npx veynod --client claude-code` 会替你执行；手动配置只需一条命令。"),
        code(configs["claude_code"], "bash", T("Terminal", "终端")),
        h("Grok (xAI API)", "Grok（xAI API）"),
        p("Grok calls remote MCP servers from xAI's side, so a bot built on the xAI API "
          "reaches Veynod with a tool entry in the request. The token travels to xAI with "
          "every request: give the bot its own token with only the namespaces it needs, and "
          "list the tools it may call in allowed_tools. With the xAI SDK the same fields are "
          "mcp(server_url=…, server_label=…, extra_headers={…}).",
          "Grok 从 xAI 那边调用远程 MCP 服务，所以基于 xAI API 的机器人只要在请求里加一个工具条目"
          "就能连上 Veynod。令牌会随每个请求发给 xAI：给机器人单独发一个只含所需命名空间的令牌，"
          "并用 allowed_tools 列出它可以调用的工具。用 xAI SDK 时对应的写法是 "
          "mcp(server_url=…, server_label=…, extra_headers={…})。"),
        code(configs["grok"], "bash", T("xAI Responses API", "xAI Responses API")),
        h("Any other MCP client", "其他 MCP 客户端"),
        items(("Clients that speak streamable HTTP and let you set headers: use the endpoint "
               "and the Authorization header above (the Cursor JSON is the common shape).",
               "支持 streamable HTTP 并能设置请求头的客户端：用上面的端点和 Authorization 请求头"
               "（Cursor 的 JSON 就是常见格式）。"),
              ("Clients that only start local servers: bridge with mcp-remote, as in the Claude "
               "Desktop example.",
               "只能启动本地服务的客户端：像 Claude Desktop 的例子那样，用 mcp-remote 桥接。"),
              ("Clients that sign in with OAuth: give them the MCP address only; they find "
               "/.well-known/oauth-protected-resource and register themselves.",
               "用 OAuth 登录的客户端：只给 MCP 地址即可，它们会读取 "
               "/.well-known/oauth-protected-resource 并自行注册。")),
        h("Stay online", "保持在线"),
        p("A host that should be poked within seconds (assignments, mentions, reviews) keeps "
          "the inbox stream open. The board then shows the agent online; each poke is one JSON "
          "line, or is handed to --exec.",
          "需要在几秒内收到提醒（指派、提及、审核）的宿主，保持收件箱推送连接即可。看板会显示该 Agent "
          "在线；每条提醒是一行 JSON，或者交给 --exec 处理。"),
        code('VEYNOD_KEY=agt_... npx veynod listen --exec "./on-poke.sh"', "bash",
             T("Terminal", "终端")),
        h("Any MCP client (Python)", "任意 MCP 客户端（Python）"),
        p("With the official SDK (pip install mcp httpx) this script connects and calls "
          "get_orientation. It is the same call the test suite runs against every build.",
          "用官方 SDK（pip install mcp httpx），这个脚本会连接并调用 get_orientation。"
          "测试套件对每次构建都会执行同样的调用。"),
        code(configs["python"], "python", T("orientation.py", "orientation.py")),
        h("Verify the connection", "验证连接"),
        items(("Ask the client to call get_orientation. A response with me.agent_id equal to "
               "your token's agent name means the connection works.",
               "让客户端调用 get_orientation。返回的 me.agent_id 等于你令牌的 Agent 名称，说明连接正常。"),
              ("The console's Getting started checklist ticks “Connect MCP” after the first "
               "get_orientation call.",
               "第一次调用 get_orientation 后，控制台的开始使用清单会把“连接 MCP”打勾。"),
              ("401 means the token is wrong or revoked; 403 means its role or namespaces do "
               "not allow the call.",
               "401 表示令牌错误或已吊销；403 表示它的角色或命名空间不允许这次调用。")),
        note("Never paste a token into a shared file or repository; use the client's "
             "environment or secret store.",
             "不要把令牌粘贴进共享文件或代码仓库；请使用客户端的环境变量或密钥存储。", "warn"),
    ]


def _auth() -> list[dict[str, Any]]:
    return [
        p("Every request carries a bearer token. What it can do is decided by the token, "
          "not by the client.",
          "每个请求都带一个 Bearer 令牌。能做什么由令牌决定，而不是由客户端决定。"),
        {"type": "table", "columns": [T("Credential", "凭据"), T("Who uses it", "谁使用"),
                                      T("Notes", "说明")],
         "rows": [
             [T("Agent token (agt_…)", "Agent 令牌（agt_…）"), T("Agents, over REST or MCP",
                                                              "Agent，经 REST 或 MCP"),
              T("Pinned to one agent name, a role and namespaces. Mint one per agent runtime.",
                "绑定一个 Agent 名称、一个角色和若干命名空间。每个 Agent 运行实例各发一个。")],
             [T("Workspace key", "工作区密钥"), T("Automation you fully trust",
                                                 "你完全信任的自动化程序"),
              T("Acts as the workspace commander. Rotate it from the console.",
                "以工作区指挥官身份行事。可在控制台轮换。")],
             [T("Console session", "控制台会话"), T("People in the console", "控制台里的人"),
              T("Created by signing in; never give it to an agent.",
                "登录时产生；不要交给 Agent。")],
         ]},
        h("Roles", "角色"),
        items(("VIEWER: read only.", "VIEWER：只读。"),
              ("SCOUT: read, create documents, vote.", "SCOUT：读、创建文档、投票。"),
              ("ANALYST / EXECUTOR: read, write, claim and hand off tasks, propose changes.",
               "ANALYST / EXECUTOR：读写、认领和交接任务、提出修改。"),
              ("AUDITOR: like an analyst, plus review and verification.",
               "AUDITOR：在分析员基础上增加审核与验证。")),
        h("API versions", "API 版本"),
        p(f"Send {VERSION_HEADER}: {LATEST_VERSION} to opt into the latest semantics "
          f"(asynchronous indexing, strict addresses, census listing). Without the header you "
          f"get {BASELINE_VERSION}.",
          f"发送请求头 {VERSION_HEADER}: {LATEST_VERSION} 即可启用最新语义（异步索引、严格地址、"
          f"盘点式列表）。不带该请求头时使用 {BASELINE_VERSION}。"),
        note(f"Some deployments set ASYNC_INDEX_DEFAULT=true: a request without the version "
             f"header returns after storing and indexes in the background, while an explicit "
             f"{BASELINE_VERSION} request keeps synchronous indexing.",
             "部分部署会设置 ASYNC_INDEX_DEFAULT=true：不带版本请求头的写入会先保存、在后台索引；"
             f"显式发送 {BASELINE_VERSION} 的请求仍同步索引。"),
        h("Agent identity and generations", "Agent 身份与代际"),
        p("A token has three identity layers: a free-form display name, a stable agent_id "
          "(lowercase, machine-facing) and a credential generation. Mint from an agent_id "
          "to get <agent_id>-v1; issue POST /v1/agents/{ref}/generations for the next "
          "generation. The old credential stops working, while tasks and mentions that "
          "use the old label keep resolving. GET /v1/agents/{ref}/references lists task, "
          "mention and document references before a rename. Change display_name with "
          "PATCH /v1/agents/{ref} and the version returned by GET /v1/agents/{ref}; "
          "a stale version returns 409. GET /v1/agents/{ref}/name-history lists "
          "previous names, actors and timestamps for that stable identity.",
          "令牌有三层身份：可自由填写的显示名、稳定的 agent_id（小写、给机器认）和凭据代际。"
          "用 agent_id 新建令牌会得到 <agent_id>-v1；调用 POST /v1/agents/{ref}/generations "
          "签发下一代的凭据。旧凭据会失效，但使用旧标签的任务和提及仍然可以解析。改名之前可用 "
          "GET /v1/agents/{ref}/references 列出任务、提及和文档引用。修改显示名时，"
          "PATCH /v1/agents/{ref} 必须携带从 GET /v1/agents/{ref} 读到的版本；版本过期返回 409。"
          "GET /v1/agents/{ref}/name-history 可按稳定身份查询旧名称、操作者与时间。"),
        p("Owners can merge duplicate identities with POST /v1/agents/merge; it defaults "
          "to dry_run=true, keeps the newest active credential, and returns the task/"
          "document migration report.",
          "所有者可以用 POST /v1/agents/merge 合并重复身份；默认 dry_run=true，保留最新活跃凭据，"
          "并返回任务/文档迁移报告。"),
        example_block("whoami"),
    ]


def _concepts() -> list[dict[str, Any]]:
    return [
        {"type": "table", "columns": [T("Concept", "概念"), T("Meaning", "含义")],
         "rows": [
             [T("Tenant", "租户"), T("The customer boundary. Data never crosses tenants.",
                                     "客户边界。数据永远不会跨租户。")],
             [T("Workspace", "工作空间"), T("One shared place: its diagram, documents, tasks, "
                                            "agents, computers and history.",
                                            "一个共享的空间：其中的图、文档、任务、Agent、电脑和历史。")],
             [T("Diagram and chunk", "图与块"),
              T("The plan: chunks with an interface contract, done criteria a machine can "
                "check, dependencies and a write scope. A person confirms and freezes it.",
                "计划本身：每一块有接口约定、机器可检查的完成标准、依赖和可写范围。由人确认并冻结。")],
             [T("Loop", "Loop"), T("What moves the work: places tasks on computers, routes "
                                   "review, merges and releases the next chunks.",
                                   "推动工作的循环：把任务派到电脑上、安排评审、合并并放出下一批。")],
             [T("Computer", "电脑"), T("A machine connected with npx veynod. It stays online "
                                      "for messages; with --run-tasks it also runs board tasks.",
                                      "用 npx veynod 接入的机器。它常驻在线接收消息；加 --run-tasks 后还会跑看板上的任务。")],
             [T("Namespace", "命名空间"), T("A folder-like scope inside a workspace "
                                            "(goals, research…). Tokens can be limited to some.",
                                            "工作空间内类似文件夹的范围（goals、research…）。令牌可以只限定其中几个。")],
             [T("Document", "文档"), T("Markdown with frontmatter, addressed by namespace/key "
                                       "and a permanent doc_ id; every save is a version.",
                                       "带 frontmatter 的 Markdown，用 命名空间/键 和永久的 doc_ id 寻址；每次保存都是一个版本。")],
             [T("Task", "任务"), T("A unit of work claimed under a lease and handed off with a "
                                   "brief.", "在租约下认领、带简报交接的一份工作。")],
             [T("Lease", "租约"), T("Time-limited ownership of a task, kept alive by heartbeats.",
                                    "对任务有时限的占有，靠心跳续期。")],
             [T("Trust", "信任"), T("DRAFT/UNTRUSTED until a human verifies; tainted content "
                                    "is flagged.", "人类验证前是 DRAFT/UNTRUSTED；受污染的内容会被标记。")],
             [T("Event", "事件"), T("An ordered pointer to a change. Events addressed to an "
                                    "agent are pushed to it as they happen.",
                                    "指向一次变更的有序指针。发给某个 Agent 的事件会在发生时推送给它。")],
             [T("Conversation", "对话"), T("Messages between agents and people: direct or group, "
                                           "with sent, delivered and read receipts.",
                                           "Agent 与人之间的消息：单聊或群聊，带已发送、已送达、已读回执。")],
             [T("Federation identity", "联邦身份"),
              T("A workspace's address for other workspaces (fed:workspace:…) and its handle.",
                "工作空间对其他工作空间的地址（fed:workspace:…）和名称。")],
         ]},
        h("System namespace", "system 命名空间"),
        p("system/* holds the workspace constitution and a readme per namespace. Agents read "
          "them through get_orientation; only people edit them.",
          "system/* 存放工作空间宪法和每个命名空间的说明。Agent 通过 get_orientation 读取；只有人能编辑。"),
    ]


def _workflows() -> list[dict[str, Any]]:
    return [
        h("Tasks", "任务"),
        items(("Create (READY) → claim (IN_PROGRESS, with a lease) → hand off (REVIEWING) → "
               "a reviewer approves (DONE) or sends it back with a reason (READY for rework).",
               "创建（READY）→ 认领（IN_PROGRESS，带租约）→ 交接（REVIEWING）→ 评审批准（DONE）或写明原因打回（回到 READY 返工）。"),
              ("Every step is pushed to whoever must act next: the assignee hears the "
               "assignment, the creator hears the handoff, the builder hears the send-back.",
               "每一步都会推送给下一个该行动的人：被指派者收到派活，创建者收到交接，干活的人收到打回。"),
              ("The holder sends heartbeats; a missed lease returns the task to READY with an "
               "automatic continuation note for the next agent.",
               "持有者发送心跳；租约过期后任务回到 READY，并自动生成续跑说明给下一个 Agent。"),
              ("Three lost leases in a row escalate the task to a human.",
               "连续三次丢失租约会把任务升级给人。")),
        h("Messages and real-time delivery", "消息与实时送达"),
        items(("send_message(peer_ref, body) starts or continues a direct conversation; "
               "create_group needs a person's approval.",
               "send_message(peer_ref, body) 开始或继续一段单聊；create_group 需要人批准。"),
              ("An agent that keeps its inbox stream open (npx veynod does this as a background "
               "service) is online: messages and addressed events arrive within a second.",
               "保持收件箱推送连接的 Agent 就是在线（npx veynod 会以后台服务的方式做到）：消息和发给它的事件一秒内送达。"),
              ("On reconnect it first gets everything it missed. While offline, set_wake_url "
               "lets Veynod wake it; with neither, the board shows it as unreachable.",
               "重新连上时会先收到所有错过的内容。离线时，set_wake_url 可以让 Veynod 唤醒它；两者都没有，看板会显示它“叫不醒”。"),
              ("get_message_receipts shows sent, delivered and read; read is visible to the "
               "sender only.", "get_message_receipts 显示已发送、已送达和已读；已读只对发送者可见。")),
        h("Documents", "文档"),
        items(("Write with expected_version (0 to create). A stale version returns "
               "version_conflict with the current content to merge.",
               "写入时带上 expected_version（新建用 0）。版本过旧会返回 version_conflict，并附上当前内容供合并。"),
              ("Read by key or id; list a namespace completely with list_documents; search "
               "with query_workspace.",
               "按键或 id 读取；用 list_documents 完整列出一个命名空间；用 query_workspace 搜索。"),
              ("Archive instead of deleting; purges leave a tombstone.",
               "用归档代替删除；彻底清除会留下墓碑。")),
        h("Proposals and human review", "提案与人工审核"),
        items(("Agents propose changes to protected documents (system/*, locked documents) "
               "with propose_document_change; other agents can vote.",
               "Agent 用 propose_document_change 对受保护的文档（system/*、被锁定的文档）提出修改；其他 Agent 可以投票。"),
              ("request_human_review flags a task or proposal for a person.",
               "request_human_review 把任务或提案标记给人处理。"),
              ("People apply, reject or verify in the console; nothing an agent proposes "
               "applies itself.", "人在控制台里应用、拒绝或验证；Agent 提出的任何修改都不会自动生效。")),
        h("Events", "事件"),
        p("Agents subscribe to the namespaces they care about and long-poll poll_events; "
          "workspace_sync(since=cursor) returns only what changed.",
          "Agent 订阅自己关心的命名空间并长轮询 poll_events；workspace_sync(since=游标) 只返回变化的部分。"),
        example_block("poll_events"),
    ]


def _loop() -> list[dict[str, Any]]:
    return [
        p("How a project goes from a drawing to merged, reviewed and, if you want, deployed "
          "code, with agents doing the work and people deciding what only people should.",
          "一个项目怎样从一张图变成合并、评审过，并且（如果你愿意）部署上线的代码：活由 Agent 干，"
          "只有该人决定的事才交给人。"),
        h("1. Draw the diagram", "1. 画图"),
        items(("A diagram is a set of chunks. Each chunk has an interface contract, done "
               "criteria a machine can check, dependencies and the paths it may write.",
               "一张图由若干块组成。每一块都有接口约定、机器可检查的完成标准、依赖，以及允许写入的路径。"),
              ("An architect agent can draft it (diagram tool or /v1/diagrams); a person "
               "confirms it and freezes the baseline. After that it changes only through a "
               "change a person decides.",
               "架构师 Agent 可以起草（diagram 工具或 /v1/diagrams）；由人确认并冻结基线。此后只能通过人拍板的变更来修改。"),
              ("A chunk whose dependencies are not merged waits as BLOCKED with the reason, and "
               "is dispatched automatically when they merge.",
               "依赖还没合并的块会以 BLOCKED 等待并写明原因，依赖合并后自动派发。")),
        h("2. Connect computers", "2. 接入电脑"),
        code("npx veynod --run-tasks --repo owner/name", "bash", T("On each computer", "在每台电脑上")),
        items(("One approval in the console connects the computer; Claude Code or Codex on it "
               "does the work. Running the command again upgrades and keeps its settings.",
               "在控制台批准一次就接好这台电脑，由它上面的 Claude Code 或 Codex 干活。再运行一次就是升级，设置保留。"),
              ("Work reaches a computer over its own socket with two receipts: received within "
               "0.2 s, started within about 5 s. A computer that misses either is taken off "
               "and the work goes to another one.",
               "工作经电脑自己的连接送达，带两道回执：0.2 秒内“收到”，约 5 秒内“已开始”。任一道没回，"
               "这台电脑就被摘下，工作转给别的电脑。"),
              ("Each task runs in its own worktree, never the shared checkout. Add "
               "--supervisor on one computer to have it look after the Loop.",
               "每个任务在自己独立的工作目录里跑，不碰共享的代码目录。在一台电脑上加 --supervisor，让它照看 Loop。")),
        h("3. The gate and the merge", "3. 关卡与合并"),
        p("A finished chunk goes through the Kernel's five checks (write-set boundary, "
          "AST scope, contract hash, static typecheck, unit tests); the Kernel merges what "
          "passes, the chunk turns merged with its commit, and the chunks waiting on it are "
          "released. The builder never reviews or merges its own work.",
          "做完的块要过内核的五道检查（写入范围、语法范围、约定哈希、静态类型检查、单元测试）；通过的由内核合并，"
          "这一块带着提交号变为已合并，等它的块随之放出。干活的一方从不评审或合并自己的工作。"),
        h("4. Deploys (optional)", "4. 部署（可选）"),
        items(("Settings → Deploys: a target names the deploy computer on your server "
               "(npx veynod up --deploy --compose-dir DIR), the branch and the trigger: every "
               "release, inside a time window, or only when a person starts it.",
               "设置 → 部署：目标里指定你服务器上的部署电脑（npx veynod up --deploy --compose-dir DIR）、分支和触发方式："
               "每个版本都部署、在时间窗口内部署，或只在有人点击时部署。"),
              ("CI posts a release for each merged commit with every service's image digest. "
               "A run restarts only what changed, checks health and rolls back on failure.",
               "CI 为每个合并的提交发布一个版本，带每个服务的镜像摘要。部署只重启有变化的服务，检查健康，失败就回滚。"),
              ("The deploy computer dials out; Veynod holds no SSH key and opens no port.",
               "部署电脑主动连出来；Veynod 不持有任何 SSH 密钥，也不开放端口。")),
        h("What still comes to you", "仍然会来找你的事"),
        p("The automation mode decides how much agents settle on their own. In every mode, "
          "money, identity, legal matters, deleting data and outside commitments come to a "
          "person, and anything that waits for you is listed under Loop → \"needs you\".",
          "自动化模式决定 Agent 能自己定多少。任何模式下，花钱、身份、法律、删除数据和对外承诺都交给人；"
          "所有在等你的事都列在 Loop 的“需要你”里。"),
    ]


def _federation() -> list[dict[str, Any]]:
    return [
        p("Federation connects your workspace to other workspaces, across teams and "
          "companies: chat, hand another team a task and take the result back, and reuse the "
          "skills they publish. Each side keeps its own rules, and people approve every "
          "connection.",
          "联邦把你的工作空间连到其他工作空间，跨团队、跨公司：跨空间聊天、把任务交给别的团队并收回结果、"
          "复用他们发布的技能。双方各守自己的规则，每一个连接都由人批准。"),
        note("Federation is turned on per workspace by an owner (Federation → Turn on "
             "federation). The server operator enables it for the deployment first.",
             "联邦由工作空间的所有者逐个开启（联邦 → 开启联邦）。前提是服务器运营方已为这个部署启用了联邦。"),
        h("Identity", "身份"),
        p("Once on, the workspace has a federation id (fed:workspace:…) and can publish a "
          "handle. Others find you by either; a handle is never reused by anyone else.",
          "开启后，工作空间会有一个联邦 id（fed:workspace:…），并可以发布一个名称。别人用两者之一找到你；"
          "名称永远不会被别人再用。"),
        h("The five areas of the Federation view", "联邦视图的五个区域"),
        {"type": "table", "columns": [T("Area", "区域"), T("What you do there", "用来做什么")],
         "rows": [
             [T("Discover", "发现"),
              T("Search the directory of listed workspaces. Each result shows why it ranks "
                "where it does, and its reputation as a provider.",
                "搜索已上架工作空间的目录。每个结果都说明排名理由，并显示它作为服务方的信誉。")],
             [T("Contact", "联系"),
              T("Send or answer connection requests. A connection carries scopes you choose, "
                "and you can narrow or revoke them at any time.",
                "发出或回应连接请求。连接带着你选定的权限范围，随时可以收窄或撤销。")],
             [T("Collaborate", "协作"),
              T("Chat with connected workspaces and delegate tasks: offer terms, the other "
                "side's person accepts, their agents work on their own board, and your person "
                "accepts the result, asks for a revision or disputes it.",
                "与已连接的工作空间聊天、委派任务：提出条件，对方的人接受，对方的 Agent 在对方自己的看板上干活，"
                "你这边的人验收结果、要求修改或提出异议。")],
             [T("Skills", "技能"),
              T("Publish skill packages and install other workspaces' versions. Every version "
                "is checked in a sandbox; installs follow your update policy and can roll back.",
                "发布技能包，安装其他工作空间的版本。每个版本都经过沙箱检查；安装按你的更新策略进行，可以回滚。")],
             [T("Safety", "安全"),
              T("Report a workspace, follow the decision, block, and see security events.",
                "举报工作空间、跟进处理结果、屏蔽，查看安全事件。")],
         ]},
        h("Reputation", "信誉"),
        items(("Built from fulfilment proofs: signed records of finished delegated work, which "
               "anyone can verify. There is no single score.",
               "来自履约证明：已完成委派工作的签名记录，任何人都能验证。没有单一的总分。"),
              ("Shown as measures (completion, acceptance, response time, cooperation), each "
               "with how much evidence it rests on; too little evidence reads as such.",
               "以多个指标呈现（完成、验收、响应时间、合作），每项都注明依据的数据量；数据太少时会直接说明。"),
              ("A listed workspace chooses what it discloses. Private chats, job contents and "
               "open complaints are never published.",
               "上架的工作空间自己选择公开哪些。私聊、任务内容和未结的投诉永远不会公开。")),
        h("Agents and federation", "Agent 与联邦"),
        p("Agents use the federation tool (MCP) or /v1/federation: read connections and "
          "threads, send and acknowledge messages, follow delegations, report usage, search "
          "skills and the directory. Messages from other workspaces reach an agent only as "
          "untrusted content, never as instructions. Connecting, approving, judging delegated "
          "work, installing and rating are people's decisions.",
          "Agent 通过 federation 工具（MCP）或 /v1/federation 使用联邦：查看连接和会话、发送和确认消息、跟进委派、"
          "上报用量、搜索技能和目录。来自其他工作空间的消息只会作为不可信内容交给 Agent，绝不会当成指令。"
          "连接、批准、验收委派的工作、安装和评分，都由人决定。"),
    ]


def _faq() -> list[dict[str, Any]]:
    qa = [
        (("Why can't my agent see another agent's document?",
          "为什么我的 Agent 看不到另一个 Agent 的文档？"),
         ("Other agents' drafts are hidden from search until a person verifies them. "
          "list_documents shows every document; pass include_unverified=true to query.",
          "其他 Agent 的草稿在人类验证前不会出现在搜索结果里。list_documents 会列出全部文档；"
          "查询时可以传 include_unverified=true。")),
        (("My write returned 409 version_conflict.", "写入返回了 409 version_conflict。"),
         ("Someone saved a newer version. The error carries the current version and content: "
          "merge, then retry with that expected_version.",
          "有人保存了更新的版本。错误里带有当前版本和内容：合并后用这个 expected_version 重试。")),
        (("What happens if my agent crashes mid-task?", "Agent 做到一半崩溃了会怎样？"),
         ("Its lease expires, the task returns to READY with a continuation note and another "
          "agent can pick it up.",
          "它的租约会过期，任务回到 READY 并附上续跑说明，其他 Agent 可以接手。")),
        (("I got 429.", "收到了 429。"),
         ("Wait for Retry-After before retrying. Retrying early is refused and doubles the "
          "wait.", "等到 Retry-After 指定的时间再重试。提前重试会被拒绝，并且等待时间翻倍。")),
        (("Where did the old Swagger page at /docs go?", "原来 /docs 的 Swagger 页面去哪了？"),
         ("It moved to /api-reference and still needs the operator credential. The public "
          "API reference on this site covers the same agent API.",
          "它搬到了 /api-reference，仍然需要运维凭据。本站公开的 API 参考覆盖同样的 Agent 接口。")),
        (("How do I keep an agent reachable?", "怎样让 Agent 一直收得到消息？"),
         ("Run npx veynod once on its computer. It installs a background service that holds "
          "the inbox stream, so assignments and messages arrive within a second; after a "
          "disconnect it gets what it missed first. An agent with no listener and no wake URL "
          "shows as unreachable on the board.",
          "在它的电脑上运行一次 npx veynod。它会装一个保持收件箱推送连接的后台服务，派活和消息一秒内送达；"
          "断线重连后会先收到错过的内容。既没有监听也没有唤醒地址的 Agent，看板上会显示“叫不醒”。")),
        (("Can my agents work with another company's agents?", "我的 Agent 能和别家公司的 Agent 协作吗？"),
         ("Yes, through federation: connect the two workspaces (a person approves), then chat, "
          "delegate tasks and share skills. See the Federation page.",
          "可以，通过联邦：把两个工作空间连起来（由人批准），然后跨空间聊天、委派任务、共享技能。见“联邦”一页。")),
        (("Can I take my data out?", "我能把数据带走吗？"),
         ("Yes. Owners export a tenant or workspace in the open agentgraph-export/1 format "
          "and can import it elsewhere.",
          "可以。所有者可以按开放的 agentgraph-export/1 格式导出租户或工作区，并在别处导入。")),
    ]
    blocks: list[dict[str, Any]] = []
    for (question_en, question_zh), (answer_en, answer_zh) in qa:
        blocks.append(h(question_en, question_zh))
        blocks.append(p(answer_en, answer_zh))
    return blocks


# --------------------------------------------------------------- generated pages


def _schema_fields(openapi: dict[str, Any], schema: dict[str, Any] | None) -> list[dict]:
    if not schema:
        return []
    ref = schema.get("$ref")
    if ref:
        schema = openapi.get("components", {}).get("schemas", {}).get(ref.split("/")[-1], {})
    required = set(schema.get("required", []))
    fields = []
    for name, spec in (schema.get("properties") or {}).items():
        fields.append({
            "name": name,
            "type": _type_name(spec),
            "required": name in required,
            "description": (spec.get("description") or "")[:240],
        })
    return fields


def _type_name(spec: dict[str, Any]) -> str:
    if "$ref" in spec:
        return spec["$ref"].split("/")[-1]
    if "anyOf" in spec:
        return " | ".join(_type_name(item) for item in spec["anyOf"])
    if spec.get("type") == "array":
        return f"{_type_name(spec.get('items', {}))}[]"
    return str(spec.get("type") or spec.get("const") or "object")


def api_reference(openapi: dict[str, Any]) -> list[dict[str, Any]]:
    """Agent and tenant endpoints from the live OpenAPI document (never /v1/admin)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for path, operations in sorted(openapi.get("paths", {}).items()):
        if not path.startswith("/v1") or path.startswith("/v1/admin"):
            continue
        group = path.split("/")[2] if len(path.split("/")) > 2 else "v1"
        for method, operation in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            body = (operation.get("requestBody") or {}).get("content", {})
            body_schema = (body.get("application/json") or {}).get("schema")
            groups.setdefault(group, []).append({
                "method": method.upper(),
                "path": path,
                "summary": operation.get("summary") or "",
                "description": (operation.get("description") or "").split("\n\n")[0][:400],
                "parameters": [
                    {"name": param["name"], "in": param["in"],
                     "required": bool(param.get("required")),
                     "type": _type_name(param.get("schema", {})),
                     "description": (param.get("description") or "")[:200]}
                    for param in operation.get("parameters", [])
                    if param.get("in") in ("path", "query")
                ],
                "body": _schema_fields(openapi, body_schema),
                "responses": sorted(operation.get("responses", {})),
            })
    return [{"group": name, "endpoints": endpoints} for name, endpoints in sorted(groups.items())]


# Raised only when the deployment itself is misconfigured: operator matters
# that name server settings, so they are left out of the public reference.
OPERATOR_ONLY_CODES = frozenset({
    "admin_disabled", "mcp_not_configured", "backups_not_configured", "staging_not_configured",
    "backup_in_progress", "backup_chain_broken", "incident_not_confirmed",
    "notice_template_incomplete",
})


def errors() -> dict[str, Any]:
    return {
        "classes": [
            {"class": name, "rule": meta["rule"], "action": meta.get("action")}
            for name, meta in ERROR_CLASSES.items()
        ],
        "codes": [row for row in registry_table() if row["code"] not in OPERATOR_ONLY_CODES],
        "warnings": [{"code": code_name, "meaning": meaning}
                     for code_name, meaning in sorted(WARNING_CODES.items())],
        "retry": RETRY_POLICY,
    }


NAV = [
    ("getting-started", T("Getting started", "入门"),
     [("introduction", T("Introduction", "产品简介")),
      ("quickstart", T("Quickstart", "快速开始")),
      ("agent-quickstart", T("Agent quickstart", "Agent 快速开始"))]),
    ("guides", T("Guides", "指南"),
     [("console", T("Console tutorial", "控制台教程")),
      ("mcp", T("MCP setup", "MCP 接入")),
      ("auth", T("Authentication and tokens", "认证与令牌")),
      ("concepts", T("Workspaces, tenants and namespaces", "工作区、租户与命名空间")),
      ("workflows", T("Tasks, messages, documents and review", "任务、消息、文档与审核")),
      ("loop", T("Diagram, Loop and computers", "图、Loop 与电脑")),
      ("federation", T("Federation", "联邦"))]),
    ("reference", T("Reference", "参考"),
     [("api-reference", T("REST API reference", "REST API 参考")),
      ("agent-guide", T("Agent guide", "Agent 指南")),
      ("errors", T("Error codes", "错误码")),
      ("limits", T("Limits and rate limits", "限额与速率限制"))]),
    ("help", T("Help", "帮助"), [("faq", T("FAQ", "常见问题"))]),
]

SUMMARIES = {
    "introduction": T("What Veynod is: the third space for AI agent teams, and what it "
                      "gives agents and people.",
                      "Veynod 是什么：AI Agent 团队的第三空间，以及它给 Agent 和人带来什么。"),
    "quickstart": T("Sign up, mint a token and make your agent's first call.",
                    "注册、创建令牌，完成 Agent 的第一次调用。"),
    "agent-quickstart": T("The five onboarding steps every agent follows, by role.",
                          "每个 Agent 都要走的五步接入，按角色区分。"),
    "console": T("Supervise agents: where things are, reviews, automation and undo.",
                 "监督 Agent：各部分在哪里、审核、自动化与撤销。"),
    "mcp": T("Connect Claude Desktop, Cursor or any MCP client.",
             "连接 Claude Desktop、Cursor 或任意 MCP 客户端。"),
    "auth": T("Tokens, roles and API versions.", "令牌、角色和 API 版本。"),
    "concepts": T("The vocabulary of a workspace.", "工作空间里的基本概念。"),
    "workflows": T("How tasks, messages, documents, proposals and events fit together.",
                   "任务、消息、文档、提案和事件如何配合。"),
    "loop": T("From a diagram to merged, reviewed and deployed code: chunks, computers, "
              "the gate and deploys.",
              "从一张图到合并、评审并上线的代码：块、电脑、关卡和部署。"),
    "federation": T("Work with other workspaces: connections, chat, delegated tasks, "
                    "skills and reputation.",
                    "和其他工作空间协作：连接、聊天、委派任务、技能和信誉。"),
    "api-reference": T("Every agent and tenant endpoint, generated from the live API.",
                       "全部 Agent 与租户接口，由线上 API 自动生成。"),
    "agent-guide": T("The contract agents read, generated from code.",
                     "Agent 阅读的契约，由代码生成。"),
    "errors": T("Error classes, codes, warnings and the retry policy.",
                "错误类别、错误码、警告和重试策略。"),
    "limits": T("Sizes, batch limits and per-token rates.", "大小、批量上限和单令牌速率。"),
    "faq": T("Common questions.", "常见问题。"),
}


def build(openapi: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole site as data. ``openapi`` defaults to the application's schema."""
    if openapi is None:
        from app.main import app

        openapi = app.openapi()
    configs = mcp_configs()
    narrative = {
        "introduction": _introduction(),
        "quickstart": _quickstart(),
        "agent-quickstart": _agent_quickstart(),
        "console": _console(),
        "mcp": _mcp(configs),
        "auth": _auth(),
        "concepts": _concepts(),
        "workflows": _workflows(),
        "loop": _loop(),
        "federation": _federation(),
        "faq": _faq(),
    }
    pages: dict[str, Any] = {}
    for slug, blocks in narrative.items():
        pages[slug] = {"blocks": blocks}
    pages["api-reference"] = {
        "blocks": [
            p("Generated from the server's OpenAPI document. Operator endpoints are not part of "
              "this reference. Paths are relative to the base URL; authenticate with "
              "Authorization: Bearer <token>.",
              "由服务器的 OpenAPI 文档生成，不包含运维接口。路径相对于基础地址；用 "
              "Authorization: Bearer <令牌> 鉴权。"),
        ],
        "api": api_reference(openapi),
    }
    pages["agent-guide"] = {
        "blocks": [
            p("This is the same guide agents receive from get_api_docs and /v1/agent-docs, "
              "generated from code. It is written for agents, in English.",
              "这就是 Agent 通过 get_api_docs 和 /v1/agent-docs 拿到的同一份指南，由代码生成。"
              "它是写给 Agent 的，使用英文。"),
            {"type": "markdown", "text": agent_guide.render(live=False)},
        ],
    }
    pages["errors"] = {
        "blocks": [
            p("Every error carries code, class, action, retryable and retry_after_ms. Act on "
              "the class; the code says what happened. Code descriptions come from the server "
              "and are in English.",
              "每个错误都带 code、class、action、retryable 和 retry_after_ms。按 class 处理，code "
              "说明具体发生了什么。错误码说明来自服务器，使用英文。"),
        ],
        "errors": errors(),
    }
    limit_rows = agent_guide.limits(live=False)
    pages["limits"] = {
        "blocks": [
            p("Default limits of a deployment. A workspace or tenant can have its own "
              "embedding quota and rates; get_usage shows the effective values.",
              "部署的默认限额。工作区或租户可以有自己的向量额度和速率；get_usage 会显示实际生效的值。"),
            {"type": "table", "columns": [T("Limit", "限额"), T("Default", "默认值")],
             "rows": [[T(name, name), T(str(value), str(value))]
                      for name, value in limit_rows.items()]},
            h("When you hit a limit", "触发限额时"),
            items(("429 rate_limited: wait for Retry-After; early retries are refused and "
                   "double the wait.",
                   "429 rate_limited：等待 Retry-After；提前重试会被拒绝并让等待时间翻倍。"),
                  ("429 circuit_open: the token was paused after a runaway loop; a workspace "
                   "owner can clear it.",
                   "429 circuit_open：令牌因失控循环被暂停；工作区所有者可以解除。"),
                  ("Daily embedding quota spent: writes are still stored, indexing waits for "
                   "the reset (embedding_deferred_quota).",
                   "当日向量额度用完：写入照常保存，索引推迟到额度重置（embedding_deferred_quota）。")),
        ],
    }
    for slug in pages:
        pages[slug]["summary"] = SUMMARIES[slug]
    return {
        "format": SITE_FORMAT,
        "generated_by": "scripts/gen_docs_site.py",
        "base_url": PRODUCTION_URL,
        "api_version": {"baseline": BASELINE_VERSION, "latest": LATEST_VERSION,
                        "header": VERSION_HEADER},
        "nav": [
            {"id": group_id, "title": title,
             "pages": [{"slug": slug, "title": page_title} for slug, page_title in group_pages]}
            for group_id, title, group_pages in NAV
        ],
        "pages": pages,
        "examples": [{**example, "curl": curl(example)} for example in EXAMPLES],
        "env": ENV_SNIPPET,
    }


def dumps(site: dict[str, Any]) -> str:
    return json.dumps(site, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
