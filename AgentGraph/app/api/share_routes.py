"""Public document share-link routes (v3.40)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_principal
from app.config import get_settings
from app.database import get_db
from app.schemas_blobs import ShareLinkCreateRequest
from app.services import share_links

settings = get_settings()
router = APIRouter(prefix="/v1", tags=["share-links"])
public_router = APIRouter(tags=["share-links"])


def _origin(request: Request) -> str:
    configured = settings.share_link_origin or settings.public_base_url
    return (configured or str(request.base_url)).rstrip("/")


@router.post("/documents/{doc_key:path}/share-links", status_code=201,
             summary="Create a public, read-only share link for one document")
async def create_share_link(
    doc_key: str,
    body: ShareLinkCreateRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await share_links.create_link(
        session, principal, doc_key, body, origin=_origin(request)
    )


@router.get("/documents/{doc_key:path}/share-links",
            summary="List share links for one document")
async def list_share_links(
    doc_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await share_links.list_links(session, principal, doc_key)


@router.delete("/share-links/{link_id}", summary="Revoke a public share link")
async def revoke_share_link(
    link_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await share_links.revoke_link(session, principal, link_id)


@router.get("/share-links/{link_id}/accesses", summary="Read a share link's access log")
async def share_link_accesses(
    link_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await share_links.access_log(session, principal, link_id)


@public_router.get("/s/{token}", response_class=HTMLResponse,
                   summary="Public share page")
async def public_share_page(
    token: str,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    link, document, result = await share_links.resolve_public(session, token)
    headers = {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    }
    if result != "ok" or document is None:
        status = 410 if result in ("expired", "revoked", "unavailable") else 404
        return HTMLResponse(
            share_links.render_expired(result), status_code=status, headers=headers
        )
    # The page carries only an image URL; the markdown body never reaches HTML.
    return HTMLResponse(
        share_links.render_page(token, document.title), headers=headers
    )


@public_router.get("/s/{token}/image", summary="Watermarked share image")
async def public_share_image(
    token: str,
    session: AsyncSession = Depends(get_db),
) -> Response:
    link, document, result = await share_links.resolve_public(
        session, token, record=False
    )
    headers = {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    }
    if result != "ok" or link is None or document is None:
        return Response(status_code=410 if result != "invalid" else 404, headers=headers)
    svg = share_links.render_svg(
        document, link,
        accessed_at=datetime.now(timezone.utc),
        raw_token=token,
    )
    return Response(content=svg, media_type="image/svg+xml", headers=headers)
