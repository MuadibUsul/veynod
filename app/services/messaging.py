"""Friend-style messaging (blueprint-L3-messaging-open).

Generalizes the 3.6.0 poke/receipt foundation to conversations (spec-realtime-
messaging v2): permanent messages, stream-first delivery with inbox fallback,
and sent/delivered/read receipts driven by an explicit per-member read
watermark (M-07). Delivery reuses the workspace_events outbox: a message emits
a ``MESSAGE_SENT`` timeline event whose ``payload.targets`` are the other
members, so the existing inbox stream pushes it live and the inbox keeps it as
the record/fallback path.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_permission
from app.errors import ApiError, Forbidden, NotFound
from app.models import Conversation, ConversationMember, Message, MessageDelivery
from app.services.common import append_event

DIRECT_KIND = "direct"
GROUP_KIND = "group"
MESSAGE_EVENT = "MESSAGE_SENT"
MESSAGE_LIMIT = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _direct_key(peer_a: str, peer_b: str) -> str:
    """Order-independent, stable 1:1 conversation key (M-10 exact compare)."""
    low, high = sorted([peer_a, peer_b])
    digest_ = hashlib.sha256(f"{low}\x00{high}".encode("utf-8")).hexdigest()[:16]
    return f"dm_{digest_}"


async def _sender_ref(session: AsyncSession, principal: Principal) -> str:
    """The caller's stable unique address (M-10): ``agent:<agent_id>`` when
    registered, else ``agent:<agent_signature>``; humans are ``human:<id>``."""
    if principal.is_human:
        if principal.user_id is not None:
            return f"human:{principal.user_id}"
        return f"human:{principal.agent_signature}"
    if principal.token_cid:
        agent_id = await session.scalar(
            text(
                "SELECT agent_id FROM agent_directory WHERE :t = ANY(token_ids) LIMIT 1"
            ),
            {"t": principal.token_cid},
        )
        if agent_id:
            return f"agent:{agent_id}"
    return f"agent:{principal.agent_signature}"


async def _resolve_agent_ref(
    session: AsyncSession, workspace_id: Any, ref: str
) -> str:
    """Normalize an agent ref to its directory ``agent:<agent_id>`` form (M-10:
    address comparison is exact, not by display name). Unknown refs pass through."""
    kind, _, ident = ref.partition(":")
    if kind != "agent" or not ident:
        return ref
    agent_id = await session.scalar(
        text(
            "SELECT agent_id FROM agent_directory WHERE workspace_id = :ws "
            "AND (agent_id = :i OR principal_id = :i OR :i = ANY(signatures) "
            "OR :i = ANY(legacy_names)) LIMIT 1"
        ),
        {"ws": workspace_id, "i": ident},
    )
    return f"agent:{agent_id}" if agent_id else ref


async def _member(
    session: AsyncSession, conversation_id: int, ref: str
) -> ConversationMember | None:
    return (
        await session.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.member_ref == ref,
            )
        )
    ).scalar_one_or_none()


async def _conversation_by_key(
    session: AsyncSession, principal: Principal, conv_key: str
) -> Conversation:
    conv = (
        await session.execute(
            select(Conversation).where(
                Conversation.workspace_id == principal.workspace.id,
                Conversation.conv_key == conv_key,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise NotFound("conversation", f"conversation '{conv_key}' not found")
    return conv


async def _members(
    session: AsyncSession, conversation_id: int
) -> list[ConversationMember]:
    return list(
        (
            await session.execute(
                select(ConversationMember).where(
                    ConversationMember.conversation_id == conversation_id
                )
            )
        ).scalars()
    )


def _conversation_out(conv: Conversation, members: list[ConversationMember]) -> dict[str, Any]:
    return {
        "conv_key": conv.conv_key,
        "kind": conv.kind,
        "title": conv.title,
        "created_by": conv.created_by,
        "created_at": conv.created_at.isoformat(),
        "members": [
            {
                "ref": m.member_ref,
                "role": m.role,
                "muted": m.muted,
                "blocked": m.blocked,
                "last_read_seq": m.last_read_seq,
                "last_read_at": m.last_read_at.isoformat() if m.last_read_at else None,
            }
            for m in members
        ],
    }


async def _get_or_create_direct_conv(
    session: AsyncSession, principal: Principal, me: str, peer: str
) -> Conversation:
    key = _direct_key(me, peer)
    conv = (
        await session.execute(
            select(Conversation).where(
                Conversation.workspace_id == principal.workspace.id,
                Conversation.conv_key == key,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            workspace_id=principal.workspace.id,
            conv_key=key,
            kind=DIRECT_KIND,
            created_by=me,
            created_at=_now(),
        )
        session.add(conv)
        await session.flush()
        for ref in (me, peer):
            session.add(
                ConversationMember(
                    workspace_id=principal.workspace.id,
                    conversation_id=conv.id,
                    member_ref=ref,
                    role="member",
                    joined_at=_now(),
                )
            )
        await session.flush()
    return conv


async def get_or_create_direct(
    session: AsyncSession, principal: Principal, peer_ref: str
) -> dict[str, Any]:
    """Resolve (or create) the 1:1 conversation between the caller and peer."""
    require_permission(principal, "state:write")
    peer = (peer_ref or "").strip()
    if not peer:
        raise ApiError(422, "validation_error", "peer_ref is required for a direct message")
    me = await _sender_ref(session, principal)
    if peer == me:
        raise ApiError(422, "validation_error", "cannot message yourself")
    peer = await _resolve_agent_ref(session, principal.workspace.id, peer)
    conv = await _get_or_create_direct_conv(session, principal, me, peer)
    members = await _members(session, conv.id)
    await session.commit()
    return _conversation_out(conv, members)


async def send_message(
    session: AsyncSession,
    principal: Principal,
    *,
    body: str,
    peer_ref: str | None = None,
    conv_key: str | None = None,
) -> dict[str, Any]:
    """Send a message. Emits the outbox event that pushes it live and stores
    it in the inbox fallback, then records per-recipient delivery receipts."""
    require_permission(principal, "state:write")
    text_body = (body or "").rstrip()
    if not text_body:
        raise ApiError(422, "validation_error", "body must not be empty")
    if len(text_body) > 8192:
        raise ApiError(422, "validation_error", "body is too long (max 8192)")

    me = await _sender_ref(session, principal)

    if conv_key:
        conv = await _conversation_by_key(session, principal, conv_key)
    elif peer_ref:
        peer = (peer_ref or "").strip()
        if not peer:
            raise ApiError(422, "validation_error", "peer_ref is required for a direct message")
        if peer == me:
            raise ApiError(422, "validation_error", "cannot message yourself")
        peer = await _resolve_agent_ref(session, principal.workspace.id, peer)
        conv = await _get_or_create_direct_conv(session, principal, me, peer)
    else:
        raise ApiError(422, "validation_error", "conv_key or peer_ref is required")

    members = await _members(session, conv.id)
    me_row = next((m for m in members if m.member_ref == me), None)
    if me_row is None:
        raise Forbidden("you are not a member of this conversation")
    if me_row.blocked:
        raise Forbidden("you are blocked in this conversation")

    recipients = [m.member_ref for m in members if m.member_ref != me]
    if not recipients:
        raise ApiError(422, "validation_error", "conversation has no other members")

    # Gap-free per-conversation seq: lock the conversation row.
    await session.execute(
        select(Conversation.id)
        .where(Conversation.id == conv.id)
        .with_for_update()
    )
    next_seq = int(
        await session.scalar(
            select(func.coalesce(func.max(Message.seq), 0) + 1).where(
                Message.conversation_id == conv.id
            )
        )
        or 1
    )

    message = Message(
        workspace_id=principal.workspace.id,
        conversation_id=conv.id,
        seq=next_seq,
        sender_ref=me,
        body=text_body,
        created_at=_now(),
    )
    session.add(message)
    await session.flush()

    # Outbox event: stream is the primary push, inbox the fallback/record.
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type=MESSAGE_EVENT,
        payload={
            "targets": recipients,
            "conversation": conv.conv_key,
            "seq": next_seq,
            "sender": me,
            "body": text_body,
            "details": {
                "delivery_kind": "message.sent",
                "priority": 1,
                "conv_key": conv.conv_key,
                "seq": next_seq,
            },
        },
        namespace="default",
    )

    # Inbox fallback is committed with the outbox event: mark it delivered now.
    # Stream delivery is recorded by the inbox stream when it pushes the frame.
    for recipient in recipients:
        session.add(
            MessageDelivery(
                workspace_id=principal.workspace.id,
                message_id=message.id,
                recipient=recipient,
                channel="inbox",
                status="delivered",
                detail="inbox fallback",
                at=_now(),
            )
        )

    await session.commit()
    return {
        "conv_key": conv.conv_key,
        "seq": next_seq,
        "sender": me,
        "body": text_body,
        "created_at": message.created_at.isoformat(),
        "recipients": recipients,
    }


async def list_conversations(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    require_permission(principal, "state:read")
    me = await _sender_ref(session, principal)
    rows = (
        await session.execute(
            text(
                "SELECT c.conv_key FROM conversations c "
                "JOIN conversation_members m ON m.conversation_id = c.id "
                "WHERE c.workspace_id = :ws AND m.member_ref = :me "
                "ORDER BY c.created_at"
            ),
            {"ws": principal.workspace.id, "me": me},
        )
    ).scalars().all()
    items = []
    for conv_key in rows:
        conv = await _conversation_by_key(session, principal, conv_key)
        members = await _members(session, conv.id)
        items.append(_conversation_out(conv, members))
    return {"items": items}


async def list_messages(
    session: AsyncSession,
    principal: Principal,
    conv_key: str,
    *,
    before_seq: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    require_permission(principal, "state:read")
    conv = await _conversation_by_key(session, principal, conv_key)
    me = await _sender_ref(session, principal)
    if await _member(session, conv.id, me) is None:
        raise Forbidden("you are not a member of this conversation")
    limit = max(1, min(limit, MESSAGE_LIMIT))
    query = select(Message).where(Message.conversation_id == conv.id)
    if before_seq is not None:
        query = query.where(Message.seq < before_seq)
    rows = (
        await session.execute(query.order_by(Message.seq.desc()).limit(limit))
    ).scalars().all()
    return {
        "conv_key": conv.conv_key,
        "items": [
            {
                "seq": m.seq,
                "sender": m.sender_ref,
                "body": m.body,
                "created_at": m.created_at.isoformat(),
            }
            for m in sorted(rows, key=lambda m: m.seq)
        ],
    }


async def mark_read(
    session: AsyncSession, principal: Principal, conv_key: str, up_to_seq: int
) -> dict[str, Any]:
    """Advance the caller's read watermark for a conversation (M-07)."""
    require_permission(principal, "state:write")
    conv = await _conversation_by_key(session, principal, conv_key)
    me = await _sender_ref(session, principal)
    member = await _member(session, conv.id, me)
    if member is None:
        raise Forbidden("you are not a member of this conversation")
    head = int(
        await session.scalar(
            select(func.coalesce(func.max(Message.seq), 0)).where(
                Message.conversation_id == conv.id
            )
        )
        or 0
    )
    target = max(member.last_read_seq, min(int(up_to_seq), head))
    if target > member.last_read_seq:
        member.last_read_seq = target
        member.last_read_at = _now()
    await session.commit()
    return {
        "conv_key": conv.conv_key,
        "last_read_seq": member.last_read_seq,
        "last_read_at": member.last_read_at.isoformat() if member.last_read_at else None,
        "head_seq": head,
    }


