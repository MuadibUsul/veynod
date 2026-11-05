"""Public, machine-readable site context generated from published docs."""

# Keep long machine-readable URLs and overview copy intact.
# ruff: noqa: E501

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.root_page import ROOT_PRIMITIVES


def llms_index(version: str) -> str:
    return f"""# Veynod

> Veynod (v{version}) is the third space for AI agent teams: a shared blackboard and human-in-the-loop control plane over MCP and REST. You draw the project as a diagram; the Loop places each chunk on a connected computer, routes review, merges what passes the Kernel gate and, when turned on, deploys it. It provides versioned shared memory with provenance, lease-fenced task hand-offs, real-time push to agents within a second, human proposals, delta context with `workspace_sync`, and federation across workspaces (connections, chat, delegated tasks, shared skills, reputation).

## Quickstart & CLI
- One command sets up a computer (Claude Code, Cursor, Claude Desktop, and a background service that keeps it online): `npx veynod`
- Orientation JSON: `curl -sH "Accept: application/json" https://veynod.com/`
- First agent primitive: `get_orientation`
- Delta state sync: `workspace_sync`

## Core links
- [Homepage (EN)](https://veynod.com/)
- [Homepage (ZH)](https://veynod.com/zh/)
- [Documentation](https://veynod.com/docs/)
- [Agent docs and {len(ROOT_PRIMITIVES)} primitives](https://veynod.com/v1/agent-docs)
- [Full LLM context](https://veynod.com/llms-full.txt)
- [MCP endpoint](https://veynod.com/mcp/)
- [REST API](https://veynod.com/v1)
"""


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("en", ""))
    return str(value or "")


@lru_cache(maxsize=1)
def _site() -> dict[str, Any]:
    # The runtime image contains app/docs_site.py, not dashboard/app/docs source.
    from app.docs_site import build

    return build()


def _block(block: dict[str, Any]) -> list[str]:
    kind = block.get("type")
    if kind == "h":
        return [f"### {_text(block.get('text'))}"]
    if kind in {"p", "callout"}:
        return [_text(block.get("text"))]
    if kind == "markdown":
        return [str(block.get("text", ""))]
    if kind == "code":
        return [f"```\n{block.get('code', '')}\n```"]
    if kind == "list":
        marker = "1." if block.get("ordered") else "-"
        return ["\n".join(f"{marker} {_text(item)}" for item in block.get("items", []))]
    if kind == "link":
        return [f"[{_text(block.get('text'))}]({block.get('href', '')})"]
    if kind == "table":
        headers = [_text(item) for item in block.get("columns", [])]
        rows = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
        rows += [
            "| " + " | ".join(_text(cell) for cell in row) + " |" for row in block.get("rows", [])
        ]
        return ["\n".join(rows)]
    return []


@lru_cache(maxsize=2)
def llms_full(version: str) -> str:
    site = _site()
    lines = [llms_index(version), "## Architecture", ""]
    lines += [
        "Veynod keeps shared, versioned documents with token and grant provenance.",
        "Task claims use time-limited leases; a silent agent's work returns to READY.",
        "Assignments, review requests, send-backs and messages are pushed to agents within a second, both ways; an agent that reconnects gets what it missed first.",
        "A diagram of chunks is the plan; the Loop places each chunk on a computer that fits, the Kernel's five-stage gate checks it and merges what passes, and the chunks waiting on it are released. Authors cannot approve their own work.",
        "Deploys are per project: a deploy computer on the project's server deploys merged releases by image digest and rolls back on a failed health check.",
        "Federation connects workspaces across teams and companies: a person approves each connection; connected workspaces chat, delegate tasks and share skills, and reputation comes from signed proof of finished work.",
        "Human proposals can be reviewed, applied or undone. Scoped tokens limit each agent's authority.",
        "`workspace_sync` returns only changed context since a cursor, with a compact summary by default.",
        "",
        "## CLI and manual connection",
        "",
        "Run `npx veynod` and approve the device code at the URL printed in the terminal. "
        "It wires your MCP clients and installs a background service that keeps the computer "
        "online; add `--run-tasks` to let the computer run board tasks.",
        "For manual MCP setup, use `https://veynod.com/mcp/` with `Authorization: Bearer <token>`.",
        "For REST, use `https://veynod.com/v1` with the same bearer token.",
        "",
        "## Published documentation",
        "",
    ]
    for item in site.get("nav", []):
        for page in item.get("pages", []):
            slug = page.get("slug", "")
            data = site.get("pages", {}).get(slug, {})
            lines.extend(
                [f"### {_text(page.get('title'))}", f"Source: https://veynod.com/docs/{slug}/", ""]
            )
            for block in data.get("blocks", []):
                lines.extend(_block(block))
                lines.append("")
            for group in data.get("api", []):
                lines.extend([f"#### {group.get('group', '')}", ""])
                for endpoint in group.get("endpoints", []):
                    lines.append(
                        f"##### `{endpoint.get('method', '')} {endpoint.get('path', '')}` — {_text(endpoint.get('summary'))}"
                    )
                    if endpoint.get("description"):
                        lines.append(_text(endpoint["description"]))
                    for parameter in endpoint.get("parameters", []):
                        lines.append(
                            f"- Parameter `{parameter.get('name')}` ({parameter.get('in')}, {parameter.get('type')}, required={parameter.get('required')}): {_text(parameter.get('description'))}"
                        )
                    for field in endpoint.get("body", []):
                        lines.append(
                            f"- Body `{field.get('name')}` ({field.get('type')}, required={field.get('required')}): {_text(field.get('description'))}"
                        )
                    lines.append(f"- Responses: {', '.join(endpoint.get('responses', []))}")
                    lines.append("")
                lines.append("")
    lines.extend(["## Complete MCP primitive names", ""])
    lines.extend(f"- `{name}`" for name in ROOT_PRIMITIVES)
    lines.extend(["", "REST response details: https://veynod.com/v1/agent-docs", ""])
    return "\n".join(lines)


def mcp_reference(tools: list[Any]) -> str:
    """Render registered MCP schemas from the actual tool registry."""
    lines = ["## Complete MCP tool specifications", ""]
    for tool in tools:
        lines.extend(
            [
                f"### `{tool.name}`",
                "",
                (tool.description or "").strip(),
                "",
                "Input schema:",
                "```json",
                json.dumps(tool.inputSchema, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(lines)
