"""Veynod FastAPI gateway: REST, admin API, MCP mount, health."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import capabilities
from app.api.admin_routes import router as admin_router
from app.api.auth_routes import router as auth_router
from app.api.blob_routes import router as blob_router
from app.api.deploy_gate_routes import admin_router as deploy_gate_admin_router
from app.api.deploy_gate_routes import public_router as deploy_gate_public_router
from app.api.deploy_gate_routes import runner_router as deploy_gate_runner_router
from app.api.human_routes import router as human_router
from app.api.inbox_stream import router as inbox_stream_router
from app.api.oauth_routes import router as oauth_router
from app.api.org_routes import router as org_router
from app.api.public_docs import router as public_docs_router
from app.api.share_routes import public_router as share_public_router
from app.api.share_routes import router as share_router
from app.api.stream_routes import admin_router as admin_stream_router
from app.api.stream_routes import router as stream_router
from app.api.v1_routes import router as v1_router
from app.api.wellknown_routes import router as wellknown_router
from app.concurrency import ConcurrencyLimitMiddleware
from app.config import get_settings
from app.database import apply_schema, check_database, engine
from app.engine.embeddings import close_embedding_provider, configured_embedding_model
from app.errors import ApiError, error_body
from app.events import event_bus
from app.mcp_server.server import create_http_app, mcp
from app.middleware import RequestContextMiddleware
from app.protocol_middleware import ProtocolMiddleware
from app.root_page import ROOT_PRIMITIVES, render_root_page
from app.seo_geo import llms_full, llms_index, mcp_reference
from app.services.external_actions import external_action_loop
from app.services.indexer import index_worker_loop
from app.services.reaper import lease_reaper_loop
from app.services.situation import MetricsMiddleware
from app.services.webhooks import webhook_delivery_loop
from app.versioning import BASELINE_VERSION, LATEST_VERSION

settings = get_settings()

# Public origin used in search metadata. Deliberately fixed: a misconfigured
# deployment must not publish a localhost or staging canonical URL.
SEO_SITE_URL = "https://veynod.com"
OG_IMAGE_PATH = Path(__file__).resolve().parent / "static" / "og.png"
FAVICON_PATH = Path(__file__).resolve().parent / "static" / "favicon.svg"
APPLE_TOUCH_ICON_PATH = Path(__file__).resolve().parent / "static" / "apple-touch-icon.png"


def _is_private_path(path: str) -> bool:
    """Customer, operator and protocol surfaces that must never be indexed."""
    return (
        path == "/dashboard"
        or path.startswith("/dashboard/")
        or path == "/v1"
        or (path.startswith("/v1/") and path != "/v1/agent-docs")
        or path == "/mcp"
        or path.startswith("/mcp/")
        or path == "/s"
        or path.startswith("/s/")
        or path in {"/api-reference", "/openapi.json", "/healthz", "/readyz", "/redoc"}
    )

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agentgraph")

mcp_http_app = create_http_app() if settings.mcp_enabled else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_production:
        token = settings.admin_token or ""
        weak = token in {
            "change-me-in-production",
            "dev-admin-token-change-me",
            "agentgraph-dev-admin",
        }
        if len(token) < 32 or weak:
            raise RuntimeError(
                "production ADMIN_TOKEN must be at least 32 random characters"
            )
    if settings.auto_init_db:
        await apply_schema()
        # One-time, idempotent: constitution + namespace readme stubs (AG-OB-02).
        from app.database import async_session_factory
        from app.services.onboarding import backfill_all_workspaces

        async with async_session_factory() as session:
            scaffolded = await backfill_all_workspaces(session)
        if scaffolded:
            logger.info("scaffolded conventions for %s workspace(s)", scaffolded)
    # Release discipline: record this build once and announce it everywhere.
    from app.services.releases import record_release

    try:
        await record_release(list(ROOT_PRIMITIVES))
    except Exception:  # noqa: BLE001 - announcing must not stop the service
        logger.exception("could not record the release")
    # Every active agent token is in the directory (memory seed spec, R-03).
    from app.services.directory import backfill_auto

    try:
        filled = await backfill_auto()
        if filled:
            logger.info("added %s token(s) to the agent directory (source=auto)", filled)
    except Exception:  # noqa: BLE001 - the directory backfill must not stop the service
        logger.exception("could not backfill the agent directory")
    await event_bus.start()
    reaper_task = asyncio.create_task(
        lease_reaper_loop(), name="agentgraph-lease-reaper"
    )
    webhook_task = asyncio.create_task(
        webhook_delivery_loop(), name="agentgraph-webhook-dispatcher"
    )
    action_task = asyncio.create_task(
        external_action_loop(), name="agentgraph-external-action-dispatcher"
    )
    index_task = asyncio.create_task(index_worker_loop(), name="agentgraph-index-worker")
    from app.services.access_log import flush as flush_access_log
    from app.services.access_log import flush_loop as access_log_loop
    from app.services.backups import backup_scheduler_loop
    from app.services.blobs import blob_gc_loop
    from app.services.incidents import incident_check_loop
    from app.services.mailer import mail_worker_loop
    from app.services.situation import evaluation_loop as situation_evaluation_loop
    from app.services.situation import flush as flush_metrics
    from app.services.situation import situation_loop
    from app.services.token_budget import flush as flush_token_usage
    from app.services.token_budget import flush_loop as token_usage_loop

    background = [
        asyncio.create_task(access_log_loop(), name="agentgraph-access-log"),
        asyncio.create_task(token_usage_loop(), name="agentgraph-token-usage"),
        asyncio.create_task(situation_loop(), name="agentgraph-metrics"),
        asyncio.create_task(situation_evaluation_loop(), name="agentgraph-situation"),
        asyncio.create_task(mail_worker_loop(), name="agentgraph-mailer"),
        asyncio.create_task(incident_check_loop(), name="agentgraph-incident-sla"),
        asyncio.create_task(blob_gc_loop(), name="agentgraph-blob-gc"),
    ]
    if settings.backup_dir and settings.backup_scheduler_enabled:
        background.append(
            asyncio.create_task(backup_scheduler_loop(), name="agentgraph-backups")
        )
    try:
        if settings.mcp_enabled:
            async with mcp.session_manager.run():
                logger.info(
                    "Veynod %s ready (embedding=%s, mcp=/mcp)",
                    settings.app_version,
                    settings.embedding_provider,
                )
                yield
        else:
            yield
    finally:
        tasks = (reaper_task, webhook_task, action_task, index_task, *background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        with suppress(Exception):
            await flush_access_log()
        with suppress(Exception):
            await flush_token_usage()
        with suppress(Exception):
            await flush_metrics()
        await event_bus.stop()
        await close_embedding_provider()
        await engine.dispose()


app = FastAPI(
    title="Veynod — Human-in-the-Loop Shared Blackboard",
    version=settings.app_version,
    description=(
        "A multi-agent shared workspace with TTL task leases, progressive "
        "L0/L1/L2 handoffs, human sovereignty locks, session-batch undo, and "
        "PostgreSQL LISTEN/NOTIFY realtime updates. REST and MCP share one "
        "capability-scoped service layer."
    ),
    lifespan=lifespan,
    # /docs is the public documentation site; Swagger UI lives at /api-reference.
    docs_url=(
        "/api-reference"
        if settings.docs_enabled and not settings.docs_require_auth
        else None
    ),
    redoc_url=(
        "/redoc"
        if settings.docs_enabled and not settings.docs_require_auth
        else None
    ),
    openapi_url=(
        "/openapi.json"
        if settings.docs_enabled and not settings.docs_require_auth
        else None
    ),
)

_CSP = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'; "
    "script-src 'self' 'unsafe-inline' https://accounts.google.com "
    "https://www.gstatic.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
    "https://fonts.googleapis.com https://accounts.google.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self' https://accounts.google.com "
    "https://oauth2.googleapis.com; "
    "frame-src https://accounts.google.com"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.url.hostname == "www.veynod.com":
        target = f"{SEO_SITE_URL}{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=301)
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    path = request.url.path
    if _is_private_path(path):
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    elif (
        (path == "/docs" or path.startswith("/docs/"))
        and response.status_code == 200
        and response.headers.get("content-type", "").startswith("text/html")
    ):
        slug = path.removeprefix("/docs").strip("/")
        canonical = f"{SEO_SITE_URL}/docs/{slug}/" if slug else f"{SEO_SITE_URL}/docs/"
        response.headers.setdefault("Link", f'<{canonical}>; rel="canonical"')
    if path.startswith("/dashboard/_next/static/"):
        response.headers.setdefault(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
    elif (
        path.startswith("/dashboard/") or path == "/dashboard"
        or path.startswith("/docs/") or path == "/docs"
    ):
        response.headers.setdefault(
            "Cache-Control", "no-store, max-age=0, must-revalidate"
        )
    elif path == "/":
        response.headers.setdefault("Cache-Control", "no-store")
    elif path.startswith("/v1/") or path.startswith("/mcp"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# Innermost: count in-flight requests per token only for authenticated calls.
app.add_middleware(ConcurrencyLimitMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_middleware(RequestContextMiddleware)
# Inside CORS so protocol errors (unsupported_api_version) still carry CORS headers.
app.add_middleware(ProtocolMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "AgentGraph-Version", "AgentGraph-Build",
                    "Idempotent-Replayed", "Retry-After"],
)

app.include_router(blob_router)
app.include_router(share_router)
app.include_router(v1_router)
app.include_router(auth_router)
app.include_router(human_router)
app.include_router(org_router)
app.include_router(stream_router)
app.include_router(inbox_stream_router)
app.include_router(admin_router)
app.include_router(admin_stream_router)
app.include_router(deploy_gate_public_router)
app.include_router(deploy_gate_runner_router)
app.include_router(deploy_gate_admin_router)
app.include_router(wellknown_router)
app.include_router(oauth_router)
if mcp_http_app is not None:
    # Trailing slash is the canonical endpoint: /mcp/
    app.mount("/mcp", mcp_http_app, name="mcp")

dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard" / "out"
if dashboard_dir.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(dashboard_dir), html=True),
        name="dashboard",
    )
# Public documentation site (no login). The Next.js export renders it at
# /docs; without a console build the API serves the same content as plain HTML.
docs_site_dir = dashboard_dir / "docs"
if docs_site_dir.is_dir():
    app.mount("/docs", StaticFiles(directory=str(docs_site_dir), html=True), name="docs")
else:
    app.include_router(public_docs_router)
app.include_router(share_public_router)


def _sitemap_lastmod() -> str:
    """Last content change we can prove: the generated docs build stamp."""
    candidates = (
        docs_site_dir / "index.html",
        Path(__file__).resolve().parent / "templates" / "root.html",
    )
    for candidate in candidates:
        try:
            stamp = candidate.stat().st_mtime
        except OSError:
            continue
        return datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat()
    return datetime.now(tz=timezone.utc).date().isoformat()


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> PlainTextResponse:
    rules = (
        "Allow: /\n"
        "Allow: /zh/\n"
        "Allow: /docs/\n"
        "Allow: /v1/agent-docs\n"
        "Allow: /llms.txt\n"
        "Allow: /llms-full.txt\n"
        "Disallow: /dashboard/\n"
        "Disallow: /s/\n"
        "Disallow: /v1/\n"
        "Disallow: /mcp/\n"
        "Disallow: /api-reference\n"
        "Disallow: /openapi.json\n"
        "Disallow: /healthz\n"
        "Disallow: /readyz\n"
    )
    body = (
        "User-agent: *\n"
        + rules + "\n"
        + "".join(
            f"User-agent: {agent}\n{rules}\n"
            for agent in ("Googlebot", "Google-Extended", "GPTBot", "OAI-SearchBot",
                          "ClaudeBot", "PerplexityBot")
        )
        + f"Sitemap: {SEO_SITE_URL}/sitemap.xml\n"
    )
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml() -> Response:
    from app.docs_site import NAV

    lastmod = _sitemap_lastmod()
    urls = [
        f"{SEO_SITE_URL}/", f"{SEO_SITE_URL}/zh/", f"{SEO_SITE_URL}/demo/",
        f"{SEO_SITE_URL}/docs/",
        f"{SEO_SITE_URL}/v1/agent-docs", f"{SEO_SITE_URL}/llms.txt",
        f"{SEO_SITE_URL}/llms-full.txt",
    ]
    seen = set(urls)
    for _group_id, _group_title, pages in NAV:
        for slug, _page_title in pages:
            # The docs home renders the introduction; /docs/introduction/ 404s.
            if slug == "introduction":
                continue
            url = f"{SEO_SITE_URL}/docs/{slug}/"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    body = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
            *[
                f"<url><loc>{url}</loc><lastmod>{lastmod}</lastmod></url>"
                for url in urls
            ],
            "</urlset>",
            "",
        ]
    )
    return Response(
        content=body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt() -> PlainTextResponse:
    return PlainTextResponse(llms_index(settings.app_version), media_type="text/plain")


@app.get("/llms-full.txt", include_in_schema=False)
async def llms_full_txt() -> PlainTextResponse:
    tools = await mcp.list_tools()
    return PlainTextResponse(
        llms_full(settings.app_version) + "\n" + mcp_reference(tools),
        media_type="text/plain",
    )


@app.get("/og.png", include_in_schema=False)
async def og_image() -> FileResponse:
    return FileResponse(
        OG_IMAGE_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(
        FAVICON_PATH,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/apple-touch-icon.png", include_in_schema=False)
async def apple_touch_icon() -> FileResponse:
    return FileResponse(
        APPLE_TOUCH_ICON_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.body(),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=error_body(
            "validation_error", "request validation failed", 422, details=details
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    message = str(exc.detail) if exc.detail else "HTTP error"
    code = "not_found" if exc.status_code == 404 else "http_error"
    headers = getattr(exc, "headers", None) or {}
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message, exc.status_code, headers=headers),
        headers=headers,
    )


@app.exception_handler(SQLAlchemyTimeoutError)
async def overload_handler(request: Request, exc: SQLAlchemyTimeoutError) -> JSONResponse:
    """Connection pool exhausted: a distinguishable, retryable 503 (AG-ST-06)."""
    logger.warning("database pool exhausted on %s", request.url.path)
    headers = {"Retry-After": "1"}
    return JSONResponse(
        status_code=503,
        content=error_body(
            "overloaded",
            "the server is at capacity; retry after the indicated delay",
            503,
            details={"retry_after_ms": 1000},
            headers=headers,
        ),
        headers=headers,
    )


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    logger.exception("database error")
    return JSONResponse(
        status_code=503,
        content=error_body(
            "database_unavailable", "database operation failed", 503,
            headers={"Retry-After": "2"},
        ),
        headers={"Retry-After": "2"},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content=error_body("internal_error", "internal server error", 500),
    )


_docs_security = HTTPBasic(auto_error=False)


def _require_docs_auth(
    credentials: HTTPBasicCredentials | None = Depends(_docs_security),
) -> None:
    if not settings.docs_require_auth:
        return
    admin = settings.admin_token
    if (
        not admin
        or credentials is None
        or not hmac.compare_digest(credentials.password, admin)
    ):
        raise HTTPException(
            status_code=401,
            detail="API docs require the admin token (HTTP Basic password)",
            headers={"WWW-Authenticate": 'Basic realm="Veynod docs"'},
        )


if settings.docs_enabled and settings.docs_require_auth:

    @app.get("/openapi.json", include_in_schema=False)
    async def protected_openapi(
        _: None = Depends(_require_docs_auth),
    ) -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/api-reference", include_in_schema=False)
    async def protected_docs(
        _: None = Depends(_require_docs_auth),
    ) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{settings.app_name} API docs",
        )


_release_cache: dict[str, Any] = {}


async def _current_release() -> dict[str, Any] | None:
    """The recorded release of this build (cached; it changes only on deploy)."""
    if "value" not in _release_cache:
        from app.services.releases import current

        try:
            value = await current()
        except Exception:  # noqa: BLE001 - the root page never fails on this
            return None
        if value is None or value["version"] != settings.app_version:
            return value
        _release_cache["value"] = value
    return _release_cache["value"]


@app.get("/", include_in_schema=False)
async def root(request: Request) -> Any:
    descriptor = {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs/",
        "api_reference": "/api-reference" if settings.docs_enabled else None,
        "health": "/healthz",
        "rest": "/v1",
        "mcp": "/mcp/" if settings.mcp_enabled else None,
        "dashboard": "/dashboard/" if dashboard_dir.is_dir() else None,
        "agent_docs": "/v1/agent-docs",
        "first_call": "get_orientation",
        "primitives": list(ROOT_PRIMITIVES),
        "api_versions": {"default": BASELINE_VERSION, "latest": LATEST_VERSION},
        "release": await _current_release(),
        "changelog": {"namespace": "system", "doc_key": "system/changelog"},
        "capabilities": capabilities.describe(app),
    }
    headers = {"Vary": "Accept", "Cache-Control": "no-store"}
    if (
        request.query_params.get("format", "").lower() == "json"
        or _wants_json(request.headers.get("accept", ""))
    ):
        headers["X-Robots-Tag"] = "noindex"
        return JSONResponse(descriptor, headers=headers)
    headers["Link"] = f'<{SEO_SITE_URL}/>; rel="canonical"'
    return HTMLResponse(render_root_page(descriptor, lang="en"), headers=headers)


def _accept_quality(accept: str, candidate: str) -> float:
    """Quality for one representation, honouring media-range specificity."""
    candidate_type, candidate_subtype = candidate.split("/", 1)
    best_specificity = -1
    best_quality = 0.0
    for item in accept.lower().split(","):
        media, *params = item.strip().split(";")
        media = media.strip()
        if media != "*/*" and "/" not in media:
            continue
        range_type, _, range_subtype = media.partition("/")
        quality = 1.0
        for param in params:
            if param.strip().startswith("q="):
                try:
                    quality = min(1.0, max(0.0, float(param.strip()[2:])))
                except ValueError:
                    quality = 0.0
        if range_type == "*" and range_subtype == "*":
            specificity = 0
        elif range_subtype == "*":
            specificity = 1
        else:
            specificity = 2
        matches = (
            (range_type == "*" and range_subtype == "*")
            or (range_type == candidate_type and range_subtype == "*")
            or (range_type == candidate_type and range_subtype == candidate_subtype)
        )
        if not matches:
            continue
        if specificity > best_specificity:
            best_specificity = specificity
            best_quality = quality
        elif specificity == best_specificity and quality > best_quality:
            best_quality = quality
    return best_quality if best_specificity >= 0 else 0.0


def _wants_json(accept: str) -> bool:
    """Compare JSON and HTML quality; ties and `*/*` keep the HTML landing page."""
    # A wildcard is insufficient to opt an agent into the orientation payload.
    if not any(item.split(";", 1)[0].strip().lower() == "application/json"
               for item in accept.split(",")):
        return False
    # XHTML is not a representation we serve; treating application/* as an
    # XHTML request would make an API client's `application/*` select HTML.
    html = _accept_quality(accept, "text/html")
    return _accept_quality(accept, "application/json") > html


@app.get("/zh/", include_in_schema=False)
@app.get("/zh", include_in_schema=False)
async def root_zh(request: Request) -> HTMLResponse:
    # The showcase only needs the public descriptor fields; no private state.
    descriptor = {
        "name": settings.app_name, "version": settings.app_version,
        "rest": "/v1", "mcp": "/mcp/" if settings.mcp_enabled else None,
        "health": "/healthz", "agent_docs": "/v1/agent-docs",
        "first_call": "get_orientation", "primitives": list(ROOT_PRIMITIVES),
        "dashboard": "/dashboard/" if dashboard_dir.is_dir() else None,
    }
    return HTMLResponse(
        render_root_page(descriptor, lang="zh"),
        headers={"Vary": "Accept", "Cache-Control": "no-store",
                 "Link": f'<{SEO_SITE_URL}/zh/>; rel="canonical"'},
    )


@app.get("/demo", include_in_schema=False)
async def demo_redirect() -> RedirectResponse:
    return RedirectResponse(url="/demo/", status_code=308)


@app.get("/demo/", include_in_schema=False)
async def demo_page() -> HTMLResponse:
    """Public read-only snapshot of the seeded demo workspace."""
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.demo_page import render_demo_page
    from app.models import CollabTask, SharedDocument, Workspace

    async with async_session_factory() as session:
        workspace = await session.scalar(
            select(Workspace).where(Workspace.name == settings.demo_workspace_name)
        )
        documents: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        if workspace is not None:
            docs = (await session.execute(
                select(SharedDocument).where(
                    SharedDocument.workspace_id == workspace.id,
                    SharedDocument.archived_at.is_(None),
                    SharedDocument.namespace != "system",
                ).order_by(SharedDocument.updated_at.desc()).limit(6)
            )).scalars().all()
            documents = [
                {"title": doc.title, "namespace": doc.namespace,
                 "verification_status": doc.verification_status,
                 "updated_at": doc.updated_at, "snippet": doc.content}
                for doc in docs
            ]
            rows = (await session.execute(
                select(CollabTask).where(CollabTask.workspace_id == workspace.id)
                .order_by(CollabTask.priority.desc(), CollabTask.updated_at.desc()).limit(8)
            )).scalars().all()
            tasks = [
                {"title": task.title,
                 "status": getattr(task.status, "value", task.status),
                 "assigned_role": task.assigned_role,
                 "assignee": task.assignee, "priority": task.priority}
                for task in rows
            ]
    return HTMLResponse(
        render_demo_page(documents, tasks, workspace_name=settings.demo_workspace_name),
        headers={"Vary": "Accept", "Cache-Control": "public, max-age=60",
                 "Link": f'<{SEO_SITE_URL}/demo/>; rel="canonical"'},
    )


@app.get("/healthz", tags=["Health"], summary="Liveness probe")
async def healthz() -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "ok", "version": settings.app_version}
    # AG-ST-10: timestamps only (no backup details without authentication).
    from app.services.backups import health_timestamps

    try:
        stamps = await asyncio.wait_for(health_timestamps(), timeout=2)
    except Exception:  # noqa: BLE001 - liveness never fails on this
        stamps = {"last_backup_at": None, "last_restore_drill_at": None}
    payload.update({key: value.isoformat() if value else None for key, value in stamps.items()})
    return payload


@app.get("/readyz", tags=["Health"], summary="Readiness probe")
async def readyz() -> JSONResponse:
    database_ok = await check_database()
    payload = {
        "status": "ok" if database_ok else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "embedding_provider": settings.embedding_provider,
        "embedding_model": configured_embedding_model(),
        "embedding_dimensions": settings.embedding_dimensions,
        "mcp": "enabled" if settings.mcp_enabled else "disabled",
        "version": settings.app_version,
    }
    return JSONResponse(status_code=200 if database_ok else 503, content=payload)
