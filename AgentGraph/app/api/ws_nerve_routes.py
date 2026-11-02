"""WS /v1/ws/nerve: the agent's full-duplex channel (see services/nerve_hub).

Handshake headers are ``Authorization: Bearer``, ``X-Agent-Id`` and
``X-Host-Id``. The agent id must match the token: a host running several agents
presents one socket each, and neither identity stands in for the other.

In-band (never a reconnect): ``auth.token_refresh.req`` slides the sub-token
before it expires, ``lease.heartbeat`` renews against the fencing epoch and
gets ``lease.revoked`` when the epoch moved, ``sys.ping`` answers ``sys.pong``
and carries the worker's busy state.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path
from typing import Any

try:  # the shared contract package lives at the repository root
    from contracts.nerve_protocol import (
        CLOSE_UNAUTHORIZED,
        NerveEnvelope,
        NerveFrameType,
        parse_frame,
    )
except ImportError:  # pragma: no cover - only when the cwd is AgentGraph/
    _root = Path(__file__).resolve().parents[3]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from contracts.nerve_protocol import (
        CLOSE_UNAUTHORIZED,
        NerveEnvelope,
        NerveFrameType,
        parse_frame,
    )
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import text

from app.auth import Principal, resolve_principal
from app.database import async_session_factory
from app.services import nerve_hub as hub
from app.services import token_tree

logger = logging.getLogger("agentgraph.ws_nerve")
router = APIRouter(prefix="/v1", tags=["Realtime"])


async def _authenticate(ws: WebSocket) -> tuple[Principal, str, str] | None:
    """Resolve the token and pin it to the headers' identities."""
    # A WebSocket is not a Request; read the header itself instead of borrowing
    # the HTTP helper.
    authorization = ws.headers.get("authorization") or ""
    scheme, _, raw_key = authorization.partition(" ")
    raw_key = raw_key.strip() if scheme.lower() == "bearer" else ""
    if not raw_key:
        raw_key = (ws.headers.get("x-api-key") or "").strip()
    agent_id = (ws.headers.get("x-agent-id") or "").strip()
    host_id = (ws.headers.get("x-host-id") or "").strip() or "unknown-host"
    if not raw_key:
        return None
    async with async_session_factory() as session:
        principal = await resolve_principal(
            session, raw_key, agent_hint=ws.headers.get("x-agent-signature"))
        if principal is None:
            return None
    if agent_id:
        # A token may name its own agent entry; a host token may not act for another.
        if principal.agent_id and principal.agent_id != agent_id:
            return None
        if principal.agent_signature and not principal.agent_id \
                and principal.agent_signature != agent_id:
            return None
    elif principal.agent_id:
        agent_id = principal.agent_id
    else:
        agent_id = principal.agent_signature
    if not agent_id:
        return None
    return principal, agent_id[:128], host_id[:128]


