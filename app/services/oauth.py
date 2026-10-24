"""OAuth 2.1 for MCP clients (v3.36): claude.ai connectors, Claude Desktop, IDEs.

Implements what the MCP authorization spec asks of a server:

* discovery: protected-resource metadata (RFC 9728) and authorization-server
  metadata (RFC 8414); a 401 from /mcp points at the former;
* dynamic client registration (RFC 7591) for public clients;
* authorization code with PKCE S256 only, no client secrets.

The authorization page is the console: a signed-in owner sees which client
asks, picks the agent name and role, and approves. The agent key is minted
through the normal console path at that moment and kept only encrypted under a
key derived from the authorization code (the server stores the code's SHA-256).
The client exchanges the code once and receives the agent key as its bearer
access token, so /mcp authenticates it exactly like a key pasted by hand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.services import product_events

settings = get_settings()

REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=5)
SCOPES = ("mcp",)
_SEAL_INFO = b"veynod-oauth-code-v1"


class OAuthError(Exception):
    """An RFC 6749 error: rendered as {"error", "error_description"}."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def base_url(request_base: str) -> str:
    return (settings.public_base_url or request_base).rstrip("/")


# ------------------------------------------------------------ discovery


def protected_resource(base: str) -> dict[str, Any]:
    return {
        "resource": f"{base}/mcp/",
        "authorization_servers": [base],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Veynod",
        "resource_documentation": f"{base}/docs/mcp/",
    }


def authorization_server(base: str) -> dict[str, Any]:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "scopes_supported": list(SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "service_documentation": f"{base}/docs/mcp/",
    }


# --------------------------------------------------------- registration


def _valid_redirect(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.netloc:
        return False
    if parts.scheme == "https":
        return True
    # Native and CLI clients listen on loopback (RFC 8252).
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


async def register(session: AsyncSession, body: dict[str, Any]) -> dict[str, Any]:
    redirect_uris = body.get("redirect_uris")
    if (not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 10
            or not all(isinstance(item, str) and len(item) <= 2_000 and _valid_redirect(item)
                       for item in redirect_uris)):
        raise OAuthError("invalid_redirect_uri",
                         "redirect_uris must list 1-10 https (or loopback http) URLs")
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise OAuthError("invalid_client_metadata",
                         "only public clients are supported (token_endpoint_auth_method none)")
    name = str(body.get("client_name") or "MCP client")[:128]
    client_id = "vnd_" + secrets.token_urlsafe(18)
    await session.execute(
        text("INSERT INTO oauth_clients (client_id, client_name, redirect_uris) "
             "VALUES (:c, :n, CAST(:r AS jsonb))"),
        {"c": client_id, "n": name, "r": json.dumps(redirect_uris)},
    )
    await session.commit()
    return {
        "client_id": client_id,
        "client_id_issued_at": int(_now().timestamp()),
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(SCOPES),
    }


# -------------------------------------------------------- authorization


async def _client(session: AsyncSession, client_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT client_id, client_name, redirect_uris FROM oauth_clients "
                 "WHERE client_id = :c"), {"c": client_id},
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    uris = row["redirect_uris"]
    return {**row, "redirect_uris": json.loads(uris) if isinstance(uris, str) else uris}


async def start_authorization(session: AsyncSession, params: dict[str, str]) -> str:
    """Validate an /oauth/authorize request; return the console approval URL.

    Raises OAuthError for problems that must not redirect (unknown client,
    unregistered redirect_uri) and returns an error redirect for the rest.
    """
    client = await _client(session, params.get("client_id", ""))
    if client is None:
        raise OAuthError("invalid_client", "unknown client_id; register first")
    redirect_uri = params.get("redirect_uri") or (
        client["redirect_uris"][0] if len(client["redirect_uris"]) == 1 else "")
    if redirect_uri not in client["redirect_uris"]:
        raise OAuthError("invalid_request", "redirect_uri is not registered for this client")
    state = params.get("state")

    def fail(error: str, description: str) -> str:
        query = {"error": error, "error_description": description}
        if state:
            query["state"] = state
        return _with_query(redirect_uri, query)

    if params.get("response_type") != "code":
        return fail("unsupported_response_type", "response_type must be code")
    challenge = params.get("code_challenge") or ""
    if params.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
        return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
    request_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO oauth_requests (id, client_id, redirect_uri, state, code_challenge, "
             "scope, resource, expires_at) VALUES (:id, :c, :r, :s, :cc, :sc, :res, :x)"),
        {"id": request_id, "c": client["client_id"], "r": redirect_uri,
         "s": (state or "")[:1_000] or None, "cc": challenge,
         "sc": (params.get("scope") or "mcp")[:200], "res": (params.get("resource") or "")[:500]
         or None, "x": _now() + REQUEST_TTL},
    )
    await session.execute(
        text("DELETE FROM oauth_requests WHERE expires_at < NOW() - INTERVAL '1 day'"))
    await session.commit()
    return f"/dashboard/oauth/?request={request_id}"


def _with_query(uri: str, query: dict[str, str]) -> str:
    return uri + ("&" if "?" in uri else "?") + urlencode(query)


