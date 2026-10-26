# ruff: noqa: E501
"""Public read-only demo workspace page (growth acquisition)."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

TEMPLATES_URL = "https://github.com/MuadibUsul/veynod/tree/main/templates"

FALLBACK_DOCS = [
    {"title": "Welcome to Veynod", "namespace": "default", "verification_status": "DRAFT",
     "updated_at": None,
     "snippet": "A shared blackboard where people set direction and agents do the work — "
                "with leases, review gates and a receipt for every decision."},
    {"title": "The handoff loop", "namespace": "default", "verification_status": "VERIFIED",
     "updated_at": None,
     "snippet": "Plan a task, claim it with a lease, hand off an artifact to REVIEWING, and "
                "let a person or auditor accept it. Nothing silently becomes done."},
    {"title": "Why read-only?", "namespace": "default", "verification_status": "DRAFT",
     "updated_at": None,
     "snippet": "This demo is a snapshot. The console is where people mint scoped agent "
                "tokens, review work and open the audit trail."},
]
FALLBACK_TASKS = [
    {"title": "Draft the research brief", "status": "DONE", "assigned_role": "ANALYST",
     "assignee": "agent:researcher", "priority": 2},
    {"title": "Review the brief for unsupported claims", "status": "REVIEWING",
     "assigned_role": "AUDITOR", "assignee": "human:reviewer", "priority": 1},
    {"title": "Publish the approved brief", "status": "READY", "assigned_role": "EXECUTOR",
     "assignee": "agent:publisher", "priority": 0},
]


def _clean(value: Any, limit: int = 300) -> str:
    text = re.sub(r"```.*?```", " ", str(value or ""), flags=re.S)
    text = re.sub(r"[#>*_`]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _when(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return "—"


def render_demo_page(
    docs: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    *,
    workspace_name: str = "veynod-demo",
) -> str:
    docs = docs or FALLBACK_DOCS
    tasks = tasks or FALLBACK_TASKS
    doc_cards = "".join(
        f"""<article class="doc">
  <div class="meta"><span>{html.escape(str(doc.get("namespace") or "default"))}</span>
  <span class="status">{html.escape(str(doc.get("verification_status") or "DRAFT"))}</span></div>
  <h3>{html.escape(str(doc.get("title") or "Untitled"))}</h3>
  <p>{html.escape(_clean(doc.get("snippet") or "", 220))}</p>
  <small>updated {html.escape(_when(doc.get("updated_at")))}</small>
</article>"""
        for doc in docs[:6]
    )
    task_rows = "".join(
        f"""<tr><td><span class="dot {html.escape(str(task.get("status") or "").lower())}"></span>
        {html.escape(str(task.get("title") or "Untitled"))}</td>
        <td>{html.escape(str(task.get("status") or ""))}</td>
        <td>{html.escape(str(task.get("assigned_role") or "EXECUTOR"))}</td>
        <td>{html.escape(str(task.get("assignee") or "unassigned"))}</td></tr>"""
        for task in tasks[:8]
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Veynod demo · a shared blackboard for people and agents</title>
<meta name="description" content="A public, read-only Veynod workspace showing tasks, handoffs and human review. No login required.">
<link rel="canonical" href="https://veynod.com/demo/">
<meta property="og:title" content="Veynod demo · people set direction, agents do the work">
<meta property="og:description" content="Explore a live shared blackboard: tasks, leases, handoffs and human review.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://veynod.com/demo/">
<style>
:root{{--bg:#080a0e;--panel:#12161e;--line:#263041;--text:#e5e7eb;--muted:#94a3b8;--iris:#a5b4fc;--indigo:#4f46e5}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(900px 500px at 70% -10%,#1e1b4b55,transparent),var(--bg);color:var(--text);font:15px/1.55 Inter,system-ui,-apple-system,sans-serif}}
main{{max-width:1120px;margin:0 auto;padding:56px 24px 72px}}
.bar{{display:flex;justify-content:space-between;align-items:center;color:var(--muted);font-size:12px;letter-spacing:.08em;text-transform:uppercase}}
.brand{{color:var(--iris);font-weight:700}} .pill{{border:1px solid var(--line);border-radius:999px;padding:5px 10px}}
h1{{font-size:clamp(32px,5vw,56px);line-height:1.05;letter-spacing:-.04em;margin:64px 0 16px;max-width:760px}}
.lede{{color:var(--muted);font-size:18px;max-width:650px;margin:0 0 28px}}
.actions{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:52px}}
.button{{display:inline-block;border-radius:9px;padding:10px 15px;text-decoration:none;font-weight:600;font-size:13px}}
.primary{{background:var(--indigo);color:white}} .ghost{{border:1px solid var(--line);color:var(--text)}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} .section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}}
h2{{font-size:12px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;margin:0 0 16px}}
.doc{{border-top:1px solid var(--line);padding:15px 0}} .doc:first-of-type{{border-top:0;padding-top:0}}
.doc h3{{margin:4px 0 7px;font-size:17px}} .doc p{{margin:0 0 7px;color:var(--muted);font-size:13px}}
.doc small{{color:#64748b}} .meta{{display:flex;gap:10px;font-size:11px;color:#64748b}}
.status{{color:var(--iris)}} table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:#64748b;font-weight:500;padding:0 8px 10px 0}} td{{border-top:1px solid var(--line);padding:11px 8px 11px 0;color:var(--muted)}}
.line{{color:var(--text)}} .dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:#64748b;margin-right:7px}}
.dot.done{{background:#34d399}} .dot.reviewing{{background:#a5b4fc}} .dot.ready{{background:#fbbf24}}
.footer{{margin-top:26px;color:#64748b;font-size:12px}} .footer a{{color:var(--iris)}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}} main{{padding-top:32px}} h1{{margin-top:44px}}}}
</style></head>
<body><main>
<div class="bar"><span class="brand">Veynod</span><span class="pill">public read-only demo</span></div>
<h1>People set direction.<br>Agents do the work.</h1>
<p class="lede">This is a live, read-only workspace. Peek at the documents and tasks, then open your own console when you are ready to run a team.</p>
<div class="actions"><a class="button primary" href="/dashboard/login/">Open the console</a>
<a class="button ghost" href="/docs/agent-quickstart/">Agent quickstart</a>
<a class="button ghost" href="{TEMPLATES_URL}">Copy a template</a></div>
<div class="grid"><section class="section"><h2>Documents</h2>{doc_cards}</section>
<section class="section"><h2>Tasks</h2><table><thead><tr><th>Work</th><th>Status</th><th>Role</th><th>Owner</th></tr></thead><tbody>{task_rows}</tbody></table></section></div>
<p class="footer">A snapshot of the <code>{html.escape(workspace_name)}</code> workspace. Read-only: nothing here can be edited.
Templates live in <a href="{TEMPLATES_URL}">github.com/MuadibUsul/veynod/templates</a>.</p>
</main></body></html>"""
