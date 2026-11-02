"""GET /v1/inbox/stream: pokes pushed to an online agent (TASK-72A59BD9).

Holding this stream open is what "online" means. On connect it replays every
unacked addressed event in seq order (or everything after Last-Event-ID),
then pushes new ones as they commit. Frames carry the seq as their id, so a
reconnect resumes without loss or duplicates; a short re-scan window catches
events that commit out of order. Each push is recorded as a delivery receipt.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from app.api.stream_routes import (
    _acquire_sse,
    _closing_frame,
    _key_revalidator,
    _parse_last_event_id,
    _release_sse,
    get_stream_principal,
)
from app.auth import Principal, extract_bearer_token
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError
from app.events import event_bus
from app.services import inbox, pokes
from app.tenant import set_session_tenant

settings = get_settings()
router = APIRouter(prefix="/v1", tags=["Realtime"])

REPLAY_LIMIT = 500
RESCAN = 50  # seqs re-read behind the cursor: transactions commit out of order
# Comment heartbeat. Often enough that a proxy idle timeout (commonly 60s)
# cannot mistake a quiet inbox for a dead upstream. Clients must ignore it.
HEARTBEAT_SECONDS = 20
LIFETIME_JITTER_SECONDS = 300
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def inbox_lifetime_seconds(base: float, *, roll: Callable[[], float] | None = None) -> float:
    """Configured max lifetime plus 0–300s so agents do not reconnect together."""
    sample = roll() if roll is not None else random.random()
    sample = min(1.0, max(0.0, float(sample)))
    return float(base) + sample * LIFETIME_JITTER_SECONDS


def planned_close_frame(reason: str = "max_lifetime") -> str:
    """A close the client can tell from a failure. ``closed`` still follows."""
    payload = json.dumps({"reason": reason, "reconnect": True})
    return f"retry: 3000\nevent: planned-close\ndata: {payload}\n\n"


async def _pending(principal: Principal, names: list[str], after: int,
                   limit: int = REPLAY_LIMIT) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        set_session_tenant(session, principal.workspace.id, principal.tenant_scope)
        rows = (await session.execute(text(
            "SELECT seq, event_id, type, source_table, task_key, doc_key, actor, version, refs, "
            "payload, created_at FROM workspace_events WHERE workspace_id = :ws "
            "AND targets ?| CAST(:me AS text[]) AND seq IS NOT NULL AND seq > :after "
            "AND actor IS DISTINCT FROM :self ORDER BY seq LIMIT :n"),
            {"ws": principal.workspace.id, "me": names, "after": max(0, after),
             "self": principal.agent_signature, "n": limit})).mappings().all()
    return [dict(row) for row in rows]


def _in_workspace(payload: str, workspace: str) -> bool:
    try:
        return str(json.loads(payload).get("workspace_id")) == workspace
    except (TypeError, ValueError, AttributeError):
        return False


def _message_of(row: dict[str, Any]) -> dict[str, Any] | None:
    """The message carried by a conversation.message event, or None."""
    details = (row.get("payload") or {}).get("details") or {}
    if details.get("delivery_kind") != "message.sent":
        return None
    return {
        "conv_key": details.get("conv_key"),
        "seq": details.get("seq"),
        "message_id": details.get("message_id"),
        "sender": details.get("sender"),
        "body": details.get("body"),
    }


def _frame(row: dict[str, Any], *, replay: bool) -> str:
    pushed = datetime.now(timezone.utc)
    body = inbox.envelope(row)
    body["timestamps"] = {
        "sent_at": (row["payload"] or {}).get("timestamp"),
        "stored_at": row["created_at"].isoformat(),
        "pushed_at": pushed.isoformat(),
        "platform_ms": int((pushed - row["created_at"]).total_seconds() * 1000),
    }
    body["replay"] = replay
    message = _message_of(row)
    event = "message" if message else "poke"
    if message:
        body["message"] = message
    return (f"id: {row['seq']}\nevent: {event}\n"
            f"data: {json.dumps(body, ensure_ascii=False, default=str)}\n\n")


async def _identities(principal: Principal) -> tuple[str, list[str], int]:
    async with async_session_factory() as session:
        set_session_tenant(session, principal.workspace.id, principal.tenant_scope)
        owner, names = await inbox._who(session, principal, False)
        acked = await inbox._acked(session, principal, owner)
    return owner, names, acked


async def _poke_stream(principal: Principal, last_event_id: int | None,
                       revalidate) -> AsyncIterator[str]:
    opened = False
    try:
        owner, names, acked = await _identities(principal)
        me = owner
        sent: OrderedDict[int, None] = OrderedDict()
        cursor = last_event_id if last_event_id is not None else acked
        started = checked = touched = time.monotonic()
        lifetime = inbox_lifetime_seconds(settings.sse_max_lifetime_seconds)

        async def emit(rows: list[dict[str, Any]], *, replay: bool) -> AsyncIterator[str]:
            nonlocal cursor
            for row in rows:
                seq = int(row["seq"])
                if seq in sent:
                    continue
                sent[seq] = None
                while len(sent) > 2_000:
                    sent.popitem(last=False)
                cursor = max(cursor, seq)
                frame = _frame(row, replay=replay)
                # Recorded as the frame is handed to the connection: a client that
                # hangs up right after reading must not leave the receipt unwritten.
                message = _message_of(row)
                if message and message.get("message_id"):
                    from app.services import messaging

                    await messaging.record_stream_delivery(
                        principal.workspace.id, message["message_id"], me)
                else:
                    await pokes.record_delivery(principal.workspace.id, seq, me, "stream")
                yield frame

        # Unsubscribe (subscribe's own finally), drop the SSE slot, and close
        # presence on every exit, including client hang-up and max lifetime.
        await pokes.presence_open(principal, me)
        opened = True
        async with event_bus.subscribe() as queue:
            yield ("retry: 3000\nevent: connected\n"
                   f"data: {json.dumps({'agent': me, 'resume_after': cursor})}\n\n")
            async for frame in emit(await _pending(principal, names, cursor), replay=True):
                yield frame
            while True:
                now = time.monotonic()
                remaining = lifetime - (now - started)
                if remaining <= 0:
                    # planned-close first so new clients reconnect on purpose.
                    # closed stays for clients that only understand that event.
                    yield planned_close_frame("max_lifetime")
                    yield _closing_frame("max_lifetime")
                    return
                if now - checked >= settings.sse_revalidate_seconds:
                    checked = now
                    refreshed = await revalidate()
                    if refreshed is None:
                        yield _closing_frame("credential_revoked")
                        return
                    principal = refreshed
                if now - touched >= pokes.TOUCH_EVERY_SECONDS:
                    touched = now
                    await pokes.presence_touch(principal)
                workspace = str(principal.workspace.id)
                try:
                    payloads = [await asyncio.wait_for(
                        queue.get(), timeout=min(HEARTBEAT_SECONDS, remaining))]
                    # Drain bursts: one query answers many notifications.
                    while not queue.empty():
                        payloads.append(queue.get_nowait())
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                else:
                    if not any(_in_workspace(item, workspace) for item in payloads):
                        continue
                # Notifications carry no targets: re-read what is addressed to me,
                # a little behind the cursor for late commits.
                rows = await _pending(principal, names, cursor - RESCAN)
                async for frame in emit(rows, replay=False):
                    yield frame
    finally:
        _release_sse(principal)
        if opened:
            # A hang-up cancels this task; the shielded close still runs to the end.
            await asyncio.shield(pokes.presence_close(principal))


@router.get("/inbox/stream",
            summary="Pokes pushed as they happen (SSE); holding it open means online")
async def inbox_stream(request: Request,
                       last_event_id: str | None = None,
                       principal: Principal = Depends(get_stream_principal)) -> StreamingResponse:
    if not principal.token_cid:
        raise ApiError(403, "forbidden", "the inbox stream is for agent tokens",
                       details={"use": "GET /v1/stream"})
    await _acquire_sse(principal)
    return StreamingResponse(
        _poke_stream(principal, _parse_last_event_id(request, last_event_id),
                     _key_revalidator(extract_bearer_token(request) or "", principal)),
        media_type="text/event-stream",
        headers=SSE_HEADERS)
