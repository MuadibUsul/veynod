"""OAuth 2.1 endpoints for MCP clients (v3.36); see app/services/oauth.py."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.services import oauth

router = APIRouter(tags=["OAuth for MCP clients"])
settings = get_settings()
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _base(request: Request) -> str:
    return oauth.base_url(str(request.base_url))


def _error(exc: oauth.OAuthError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status, headers=_NO_STORE)


@router.get("/.well-known/oauth-protected-resource",
            summary="Protected-resource metadata for /mcp (RFC 9728)")
@router.get("/.well-known/oauth-protected-resource/{resource:path}", include_in_schema=False)
async def protected_resource(request: Request, resource: str = "") -> dict:
    del resource
    return oauth.protected_resource(_base(request))


@router.get("/.well-known/oauth-authorization-server",
            summary="Authorization-server metadata (RFC 8414)")
@router.get("/.well-known/oauth-authorization-server/{issuer:path}", include_in_schema=False)
@router.get("/.well-known/openid-configuration", include_in_schema=False)
async def authorization_server(request: Request, issuer: str = "") -> dict:
    del issuer
    return oauth.authorization_server(_base(request))


@router.post("/oauth/register", status_code=201,
             summary="Register a public MCP client (RFC 7591 dynamic registration)")
async def register(request: Request, session: AsyncSession = Depends(get_db)):
    from app.api.auth_routes import _client_ip
    from app.services.accounts import _rate_limit_login

    await _rate_limit_login(session, f"oauth-register:{_client_ip(request) or 'unknown'}",
                            None, limit=settings.cli_sessions_per_ip_per_minute)
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return _error(oauth.OAuthError("invalid_client_metadata", "send a JSON object"))
    try:
        return JSONResponse(await oauth.register(session, body), status_code=201,
                            headers=_NO_STORE)
    except oauth.OAuthError as exc:
        return _error(exc)


@router.get("/oauth/authorize", summary="Start authorization: approve in the Veynod console")
async def authorize(request: Request, session: AsyncSession = Depends(get_db)):
    try:
        target = await oauth.start_authorization(session, dict(request.query_params))
    except oauth.OAuthError as exc:
        return _error(exc)
    return RedirectResponse(target, status_code=302)


@router.post("/oauth/token", summary="Exchange an authorization code (PKCE) for an access token")
async def token(request: Request, session: AsyncSession = Depends(get_db)):
    raw = (await request.body()).decode("utf-8", errors="replace")
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            form = {key: str(value) for key, value in json.loads(raw or "{}").items()}
        except (ValueError, AttributeError):
            form = {}
    else:
        form = dict(parse_qsl(raw, keep_blank_values=True))
    try:
        return JSONResponse(await oauth.exchange(session, form), headers=_NO_STORE)
    except oauth.OAuthError as exc:
        return _error(exc)
