"""Public document share links (v3.40).

A link is a capability bound to one document and one token hash.  The public
page contains only an image; the document body is rendered server-side into an
SVG with a watermark, so the HTML never carries the markdown source.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.models import SharedDocument, ShareLink, ShareLinkAccess
from app.services import addresses as address_service
from app.services.common import append_event

settings = get_settings()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _expiry(request: Any) -> datetime | None:
    if request.expiry == "permanent":
        return None
    if request.expiry == "24h":
        return _utcnow() + timedelta(hours=24)
    if request.expiry == "7d":
        return _utcnow() + timedelta(days=7)
    if request.expiry == "30d":
        return _utcnow() + timedelta(days=30)
    if request.custom_hours is not None:
        return _utcnow() + timedelta(hours=int(request.custom_hours))
    if request.custom_days is not None:
        return _utcnow() + timedelta(days=int(request.custom_days))
    raise ApiError(422, "validation_error", "custom expiry is missing a duration",
                   details={"field": "custom_hours|custom_days"})


async def _document(
    session: AsyncSession, principal: Principal, ref: str
) -> Any:
    return await address_service.resolve_document(session, principal.workspace.id, ref)

def _can_manage(principal: Principal, document: SharedDocument) -> bool:
    return principal.is_commander or document.created_by == principal.agent_signature


def link_out(
    link: ShareLink, *, raw_token: str | None = None, origin: str | None = None
) -> dict[str, Any]:
    data = {
        "id": str(link.id),
        "document_id": str(link.document_id),
        "namespace": link.namespace,
        "created_at": link.created_at,
        "expires_at": link.expires_at,
        "revoked_at": link.revoked_at,
        "last_accessed_at": link.last_accessed_at,
        "access_count": int(link.access_count or 0),
        "permanent": link.expires_at is None,
    }
    if raw_token:
        base = (origin or settings.share_link_origin or "").rstrip("/")
        data["url"] = f"{base}/s/{raw_token}" if base else f"/s/{raw_token}"
        data["token"] = raw_token
    return data


async def create_link(
    session: AsyncSession,
    principal: Principal,
    document_ref: str,
    request: Any,
    *,
    origin: str | None = None,
) -> dict[str, Any]:
    document = await _document(session, principal, document_ref)
    if not _can_manage(principal, document):
        raise Forbidden(
            "only the document creator or a workspace commander can create a share link"
        )
    if document.archived_at is not None:
        raise ApiError(409, "document_archived", "archived documents cannot be shared")
    raw_token = secrets.token_urlsafe(32)
    link = ShareLink(
        workspace_id=principal.workspace.id,
        document_id=document.id,
        namespace=document.namespace,
        token_hash=_hash(raw_token),
        created_by=principal.agent_signature,
        expires_at=_expiry(request),
    )
    session.add(link)
    await session.flush()
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="SHARE_LINK_CREATED",
        payload={"link_id": str(link.id), "doc_key": document.doc_key,
                 "expires_at": link.expires_at.isoformat() if link.expires_at else None},
        namespace=document.namespace,
    )
    await session.commit()
    return link_out(link, raw_token=raw_token, origin=origin)


async def list_links(
    session: AsyncSession, principal: Principal, document_ref: str
) -> dict[str, Any]:
    document = await _document(session, principal, document_ref)
    if not _can_manage(principal, document):
        raise Forbidden("only the document creator or a workspace commander can list share links")
    rows = (await session.execute(
        select(ShareLink).where(ShareLink.document_id == document.id)
        .order_by(ShareLink.created_at.desc())
    )).scalars().all()
    return {"document": document.doc_key, "items": [link_out(row) for row in rows]}


async def revoke_link(
    session: AsyncSession, principal: Principal, link_id: str
) -> dict[str, Any]:
    try:
        link_uuid = uuid.UUID(link_id)
    except (ValueError, TypeError) as exc:
        raise NotFound("share link", f"share link '{link_id}' not found") from exc
    link = await session.scalar(select(ShareLink).where(
        ShareLink.id == link_uuid,
        ShareLink.workspace_id == principal.workspace.id,
    ))
    if link is None:
        raise NotFound("share link", f"share link '{link_id}' not found")
    document = await session.get(SharedDocument, link.document_id)
    if document is None or not _can_manage(principal, document):
        raise Forbidden("only the document creator or a workspace commander can revoke this link")
    if link.revoked_at is None:
        link.revoked_at = _utcnow()
        link.revoked_by = principal.agent_signature
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=principal.agent_signature,
            event_type="SHARE_LINK_REVOKED",
            payload={"link_id": str(link.id)},
            namespace=link.namespace,
        )
        await session.commit()
    return {"id": str(link.id), "revoked": True, "revoked_at": link.revoked_at}


async def resolve_public(
    session: AsyncSession, raw_token: str, *, record: bool = True
) -> tuple[ShareLink | None, SharedDocument | None, str]:
    """Resolve a public token and optionally record the access result.

    Returns ``(link, document, result)`` where result is ``ok``, ``expired`` or
    ``revoked``.  The caller must render an empty expired page when not ok.
    """
    link = await session.scalar(select(ShareLink).where(
        ShareLink.token_hash == _hash(raw_token)
    ))
    if link is None:
        return None, None, "invalid"
    document = await session.get(SharedDocument, link.document_id)
    now = _utcnow()
    result = "revoked" if link.revoked_at is not None else (
        "expired" if link.expires_at is not None and link.expires_at <= now else "ok"
    )
    if result == "ok" and (document is None or document.archived_at is not None):
        result = "unavailable"
    if record:
        link.access_count = int(link.access_count or 0) + 1
        link.last_accessed_at = now
        session.add(ShareLinkAccess(link_id=link.id, accessed_at=now, result=result))
        await session.commit()
    return link, document if result == "ok" else None, result


async def access_log(
    session: AsyncSession, principal: Principal, link_id: str
) -> dict[str, Any]:
    try:
        link_uuid = uuid.UUID(link_id)
    except (ValueError, TypeError) as exc:
        raise NotFound("share link", f"share link '{link_id}' not found") from exc
    link = await session.scalar(select(ShareLink).where(
        ShareLink.id == link_uuid,
        ShareLink.workspace_id == principal.workspace.id,
    ))
    if link is None:
        raise NotFound("share link", f"share link '{link_id}' not found")
    document = await session.get(SharedDocument, link.document_id)
    if document is None or not _can_manage(principal, document):
        raise Forbidden("only the document creator or a workspace commander can inspect this log")
    rows = (await session.execute(
        select(ShareLinkAccess).where(ShareLinkAccess.link_id == link.id)
        .order_by(ShareLinkAccess.accessed_at.desc()).limit(200)
    )).scalars().all()
    return {"items": [
        {"accessed_at": row.accessed_at, "result": row.result} for row in rows
    ], "count": len(rows)}


def _markdown_lines(content: str) -> tuple[str, list[str]]:
    """A small, dependency-free markdown-to-readable-lines renderer."""
    lines: list[str] = []
    title = ""
    in_code = False
    for raw in content.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            lines.append("─" * 32)
            continue
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
            continue
        if stripped.startswith(("#", "##", "###")):
            text = stripped.lstrip("#").strip()
        elif stripped.startswith(("- ", "* ", "+ ")):
            text = "• " + stripped[2:].strip()
        elif stripped[:3].isdigit() and stripped[3:5] in (". ", ") "):
            text = stripped
        else:
            text = stripped
        # Strip link targets, keeping the label; remove common emphasis markers.
        while "](" in text and "[" in text.split("](", 1)[0]:
            prefix, rest = text.split("](", 1)
            label = prefix.rsplit("[", 1)[-1]
            suffix = rest.split(")", 1)[-1] if ")" in rest else ""
            text = prefix.rsplit("[", 1)[0] + label + suffix
        text = text.replace("**", "").replace("__", "").replace("`", "")
        if stripped == "":
            lines.append("")
        elif in_code:
            lines.append("  " + text)
        else:
            wrapped = textwrap.wrap(text, width=88, break_long_words=True,
                                    replace_whitespace=False) or [""]
            lines.extend(wrapped)
    return title or "Shared document", lines


def render_page(raw_token: str, title: str = "Shared document") -> str:
    safe_title = html.escape(title[:120])
    token = html.escape(raw_token)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{safe_title}</title>
<style>
html,body{{margin:0;background:#080a0e;color:#cbd5e1;font-family:Inter,system-ui,sans-serif}}
body{{min-height:100vh;display:flex;justify-content:center;padding:24px}}
main{{width:min(100%,980px)}} img{{display:block;width:100%;height:auto;border-radius:12px;
box-shadow:0 24px 80px rgba(0,0,0,.45);-webkit-user-drag:none;user-select:none;pointer-events:none}}
*{{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}}
</style></head>
<body><main><img src="/s/{token}/image" alt="{safe_title}" draggable="false"></main>
<script>
for(const e of ['contextmenu','selectstart','dragstart','copy','cut'])document.addEventListener(
  e,ev=>{{ev.preventDefault();return false}},{{capture:true}});
document.addEventListener('keydown',ev=>{{if((ev.ctrlKey||ev.metaKey)&&
  ['c','x','a','s','u'].includes(ev.key.toLowerCase()))ev.preventDefault()}});
</script></body></html>"""