async def _request_row(session: AsyncSession, request_id: str, *, lock: bool = False):
    try:
        key = uuid.UUID(request_id)
    except ValueError:
        raise NotFound("oauth_request", "unknown authorization request") from None
    row = (
        await session.execute(
            text("SELECT r.*, c.client_name FROM oauth_requests r JOIN oauth_clients c "
                 "USING (client_id) WHERE r.id = :id" + (" FOR UPDATE OF r" if lock else "")),
            {"id": key},
        )
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("oauth_request", "unknown authorization request")
    return row


async def describe(unscoped: AsyncSession, request_id: str) -> dict[str, Any]:
    row = await _request_row(unscoped, request_id)
    status = row["status"]
    if status == "pending" and row["expires_at"] <= _now():
        status = "expired"
    return {
        "request_id": str(row["id"]),
        "status": status,
        "client_name": row["client_name"],
        "redirect_host": urlsplit(row["redirect_uri"]).netloc,
        "scope": row["scope"],
        "expires_at": row["expires_at"].isoformat(),
    }


def _require_owner(principal: Any) -> None:
    if principal.human_role not in ("OWNER", "ADMIN"):
        raise Forbidden("an owner or admin of this workspace connects MCP clients")


def _seal(raw_key: str, code: str, request_id: uuid.UUID) -> str:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=request_id.bytes,
               info=_SEAL_INFO).derive(code.encode())
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(key).encrypt(nonce, raw_key.encode(), _SEAL_INFO)
    return base64.urlsafe_b64encode(nonce + sealed).decode()


def _open(sealed: str, code: str, request_id: uuid.UUID) -> str:
    data = base64.urlsafe_b64decode(sealed)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=request_id.bytes,
               info=_SEAL_INFO).derive(code.encode())
    return AESGCM(key).decrypt(data[:12], data[12:], _SEAL_INFO).decode()


async def approve(scoped: AsyncSession, unscoped: AsyncSession, principal: Any,
                  request_id: str, *, agent_signature: str, role: str,
                  namespaces: list[str], issuer: str) -> dict[str, Any]:
    from app.services.cli_setup import mint_unique

    _require_owner(principal)
    row = await _request_row(unscoped, request_id, lock=True)
    if row["status"] != "pending":
        raise ApiError(409, "oauth_request_decided", f"this request was already {row['status']}")
    if row["expires_at"] <= _now():
        raise ApiError(410, "oauth_request_expired",
                       "this connection request expired; connect again from your client")
    created = await mint_unique(
        scoped, principal.workspace, agent_signature, role, namespaces,
        purpose=f"OAuth: {row['client_name'] or 'MCP client'}",
    )
    code = secrets.token_urlsafe(32)
    await unscoped.execute(
        text("UPDATE oauth_requests SET status = 'approved', workspace_id = :w, user_id = :u, "
             "agent_signature = :a, token_id = :t, code_hash = :h, sealed_key = :s, "
             "decided_at = NOW(), expires_at = :x WHERE id = :id"),
        {"w": principal.workspace.id, "u": principal.user_id,
         "a": created.token.agent_signature, "t": uuid.UUID(str(created.token.id)),
         "h": _sha256(code), "s": _seal(created.api_key, code, row["id"]),
         "x": _now() + CODE_TTL, "id": row["id"]},
    )
    await unscoped.commit()
    await product_events.record("oauth_approved", workspace_id=principal.workspace.id,
                                user_id=principal.user_id,
                                client=(row["client_name"] or "")[:64])
    query = {"code": code, "iss": issuer}
    if row["state"]:
        query["state"] = row["state"]
    return {"redirect_to": _with_query(row["redirect_uri"], query),
            "agent_signature": created.token.agent_signature,
            "workspace": {"id": str(principal.workspace.id),
                          "name": principal.workspace.name}}


async def deny(unscoped: AsyncSession, principal: Any, request_id: str) -> dict[str, Any]:
    _require_owner(principal)
    row = await _request_row(unscoped, request_id, lock=True)
    if row["status"] != "pending":
        raise ApiError(409, "oauth_request_decided", f"this request was already {row['status']}")
    await unscoped.execute(
        text("UPDATE oauth_requests SET status = 'denied', decided_at = NOW(), user_id = :u "
             "WHERE id = :id"), {"u": principal.user_id, "id": row["id"]})
    await unscoped.commit()
    query = {"error": "access_denied", "error_description": "the owner refused the connection"}
    if row["state"]:
        query["state"] = row["state"]
    return {"redirect_to": _with_query(row["redirect_uri"], query)}


# ----------------------------------------------------------------- token


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    return secrets.compare_digest(
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), challenge)


async def exchange(session: AsyncSession, form: dict[str, str]) -> dict[str, Any]:
    grant = form.get("grant_type")
    if grant != "authorization_code":
        raise OAuthError("unsupported_grant_type", "only authorization_code is supported")
    code = form.get("code") or ""
    row = (
        await session.execute(
            text("SELECT * FROM oauth_requests WHERE code_hash = :h FOR UPDATE"),
            {"h": _sha256(code)},
        )
    ).mappings().one_or_none()
    if row is None or row["status"] != "approved" or row["expires_at"] <= _now():
        raise OAuthError("invalid_grant", "the code is unknown, used or expired")
    if form.get("client_id") and form["client_id"] != row["client_id"]:
        raise OAuthError("invalid_grant", "the code was issued to another client")
    if form.get("redirect_uri") and form["redirect_uri"] != row["redirect_uri"]:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization")
    if not _pkce_ok(form.get("code_verifier") or "", row["code_challenge"]):
        raise OAuthError("invalid_grant", "code_verifier does not match the code_challenge")
    access_token = _open(row["sealed_key"], code, row["id"])
    await session.execute(
        text("UPDATE oauth_requests SET status = 'consumed', sealed_key = NULL, "
             "consumed_at = NOW() WHERE id = :id"), {"id": row["id"]})
    await session.execute(
        text("UPDATE oauth_clients SET last_used_at = NOW() WHERE client_id = :c"),
        {"c": row["client_id"]})
    await session.commit()
    await product_events.record("oauth_token_issued", workspace_id=row["workspace_id"],
                                user_id=row["user_id"])
    return {"access_token": access_token, "token_type": "Bearer",
            "scope": row["scope"] or "mcp"}
