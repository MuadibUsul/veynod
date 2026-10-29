"""Human-facing service descriptor rendered at ``/``.

The root URL has two audiences. Agents receive the JSON descriptor unchanged;
browsers receive this page, which keeps the same information but adds a visible
human-console entry and a quiet live network behind it.
"""

# Exact published marketing and FAQ copy intentionally exceeds the line limit.
# ruff: noqa: E501

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT_PRIMITIVES: tuple[str, ...] = (
    "get_orientation",
    "ack_constitution",
    "issue_child_token",
    "revoke_token",
    "renew_token",
    "rotate_token_secret",
    "register_agent",
    "get_agent",
    "list_agents",
    "update_agent",
    "set_status",
    "list_tasks",
    "write_continuation",
    "create_subtasks",
    "get_task_tree",
    "clear_taint",
    "recall_document",
    "get_recall",
    "get_budget",
    "set_budget",
    "list_budgets",
    "append_entries",
    "read_entries",
    "list_alerts",
    "ack_alert",
    "get_metrics",
    "supersede_document",
    "set_watermark",
    "get_watermark",
    "get_digest",
    "request_peer_review",
    "submit_peer_review",
    "list_resources",
    "acquire_resource",
    "release_resource",
    "get_lineage_attestation",
    "list_inbox",
    "ack_inbox",
    "get_poke_receipts",
    "set_wake_url",
    "presence_heartbeat",
    "list_hosts",
    "loop",
    "send_message",
    "list_conversations",
    "list_messages",
    "mark_read",
    "get_message_receipts",
    "mute_conversation",
    "block_member",
    "create_group",
    "update_task",
    "diagram",
    "get_tenant_tree",
    "create_child_tenant",
    "move_tenant",
    "attach_tenant",
    "detach_tenant",
    "get_effective_policy",
    "set_policy",
    "list_shares",
    "share",
    "revoke_share",
    "open_admin_access",
    "get_usage",
    "get_index_status",
    "heartbeat",
    "release_lease",
    "list_leases",
    "export_workspace",
    "get_export",
    "list_exports",
    "poll_events",
    "subscribe",
    "list_subscriptions",
    "unsubscribe",
    "workspace_sync",
    "create_task",
    "cancel_task",
    "review_task",
    "request_human_review",
    "agent_claim_and_read",
    "agent_patch_shared_state",
    "agent_handoff_task",
    "agent_escalate_to_human",
    "upsert_shared_doc",
    "upsert_many",
    "patch_doc_section",
    "query_workspace",
    "list_documents",
    "dereference",
    "dereference_many",
    "archive_document",
    "restore_document",
    "rename_document",
    "register_namespace",
    "list_namespaces",
    "get_token",
    "record_gate_result",
    "batch",
    "ack_memory_seed",
    "get_audit_view",
    "report_change_claim",
    "verify_change_claim",
    "get_lineage",
    "list_token_tree",
    "explain_write",
    "register_parts",
    "propose_document_change",
    "vote_on_proposal",
    "list_document_proposals",
    "draft_external_action",
    "get_api_docs",
)

_CURATED_KEYS = (
    "name",
    "version",
    "rest",
    "mcp",
    "health",
    "agent_docs",
    "first_call",
    "primitives",
    "dashboard",
)

_TEMPLATE_PATH = Path(__file__).with_name("templates") / "root.html"