@router.websocket("/ws/nerve")
async def ws_nerve(ws: WebSocket) -> None:
    authenticated = await _authenticate(ws)
    if authenticated is None:
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="UNAUTHORIZED_OR_IDENTITY_MISMATCH")
        return
    principal, agent_id, host_id = authenticated

    await ws.accept()
    sock = hub.ActiveSocket(
        workspace_id=principal.workspace.id, agent_id=agent_id, host_id=host_id,
        conn_id=str(uuid.uuid4()), ws=ws,
    )
    async with async_session_factory() as session:
        await hub.presence_row(session, workspace_id=principal.workspace.id,
                               token_id=principal.token_cid or agent_id,
                               agent_id=agent_id, host_id=host_id, conn_id=sock.conn_id)
    await hub.nerve_hub.register(sock)
    await ws.send_json(NerveEnvelope(
        type=NerveFrameType.AUTH_ACK,
        payload={"conn_id": sock.conn_id, "agent_id": agent_id, "host_id": host_id,
                 "workspace_id": str(principal.workspace.id),
                 "ping_every_s": 5.0},
    ).to_dict())
    await _announce(principal)
    logger.info("agent %s online over the nerve (host=%s)", agent_id, host_id)

    try:
        while True:
            raw = await ws.receive_json()
            frame = parse_frame(raw)
            if frame is None:
                continue
            if frame["type"] != NerveFrameType.PING.value:
                # A ping is 5 s apart; touching on every frame keeps a busy
                # socket younger than the presence window without a second timer.
                async with async_session_factory() as session:
                    await hub.touch_presence(session, principal.workspace.id, sock.conn_id)
            if frame["type"] == NerveFrameType.TOKEN_REFRESH_REQ.value:
                await _refresh_token(ws, principal, agent_id, frame["frame_id"])
                continue
            if frame["type"] == NerveFrameType.LEASE_HEARTBEAT.value:
                await _heartbeat_lease(ws, principal, agent_id, frame)
                continue
            reply = hub.nerve_hub.on_frame(sock, frame)
            if reply is not None:
                await ws.send_json(reply)
    except WebSocketDisconnect:
        removed = await hub.nerve_hub.unregister(sock, "CLIENT_DISCONNECTED")
    except Exception as exc:  # noqa: BLE001 - one socket error is not the process's
        logger.warning("nerve socket error for %s: %s", agent_id, exc)
        removed = await hub.nerve_hub.unregister(sock, f"SOCKET_ERROR:{exc}"[:200])
    if removed:
        async with async_session_factory() as session:
            await hub.clear_presence(session, workspace_id=principal.workspace.id,
                                     conn_id=sock.conn_id)
        await _announce(principal)


async def _refresh_token(ws: WebSocket, principal: Principal, agent_id: str,
                         frame_id: str | None) -> None:
    """Slide the caller's own sub-token in place (same id, new secret).

    Rotation keeps the id, so assignments survive; the previous secret stays
    valid for a grace window, so an in-flight call does not see a 401.
    """
    try:
        async with async_session_factory() as session:
            result = await token_tree.rotate_secret(session, principal, principal.token_cid or "")
    except Exception as exc:  # noqa: BLE001 - report, the old secret still works
        await ws.send_json(NerveEnvelope(
            type=NerveFrameType.TOKEN_REFRESH_RES, frame_id=frame_id,
            payload={"error": str(exc)[:200]},
        ).to_dict())
        return
    await ws.send_json(NerveEnvelope(
        type=NerveFrameType.TOKEN_REFRESH_RES, frame_id=frame_id,
        payload={"token": result["token"], "agent_id": agent_id,
                 "previous_secret_valid_until":
                     result["previous_secret_valid_until"].isoformat()},
    ).to_dict())


async def _heartbeat_lease(ws: WebSocket, principal: Principal, agent_id: str,
                           frame: dict[str, Any]) -> None:
    """Renew the Product lease only at the current fencing epoch."""
    task_key = str(frame["payload"].get("task_key") or "")
    epoch = frame["payload"].get("lease_epoch")
    if not task_key or not isinstance(epoch, int):
        return
    async with async_session_factory() as session:
        result = await session.execute(text(
            "UPDATE collab_tasks SET lease_expires_at = NOW() + make_interval(secs => :ttl), "
            "updated_at = NOW() WHERE workspace_id = :ws AND task_key = :k "
            "AND lease_epoch = :e AND status = 'IN_PROGRESS'"),
            {"ttl": hub._lease_ttl(), "ws": principal.workspace.id, "k": task_key, "e": epoch})
        await session.commit()
    if (result.rowcount or 0) == 0:
        await ws.send_json(NerveEnvelope(
            type=NerveFrameType.LEASE_REVOKED,
            payload={"task_key": task_key, "lease_epoch": epoch,
                     "reason": "FENCING_EPOCH_EXPIRED"},
        ).to_dict())


async def _announce(principal: Principal) -> None:
    """Recompute presence once per connect/disconnect without failing the socket."""
    try:
        from app.services import presence

        await presence.reconcile(principal.workspace.id)
    except Exception:  # noqa: BLE001
        logger.debug("presence announce failed", exc_info=True)