async def message_receipts(
    session: AsyncSession, principal: Principal, *, conv_key: str, seq: int
) -> dict[str, Any]:
    """Sent / delivered / read for one message, per recipient (M-07)."""
    require_permission(principal, "state:read")
    conv = await _conversation_by_key(session, principal, conv_key)
    message = (
        await session.execute(
            select(Message).where(
                Message.conversation_id == conv.id, Message.seq == int(seq)
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise NotFound("message", f"message {conv_key}#{seq} not found")
    members = await _members(session, conv.id)
    deliveries = (
        await session.execute(
            select(MessageDelivery).where(MessageDelivery.message_id == message.id)
        )
    ).scalars().all()
    out = {
        "conv_key": conv.conv_key,
        "seq": message.seq,
        "sent": {
            "at": message.created_at.isoformat(),
            "by": message.sender_ref,
        },
        "recipients": [],
    }
    for member in members:
        if member.member_ref == message.sender_ref:
            continue
        delivered = [d for d in deliveries if d.recipient == member.member_ref]
        delivered_at = min((d.at for d in delivered), default=None)
        read = member.last_read_seq >= message.seq
        out["recipients"].append(
            {
                "ref": member.member_ref,
                "delivered": {
                    "at": delivered_at.isoformat() if delivered_at else None,
                    "channels": sorted({d.channel for d in delivered}),
                },
                "read": {
                    "at": member.last_read_at.isoformat() if read and member.last_read_at else None,
                    "via": "mark_read" if read else None,
                },
            }
        )
    return out