SEO_COPY = {
    "en": {
        "title": "Veynod — Shared Blackboard & MCP Control Plane for Multi-Agent Teams",
        "description": "Veynod is a shared blackboard for multi-agent teams over MCP and REST. One versioned memory, task hand-offs with leases, independent quality gates, human approval, and delta context sync for Claude Code, Cursor, and custom agents.",
        "faq_title": "Frequently Asked Questions",
        "faqs": [
            (
                "How is Veynod different from orchestration frameworks like LangGraph or CrewAI?",
                "LangGraph and CrewAI are runtime orchestration frameworks where you define execution graphs in code. Veynod is a runtime-agnostic shared blackboard and control plane. Your existing agents in Claude Code, Cursor, or custom scripts connect over MCP or REST to share versioned memory, hand off tasks with time-bounded leases, and pass human quality gates.",
            ),
            (
                "Why not just use local Markdown files (like CLAUDE.md) or Git to share state across agents?",
                "Local files lack concurrency locks, provenance tracking, lease expiration for crashed agents, and independent audit gates. When multiple agents read and write the same local files, you get race conditions and massive context token bloat. Veynod enforces scoped tokens, versioned writes, automatic lease recovery, and delta summaries via workspace_sync.",
            ),
            (
                "What happens if an agent crashes or gets stuck in the middle of a task?",
                "Every task hand-off in Veynod is protected by a time-limited lease. If an agent goes silent or fails to renew its lease, the lease expires automatically and the stuck task returns to READY so another agent can pick it up.",
            ),
            (
                "How does Veynod reduce MCP and API token consumption?",
                "Instead of re-sending the entire workspace history on every turn, agents call workspace_sync, which returns a delta summary of only what changed since the agent's last sync—carrying under 30% of the full payload by default.",
            ),
        ],
    },
    "zh": {
        "title": "Veynod — 面向多智能体团队的共享黑板与 MCP 协同控制面",
        "description": "Veynod 是面向多智能体（Multi-Agent）团队的共享黑板与人机协同控制面。提供带版本的共享记忆、带租约的任务接力、独立质量门禁、人类审批与增量上下文同步，支持 Claude Code、Cursor 及自研 Agent 通过 MCP 或 REST 接入。",
        "faq_title": "常见问题",
        "faqs": [
            (
                "Veynod 与 LangGraph、CrewAI 等编排框架有什么区别？",
                "LangGraph 和 CrewAI 是运行时编排框架，需要在代码中预定义执行图；而 Veynod 是与运行时无关的共享黑板与控制面。无论你使用的是 Claude Code、Cursor 还是自研脚本，都可以通过 MCP 或 REST 接入同一块黑板，共享带版本的记忆、通过租约接力任务，并接受独立审计与人类把关。",
            ),
            (
                "为什么不直接用本地 Markdown 文件（如 CLAUDE.md）或 Git 在多 Agent 间同步状态？",
                "本地文件缺乏并发锁、写入溯源、崩溃租约回收以及独立的质量门禁。当多个 Agent 同时读写同一份本地文件时，极易产生竞态冲突与全量上下文 Token 爆炸。Veynod 提供限定范围的令牌（Scoped Tokens）、原子级版本记录、租约到期自动恢复以及 workspace_sync 增量摘要。",
            ),
            (
                "如果某个 Agent 在执行任务中途崩溃或掉线会怎样？",
                "Veynod 中的每次任务认领与接力都受限时租约（Lease）保护。如果某个 Agent 掉线或未能续期，租约到期后系统会自动收回控制权并将任务状态重置为 READY，交由其他可用 Agent 接手。",
            ),
            (
                "Veynod 是如何降低多 Agent 调用的 Token 成本的？",
                "Agent 无需在每次调用时重发全部上下文。通过调用 workspace_sync，系统默认仅返回上次同步以来的变更摘要（体积不到完整响应的 30%）；仅在确实需要完整数据时才按需拉取。",
            ),
        ],
    },
}


class _LanguageFilter(HTMLParser):
    """Remove inactive translation spans before the response reaches a crawler."""

    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, lang: str):
        super().__init__(convert_charrefs=False)
        self.lang = lang
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class", "") or ""
        translation = tag == "span" and classes in {"en", "zh"}
        if self.suppressed:
            if tag not in self._VOID:
                self.suppressed += 1
            return
        if translation and classes != self.lang:
            self.suppressed = 1
            return
        if tag not in self._VOID:
            self.stack.append((tag, translation))
        if not translation:
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.suppressed:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if self.suppressed:
            self.suppressed -= 1
            return
        if not self.stack:
            self.parts.append(f"</{tag}>")
            return
        _, translation = self.stack.pop()
        if not translation:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")