def render_expired(reason: str = "expired") -> str:
    message = "This link has expired." if reason == "expired" else (
        "This link is no longer available." if reason == "revoked"
        else "This link is not available.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow"><title>Link unavailable</title>
<style>html,body{{margin:0;background:#080a0e;color:#94a3b8;font-family:Inter,system-ui,sans-serif}}
body{{min-height:100vh;display:grid;place-items:center}}main{{text-align:center;font-size:15px}}</style>
</head><body><main>{html.escape(message)}</main></body></html>"""


def render_svg(
    document: SharedDocument, link: ShareLink, *, accessed_at: datetime,
    raw_token: str,
) -> str:
    title, lines = _markdown_lines(document.content or "")
    width = settings.share_link_image_width
    padding = 64
    line_height = 30
    max_lines = 900
    lines = lines[:max_lines]
    height = max(720, padding * 2 + 90 + line_height * max(1, len(lines)))
    watermark = f"{str(link.id)[:8]} · {accessed_at.strftime('%Y-%m-%d %H:%M UTC')}"
    body: list[str] = []
    y = padding + 46
    body.append(
        f'<text x="{padding}" y="{y}" fill="#f8fafc" font-size="30" '
        f'font-weight="650" font-family="Inter,system-ui,sans-serif">'
        f'{html.escape(title[:90])}</text>'
    )
    y += 58
    for line in lines:
        if y > height - padding - 36:
            break
        if not line:
            y += line_height // 2
            continue
        body.append(
            f'<text x="{padding}" y="{y}" fill="#cbd5e1" font-size="19" '
            f'font-family="Inter,system-ui,sans-serif">{html.escape(line)}</text>'
        )
        y += line_height
    # Repeating low-contrast watermark; it is part of the image, not HTML.
    marks: list[str] = []
    for row in range(0, height, 260):
        for col in range(-120, width + 200, 420):
            marks.append(
                f'<text x="{col}" y="{row + 140}" fill="#a5b4fc" opacity="0.16" '
                f'font-size="15" font-family="Inter,system-ui,sans-serif" '
                f'transform="rotate(-24 {col} {row + 140})">{html.escape(watermark)}</text>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        '<rect width="100%" height="100%" fill="#080a0e"/>\n'
        f'<rect x="28" y="28" width="{width - 56}" height="{height - 56}" rx="14" '
        'fill="none" stroke="#1e293b"/>\n'
        f"<g>{''.join(marks)}</g>\n"
        f"<g>{''.join(body)}</g>\n"
        f'<text x="{width - padding}" y="{height - 34}" text-anchor="end" '
        'fill="#475569" font-size="14" font-family="Inter,system-ui,sans-serif">'
        'Veynod · shared document</text>\n'
        "</svg>"
    )