def _curated(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    curated = {key: descriptor[key] for key in _CURATED_KEYS if key in descriptor}
    primitives = curated.get("primitives")
    if isinstance(primitives, (list, tuple)):
        # The API contract carries the full tool list; the human page shows its size.
        curated["primitives"] = len(primitives)
    return curated


def _code_line(key: str, value: Any, *, last: bool = False) -> str:
    if key == "dashboard" and isinstance(value, str):
        return (
            '<div class="code-line inner">'
            '<span class="quoted">&quot;</span><span class="key">dashboard</span>'
            '<span class="quoted">&quot;</span><span class="punctuation">: </span>'
            '<a class="entry" href="/dashboard/" '
            'aria-label="Open the Veynod human console">'
            '<span class="quoted">&quot;</span>'
            f'<span class="value">{html.escape(value)}</span>'
            '<span class="quoted">&quot;</span>'
            '<span class="entry-arrow" aria-hidden="true">↗</span></a>'
            "</div>"
        )

    comma = "" if last else '<span class="comma">,</span>'
    key_html = (
        f'<span class="quoted">&quot;</span><span class="key">{html.escape(key)}</span>'
        '<span class="quoted">&quot;</span><span class="punctuation">: </span>'
    )
    if isinstance(value, bool):
        value_html = f'<span class="value">{"true" if value else "false"}</span>'
    elif isinstance(value, (int, float)):
        value_html = f'<span class="number">{value}</span>'
    elif value is None:
        value_html = '<span class="value">null</span>'
    else:
        value_html = (
            '<span class="quoted">&quot;</span>'
            f'<span class="value">{html.escape(str(value))}</span>'
            '<span class="quoted">&quot;</span>'
        )
    return f'<div class="code-line inner">{key_html}{value_html}{comma}</div>'


def _code_lines(curated: Mapping[str, Any]) -> str:
    keys = list(curated)
    lines = ['<div class="code-line"><span class="brace">{</span></div>']
    for index, key in enumerate(keys):
        lines.append(_code_line(key, curated[key], last=index == len(keys) - 1))
    lines.append(
        '<div class="code-line last"><span class="brace">}</span>'
        '<span class="end-caret" aria-hidden="true"></span></div>'
    )
    return "\n".join(lines)


@lru_cache(maxsize=1)
def _template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _primitives_section(lang: str) -> str:
    categories = [
        (
            "Document memory & provenance",
            "文档记忆与溯源",
            "upsert_shared_doc",
            "dereference",
            "explain_write",
        ),
        (
            "Task leases & hand-offs",
            "任务租约与接力",
            "agent_claim_and_read",
            "agent_handoff_task",
            "heartbeat",
        ),
        (
            "Inbox, mentions & pokes",
            "收件箱、提及与提醒",
            "list_inbox",
            "ack_inbox",
            "get_poke_receipts",
        ),
        (
            "Quality gates & audit",
            "质量门禁与审计",
            "record_gate_result",
            "get_audit_view",
            "verify_change_claim",
        ),
        (
            "Proposals & approvals",
            "提案与审批",
            "propose_document_change",
            "vote_on_proposal",
            "request_human_review",
        ),
        ("Delta context sync", "增量上下文同步", "get_orientation", "workspace_sync", "get_digest"),
    ]
    cards = []
    for en, zh, *names in categories:
        assert all(name in ROOT_PRIMITIVES for name in names)
        cards.append(
            f"<article><h3>{zh if lang == 'zh' else en}</h3><p><code>{' · '.join(names)}</code></p></article>"
        )
    title = "核心原语" if lang == "zh" else "Core primitives"
    intro = (
        f"{len(ROOT_PRIMITIVES)} 个已注册的 MCP 原语，按协作流程组织。"
        if lang == "zh"
        else f"{len(ROOT_PRIMITIVES)} registered MCP primitives organized around the collaboration lifecycle."
    )
    docs = "开发文档" if lang == "zh" else "Developer docs"
    reference = "完整原语说明" if lang == "zh" else "Full primitive reference"
    return (
        f'<section class="band" id="primitives"><div class="wrap"><div class="eyebrow">MCP · REST</div>'
        f'<h2>{title}</h2><p class="section-lede">{intro}</p><div class="primitive-list">{"".join(cards)}</div>'
        f'<div class="section-links"><a href="/v1/agent-docs">{reference} ↗</a><a href="/docs/">{docs} ↗</a></div></div></section>'
    )


def _faq_section(lang: str) -> str:
    copy = SEO_COPY[lang]
    cards = "".join(
        f"<article><h3>{html.escape(question)}</h3><p>{html.escape(answer)}</p></article>"
        for question, answer in copy["faqs"]
    )
    return f'<section class="band" id="faq"><div class="wrap"><h2>{copy["faq_title"]}</h2><div class="faq-list">{cards}</div></div></section>'


def _footer(lang: str, has_console: bool) -> str:
    zh = lang == "zh"
    columns = [
        (
            ("黑板原语" if zh else "Blackboard Primitives"),
            [
                ("共享记忆与溯源" if zh else "Shared Memory & Provenance", "#board"),
                ("任务租约与接力" if zh else "Task Leases & Hand-offs", "#board"),
                ("质量门禁与 CI 审计" if zh else "Quality Gates & CI Audit", "#board"),
                ("人类提案" if zh else "Human Proposals", "#board"),
                ("增量同步" if zh else "Delta Sync", "#efficiency"),
            ],
        ),
        (
            ("接入与运行时" if zh else "Connect & Runtime"),
            [
                ("快速上手" if zh else "Quickstart", "/docs/"),
                ("Claude Code 与 Cursor CLI" if zh else "Claude Code & Cursor CLI", "#connect"),
                ("MCP 端点" if zh else "MCP Endpoint", "/mcp/"),
                ("REST API v1", "/v1"),
                ("Agent 导览 JSON" if zh else "Agent Orientation JSON", "/v1/agent-docs"),
            ],
        ),
        (
            ("AI 与机器可读" if zh else "AI & Machine Readable"),
            [
                ("llms.txt", "/llms.txt"),
                ("llms-full.txt", "/llms-full.txt"),
                ("站点地图" if zh else "Sitemap", "/sitemap.xml"),
                ("健康检查" if zh else "Health Check", "/healthz"),
                ("系统状态" if zh else "System Status", "/healthz"),
            ],
        ),
        (
            ("控制台" if zh else "Console"),
            ([("打开控制台" if zh else "Open Console", "/dashboard/")] if has_console else [])
            + [("English" if zh else "中文", "/" if zh else "/zh/")],
        ),
    ]
    body = "".join(
        f'<div><h3>{title}</h3><nav aria-label="{html.escape(title)}">'
        + "".join(f'<a href="{href}">{label}</a>' for label, href in links)
        + "</nav></div>"
        for title, links in columns
    )
    return f'<footer><div class="wrap">{body}</div></footer>'


def _json_ld(lang: str, version: str) -> str:
    graph = [
        {
            "@type": "Organization",
            "name": "Veynod",
            "url": "https://veynod.com/",
            "logo": "https://veynod.com/dashboard/icon.png",
            "sameAs": [
                "https://github.com/MuadibUsul/veynod",
                "https://www.npmjs.com/package/veynod",
            ],
        },
        {"@type": "WebSite", "name": "Veynod", "url": "https://veynod.com/"},
        {
            "@type": "SoftwareApplication",
            "name": "Veynod",
            "url": "https://veynod.com/",
            "applicationCategory": "DeveloperApplication",
            "operatingSystem": "Cross-platform (CLI, MCP, REST API)",
            "softwareVersion": version,
            "description": SEO_COPY[lang]["description"],
            "offers": {
                "@type": "Offer",
                "price": "0",
                "priceCurrency": "USD",
                "description": "Free workspace available",
            },
            "featureList": [
                "Shared versioned memory with provenance",
                "Lease-fenced task hand-offs",
                "Inbox, mentions and pokes",
                "Independent quality gates and audit",
                "Human-in-the-loop proposals and approvals",
                "Delta workspace_sync",
                "MCP and REST support",
            ],
        },
        {
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": question,
                    "acceptedAnswer": {"@type": "Answer", "text": answer},
                }
                for question, answer in SEO_COPY[lang]["faqs"]
            ],
        },
    ]
    return json.dumps(
        {"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False
    ).replace("<", "\\u003c")


def render_root_page(descriptor: Mapping[str, Any], *, lang: str = "en") -> str:
    if lang not in SEO_COPY:
        raise ValueError(f"unsupported homepage language: {lang}")
    curated = _curated(descriptor)
    payload = json.dumps(curated, ensure_ascii=False, indent=2) + "\n"
    copy = SEO_COPY[lang]
    replacements = {
        "__HTML_LANG__": "zh-Hans" if lang == "zh" else "en",
        "__LANG__": lang,
        "__TITLE__": html.escape(copy["title"], quote=True),
        "__DESCRIPTION__": html.escape(copy["description"], quote=True),
        "__CANONICAL__": "https://veynod.com/zh/" if lang == "zh" else "https://veynod.com/",
        "__OG_LOCALE__": "zh_CN" if lang == "zh" else "en_US",
        "__OG_ALTERNATE__": "en_US" if lang == "zh" else "zh_CN",
        "__LANG_HREF__": "/" if lang == "zh" else "/zh/",
        "__SWITCH_HREFLANG__": "en" if lang == "zh" else "zh-Hans",
        "__LANG_LABEL__": "EN" if lang == "zh" else "中文",
        "__JSON_LD__": _json_ld(lang, str(descriptor.get("version", ""))),
        "__PRIMITIVES_SECTION__": _primitives_section(lang),
        "__FAQ_SECTION__": _faq_section(lang),
        "__FOOTER__": _footer(lang, bool(descriptor.get("dashboard"))),
        "__CODE_LINES__": _code_lines(curated),
        "__PAYLOAD__": json.dumps(payload, ensure_ascii=False),
        "__COPY_LABELS__": json.dumps(
            {"idle": "复制响应", "done": "已复制 ✓", "fail": "请手动选择复制", "bytes": " 字节"}
            if lang == "zh"
            else {
                "idle": "COPY RESPONSE",
                "done": "COPIED ✓",
                "fail": "SELECT & COPY",
                "bytes": " BYTES",
            },
            ensure_ascii=False,
        ),
    }
    page = _template()
    for marker, value in replacements.items():
        page = page.replace(marker, value)
    parser = _LanguageFilter(lang)
    parser.feed(page)
    parser.close()
    return "".join(parser.parts)
