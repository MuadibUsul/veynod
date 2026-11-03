"""First-class blob storage (v3.40).

Blobs are content-addressed binary objects, separate from the document text
table.  Documents refer to them through ``document_blobs``; the bytes never
enter embeddings, semantic search, the document version chain, or taint
propagation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, ensure_namespace, require_permission
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.models import (
    Blob,
    BlobUpload,
    BlobUploadPart,
    CleanupGrant,
    DocumentBlob,
    SharedDocument,
)
from app.services import addresses as address_service
from app.services.common import append_event

settings = get_settings()
MAX_UPLOAD_PART_BYTES = 8 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise NotFound("blob", f"blob '{value}' not found") from exc


def blob_out(blob: Blob, *, include_download: bool = True) -> dict[str, Any]:
    available = blob.content is not None and blob.deleted_at is None
    return {
        "id": str(blob.id),
        "sha256": blob.sha256,
        "file_name": blob.file_name,
        "media_type": blob.media_type,
        "byte_size": blob.byte_size,
        "uploader": blob.uploader,
        "created_at": blob.created_at,
        "available": available,
        "broken": blob.deleted_at is not None,
        "download_url": f"/v1/blobs/{blob.id}" if include_download else None,
    }


def attachment_out(blob: Blob, attachment: DocumentBlob) -> dict[str, Any]:
    return {
        "blob_id": str(blob.id),
        "file_name": attachment.file_name,
        "media_type": blob.media_type,
        "byte_size": blob.byte_size,
        "sha256": blob.sha256,
        "available": blob.content is not None and blob.deleted_at is None,
        "broken": blob.deleted_at is not None,
        "attached_at": attachment.created_at,
        "attached_by": attachment.created_by,
        "download_url": f"/v1/blobs/{blob.id}",
    }


async def create_blob(
    session: AsyncSession,
    principal: Principal,
    data: bytes,
    *,
    file_name: str,
    media_type: str = "application/octet-stream",
    agent: str | None = None,
) -> Blob:
    require_permission(principal, "doc:write")
    if len(data) > settings.max_blob_bytes:
        raise ApiError(
            413,
            "blob_too_large",
            f"blob is {len(data)} bytes; the limit is {settings.max_blob_bytes}",
            details={"limit_bytes": settings.max_blob_bytes, "actual_bytes": len(data)},
        )
    digest = _sha(data)
    existing = await session.scalar(select(Blob).where(
        Blob.workspace_id == principal.workspace.id,
        Blob.sha256 == digest,
    ))
    if existing is not None and existing.content is not None and existing.deleted_at is None:
        return existing
    if existing is not None:
        # A purged content-addressed row is revived rather than duplicated.
        existing.content = data
        existing.byte_size = len(data)
        existing.file_name = file_name
        existing.media_type = media_type
        existing.deleted_at = None
        existing.deleted_by = None
        existing.delete_reason = None
        blob = existing
    else:
        blob = Blob(
            workspace_id=principal.workspace.id,
            sha256=digest,
            file_name=file_name[:512],
            media_type=media_type[:256],
            byte_size=len(data),
            content=data,
            uploader=agent or principal.agent_signature,
        )
        session.add(blob)
    await session.flush()
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent or principal.agent_signature,
        event_type="BLOB_UPLOADED",
        payload={"blob_id": str(blob.id), "sha256": digest,
                 "file_name": blob.file_name, "byte_size": blob.byte_size},
    )
    return blob


async def list_blobs(
    session: AsyncSession, principal: Principal, *, limit: int = 100
) -> dict[str, Any]:
    require_permission(principal, "doc:read")
    limit = max(1, min(limit, 500))
    rows = (await session.execute(
        select(Blob).where(Blob.workspace_id == principal.workspace.id)
        .order_by(Blob.created_at.desc()).limit(limit)
    )).scalars().all()
    total = await session.scalar(select(func.count(Blob.id)).where(
        Blob.workspace_id == principal.workspace.id))
    live = await session.scalar(select(func.coalesce(func.sum(Blob.byte_size), 0)).where(
        Blob.workspace_id == principal.workspace.id, Blob.deleted_at.is_(None)))
    return {
        "items": [blob_out(row) for row in rows],
        "count": len(rows),
        "total": int(total or 0),
        "live_bytes": int(live or 0),
    }


async def _get_blob(
    session: AsyncSession, principal: Principal, blob_id: str
) -> Blob:
    blob = await session.scalar(select(Blob).where(
        Blob.id == _uuid(blob_id),
        Blob.workspace_id == principal.workspace.id,
    ))
    if blob is None:
        raise NotFound("blob", f"blob '{blob_id}' not found")
    return blob


async def read_blob(
    session: AsyncSession, principal: Principal, blob_id: str
) -> Blob:
    require_permission(principal, "doc:read")
    blob = await _get_blob(session, principal, blob_id)
    if blob.deleted_at is not None or blob.content is None:
        raise ApiError(
            410,
            "blob_purged",
            "this blob was purged; the reference is now broken",
            details={"blob_id": str(blob.id), "broken": True},
        )
    return blob


async def delete_blob(
    session: AsyncSession,
    principal: Principal,
    blob_id: str,
    *,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    require_permission(principal, "doc:write")
    blob = await _get_blob(session, principal, blob_id)
    refs = (await session.execute(
        select(SharedDocument.doc_key)
        .join(DocumentBlob, DocumentBlob.document_id == SharedDocument.id)
        .where(DocumentBlob.blob_id == blob.id)
        .order_by(SharedDocument.doc_key)
    )).scalars().all()
    if refs and not force:
        raise ApiError(
            409,
            "blob_referenced",
            "blob is referenced by documents; detach it or force-delete as owner",
            details={"blob_id": str(blob.id), "documents": list(refs)},
        )
    if force and not principal.is_commander:
        raise Forbidden("only a workspace owner or commander can force-delete a blob")
    if force and not (reason or "").strip():
        raise ApiError(422, "validation_error", "force delete needs a reason",
                       details={"field": "reason"})
    if not principal.is_commander and blob.uploader != principal.agent_signature:
        raise Forbidden("only the uploader or a workspace commander can delete this blob")
    recoverable_until = await _recoverable_until(session, principal)
    if recoverable_until is None:
        blob.content = None
    blob.deleted_at = _utcnow()
    blob.deleted_by = principal.agent_signature
    blob.delete_reason = reason
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="BLOB_PURGED" if force else "BLOB_DELETED",
        payload={"blob_id": str(blob.id), "sha256": blob.sha256,
                 "file_name": blob.file_name, "force": force,
                 "references": list(refs), "reason": reason,
                 **({"recoverable_until": recoverable_until.isoformat()}
                    if recoverable_until else {})},
    )
    await session.commit()
    out: dict[str, Any] = {"blob": blob_out(blob), "references": list(refs), "force": force}
    if recoverable_until is not None:
        out["recoverable_until"] = recoverable_until.isoformat()
    return out


async def _recoverable_until(session: AsyncSession, principal: Principal) -> datetime | None:
    """Loop A-06: under semi or full automation nobody approves an agent's delete,
    so its bytes stay restorable for the retention window instead of going now."""
    if principal.is_human:
        return None
    from app.services import automation

    if await automation.current_mode(session, principal.workspace.id) == "manual":
        return None
    return _utcnow() + timedelta(days=settings.blob_delete_retention_days)


async def restore_blob(session: AsyncSession, principal: Principal, blob_id: str
                       ) -> dict[str, Any]:
    """Undo a recoverable delete while its bytes are still kept."""
    require_permission(principal, "doc:write")
    blob = await _get_blob(session, principal, blob_id)
    if blob.deleted_at is None:
        raise ApiError(409, "blob_not_deleted", "this blob is not deleted",
                       details={"blob_id": str(blob.id)})
    if blob.content is None:
        raise ApiError(409, "blob_not_recoverable",
                       "this blob's bytes are gone; upload the file again",
                       details={"blob_id": str(blob.id)})
    if not (principal.is_human or principal.is_commander
            or blob.deleted_by == principal.agent_signature):
        raise Forbidden("only a person, a commander or whoever deleted it can restore a blob")
    deleted_by = blob.deleted_by
    blob.deleted_at = None
    blob.deleted_by = None
    blob.delete_reason = None
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="BLOB_RESTORED",
        payload={"blob_id": str(blob.id), "sha256": blob.sha256,
                 "file_name": blob.file_name, "deleted_by": deleted_by},
    )
    await session.commit()
    return {"blob": blob_out(blob), "restored": True}


async def _document(
    session: AsyncSession, principal: Principal, ref: str
) -> SharedDocument:
    return await address_service.resolve_document(
        session, principal.workspace.id, ref
    )


async def attach_blob(
    session: AsyncSession,
    principal: Principal,
    document_ref: str,
    blob_id: str,
    *,
    file_name: str | None = None,
) -> dict[str, Any]:
    require_permission(principal, "doc:write")
    document = await _document(session, principal, document_ref)
    from app.services.documents import _ensure_visible

    await _ensure_visible(session, principal, document, include_unverified=False,
                          include_tainted=False)
    ensure_namespace(principal, document.namespace, write=True)
    blob = await _get_blob(session, principal, blob_id)
    if blob.deleted_at is not None or blob.content is None:
        raise ApiError(409, "blob_purged", "cannot attach a purged blob",
                       details={"blob_id": str(blob.id)})
    existing = await session.scalar(select(DocumentBlob).where(
        DocumentBlob.document_id == document.id,
        DocumentBlob.blob_id == blob.id,
    ))
    if existing is None:
        existing = DocumentBlob(
            document_id=document.id,
            blob_id=blob.id,
            workspace_id=principal.workspace.id,
            file_name=(file_name or blob.file_name)[:512],
            created_by=principal.agent_signature,
        )
        session.add(existing)
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=principal.agent_signature,
            event_type="BLOB_ATTACHED",
            payload={"doc_key": document.doc_key, "blob_id": str(blob.id),
                     "file_name": existing.file_name},
            namespace=document.namespace,
        )
        await session.commit()
    return {"document": document.doc_key, "attachment": attachment_out(blob, existing)}


async def detach_blob(
    session: AsyncSession,
    principal: Principal,
    document_ref: str,
    blob_id: str,
) -> dict[str, Any]:
    require_permission(principal, "doc:write")
    document = await _document(session, principal, document_ref)
    from app.services.documents import _ensure_visible

    await _ensure_visible(session, principal, document, include_unverified=False,
                          include_tainted=False)
    ensure_namespace(principal, document.namespace, write=True)
    attachment = await session.scalar(select(DocumentBlob).where(
        DocumentBlob.document_id == document.id,
        DocumentBlob.blob_id == _uuid(blob_id),
    ))
    if attachment is None:
        raise NotFound("blob attachment", f"blob '{blob_id}' is not attached")
    await session.delete(attachment)
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="BLOB_DETACHED",
        payload={"doc_key": document.doc_key, "blob_id": str(attachment.blob_id)},
        namespace=document.namespace,
    )
    await session.commit()
    return {"document": document.doc_key, "blob_id": str(attachment.blob_id)}


async def document_blobs(
    session: AsyncSession, principal: Principal, document_ref: str
) -> dict[str, Any]:
    require_permission(principal, "doc:read")
    document = await _document(session, principal, document_ref)
    from app.services.documents import _ensure_visible

    await _ensure_visible(session, principal, document, include_unverified=False,
                          include_tainted=False)
    rows = (await session.execute(
        select(DocumentBlob, Blob)
        .join(Blob, Blob.id == DocumentBlob.blob_id)
        .where(DocumentBlob.document_id == document.id)
        .order_by(DocumentBlob.created_at)
    )).all()
    return {
        "document": document.doc_key,
        "items": [attachment_out(blob, attachment) for attachment, blob in rows],
        "count": len(rows),
    }


# ---------------------------------------------------------------- uploads
async def start_upload(
    session: AsyncSession,
    principal: Principal,
    *,
    file_name: str,
    media_type: str = "application/octet-stream",
    total_parts: int | None = None,
) -> BlobUpload:
    require_permission(principal, "doc:write")
    upload = BlobUpload(
        workspace_id=principal.workspace.id,
        uploader=principal.agent_signature,
        file_name=file_name[:512],
        media_type=media_type[:256],
        total_parts=total_parts,
        expires_at=_utcnow() + timedelta(hours=settings.blob_upload_ttl_hours),
    )
    session.add(upload)
    await session.commit()
    await session.refresh(upload)
    return upload


async def put_upload_part(
    session: AsyncSession,
    principal: Principal,
    upload_id: str,
    part_index: int,
    data: bytes,
) -> dict[str, Any]:
    require_permission(principal, "doc:write")
    if part_index < 0:
        raise ApiError(422, "validation_error", "part_index must be nonnegative")
    if len(data) > min(MAX_UPLOAD_PART_BYTES, settings.max_blob_bytes):
        raise ApiError(413, "payload_too_large", "upload part exceeds the size limit")
    upload = await session.scalar(select(BlobUpload).where(
        BlobUpload.id == _uuid(upload_id),
        BlobUpload.workspace_id == principal.workspace.id,
        BlobUpload.uploader == principal.agent_signature,
    ).with_for_update())
    if upload is None:
        raise NotFound("blob upload", f"upload '{upload_id}' not found")
    if upload.expires_at < _utcnow():
        raise ApiError(410, "upload_expired", "this upload session expired")
    if upload.total_parts is not None and part_index >= upload.total_parts:
        raise ApiError(422, "validation_error", "part_index is outside total_parts",
                       details={"part_index": part_index, "total_parts": upload.total_parts})
    existing = await session.scalar(select(BlobUploadPart).where(
        BlobUploadPart.upload_id == upload.id,
        BlobUploadPart.part_index == part_index,
    ))
    stored = await session.scalar(select(func.coalesce(func.sum(BlobUploadPart.byte_size), 0))
                                  .where(BlobUploadPart.upload_id == upload.id))
    new_size = int(stored or 0) - (existing.byte_size if existing else 0) + len(data)
    if new_size > settings.max_blob_bytes:
        raise ApiError(413, "payload_too_large", "upload exceeds the blob size limit")
    if existing is None:
        existing = BlobUploadPart(
            upload_id=upload.id, part_index=part_index, content=data,
            byte_size=len(data),
        )
        session.add(existing)
    else:
        existing.content = data
        existing.byte_size = len(data)
    await session.commit()
    return {"upload_id": str(upload.id), "part_index": part_index,
            "byte_size": len(data), "sha256": _sha(data)}


async def complete_upload(
    session: AsyncSession, principal: Principal, upload_id: str
) -> Blob:
    require_permission(principal, "doc:write")
    upload = await session.scalar(select(BlobUpload).where(
        BlobUpload.id == _uuid(upload_id),
        BlobUpload.workspace_id == principal.workspace.id,
        BlobUpload.uploader == principal.agent_signature,
    ).with_for_update())
    if upload is None:
        raise NotFound("blob upload", f"upload '{upload_id}' not found")
    if upload.expires_at < _utcnow():
        raise ApiError(410, "upload_expired", "this upload session expired")
    parts = (await session.execute(
        select(BlobUploadPart).where(BlobUploadPart.upload_id == upload.id)
        .order_by(BlobUploadPart.part_index)
    )).scalars().all()
    if not parts:
        raise ApiError(422, "validation_error", "upload has no parts")
    if any(part.part_index != index for index, part in enumerate(parts)):
        raise ApiError(409, "upload_incomplete", "upload parts must be contiguous")
    if upload.total_parts is not None and len(parts) != upload.total_parts:
        raise ApiError(409, "upload_incomplete",
                       f"expected {upload.total_parts} parts, got {len(parts)}",
                       details={"upload_id": str(upload.id),
                                "missing": upload.total_parts - len(parts)})
    if sum(part.byte_size for part in parts) > settings.max_blob_bytes:
        raise ApiError(413, "payload_too_large", "upload exceeds the blob size limit")
    data = b"".join(part.content for part in parts)
    blob = await create_blob(
        session, principal, data,
        file_name=upload.file_name, media_type=upload.media_type,
    )
    await session.delete(upload)
    await session.commit()
    return blob


async def cancel_upload(
    session: AsyncSession, principal: Principal, upload_id: str
) -> dict[str, Any]:
    require_permission(principal, "doc:write")
    upload = await session.scalar(select(BlobUpload).where(
        BlobUpload.id == _uuid(upload_id),
        BlobUpload.workspace_id == principal.workspace.id,
        BlobUpload.uploader == principal.agent_signature,
    ))
    if upload is None:
        raise NotFound("blob upload", f"upload '{upload_id}' not found")
    await session.delete(upload)
    await session.commit()
    return {"upload_id": str(upload.id), "cancelled": True}


# ---------------------------------------------------------------- lifecycle
async def gc_blobs(session: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """Purge unreferenced blobs older than the configured TTL."""
    cutoff = (now or _utcnow()) - timedelta(days=settings.blob_unreferenced_ttl_days)
    rows = (await session.execute(
        select(Blob).where(
            Blob.deleted_at.is_(None),
            Blob.created_at <= cutoff,
            ~select(DocumentBlob.blob_id).where(DocumentBlob.blob_id == Blob.id).exists(),
        )
    )).scalars().all()
    reclaimed = 0
    for blob in rows:
        blob.content = None
        blob.deleted_at = _utcnow()
        blob.deleted_by = "blob_gc"
        blob.delete_reason = "unreferenced TTL"
        reclaimed += blob.byte_size
    # Recoverable deletes (Loop A-06) whose retention window has passed.
    kept_until = (now or _utcnow()) - timedelta(days=settings.blob_delete_retention_days)
    expired = (await session.execute(
        select(Blob).where(Blob.deleted_at.is_not(None), Blob.deleted_at <= kept_until,
                           Blob.content.is_not(None))
    )).scalars().all()
    for blob in expired:
        blob.content = None
        reclaimed += blob.byte_size
    expired_uploads = (await session.execute(
        select(BlobUpload).where(BlobUpload.expires_at <= (now or _utcnow()))
        .limit(1000).with_for_update(skip_locked=True)
    )).scalars().all()
    for upload in expired_uploads:
        await session.delete(upload)
    by_workspace: dict[Any, dict[str, int]] = {}
    for blob in rows:
        stats = by_workspace.setdefault(blob.workspace_id, {"count": 0, "reclaimed_bytes": 0})
        stats["count"] += 1
        stats["reclaimed_bytes"] += blob.byte_size
    for workspace_id, stats in by_workspace.items():
        await append_event(
            session,
            workspace_id=workspace_id,
            agent="blob_gc",
            event_type="BLOB_GC",
            payload=stats,
        )
    if rows or expired or expired_uploads:
        await session.commit()
    return {"purged": len(rows), "expired_deletes": len(expired),
            "expired_uploads": len(expired_uploads), "reclaimed_bytes": reclaimed}


async def create_cleanup_grant(
    session: AsyncSession,
    principal: Principal,
    *,
    grantee: str,
    scope_namespace: str | None,
    doc_key_prefix: str | None,
    expires_hours: int,
) -> CleanupGrant:
    if not principal.is_commander:
        raise Forbidden("only a workspace owner or commander can delegate cleanup")
    if expires_hours <= 0:
        raise ApiError(422, "validation_error", "expires_hours must be positive",
                       details={"field": "expires_hours"})
    grant = CleanupGrant(
        workspace_id=principal.workspace.id,
        grantee=grantee[:160],
        scope_namespace=scope_namespace,
        doc_key_prefix=doc_key_prefix,
        created_by=principal.agent_signature,
        expires_at=_utcnow() + timedelta(hours=expires_hours),
    )
    session.add(grant)
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="CLEANUP_GRANT_CREATED",
        payload={"grant_id": str(grant.id), "grantee": grant.grantee,
                 "namespace": scope_namespace, "prefix": doc_key_prefix},
    )
    await session.commit()
    await session.refresh(grant)
    return grant


async def list_cleanup_grants(
    session: AsyncSession, principal: Principal
) -> dict[str, Any]:
    if not principal.is_commander:
        raise Forbidden("only a workspace owner or commander can list cleanup grants")
    rows = (await session.execute(
        select(CleanupGrant).where(CleanupGrant.workspace_id == principal.workspace.id)
        .order_by(CleanupGrant.created_at.desc())
    )).scalars().all()
    return {"items": [
        {"id": str(row.id), "grantee": row.grantee, "namespace": row.scope_namespace,
         "doc_key_prefix": row.doc_key_prefix, "created_by": row.created_by,
         "created_at": row.created_at, "expires_at": row.expires_at,
         "revoked_at": row.revoked_at, "revoked_by": row.revoked_by}
        for row in rows
    ]}


async def revoke_cleanup_grant(
    session: AsyncSession, principal: Principal, grant_id: str
) -> dict[str, Any]:
    if not principal.is_commander:
        raise Forbidden("only a workspace owner or commander can revoke cleanup grants")
    grant = await session.scalar(select(CleanupGrant).where(
        CleanupGrant.id == _uuid(grant_id),
        CleanupGrant.workspace_id == principal.workspace.id,
    ))
    if grant is None:
        raise NotFound("cleanup grant", f"grant '{grant_id}' not found")
    if grant.revoked_at is None:
        grant.revoked_at = _utcnow()
        grant.revoked_by = principal.agent_signature
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=principal.agent_signature,
            event_type="CLEANUP_GRANT_REVOKED",
            payload={"grant_id": str(grant.id), "grantee": grant.grantee},
        )
        await session.commit()
    return {"id": str(grant.id), "revoked": True}


async def can_archive_with_grant(
    session: AsyncSession, principal: Principal, document: SharedDocument
) -> bool:
    if principal.is_commander or document.created_by == principal.agent_signature:
        return True
    candidates = {principal.agent_signature, f"agent:{principal.agent_signature}"}
    grant = await session.scalar(select(CleanupGrant).where(
        CleanupGrant.workspace_id == principal.workspace.id,
        CleanupGrant.grantee.in_(candidates),
        CleanupGrant.revoked_at.is_(None),
        CleanupGrant.expires_at > _utcnow(),
    ).order_by(CleanupGrant.created_at.desc()))
    if grant is None:
        return False
    if grant.scope_namespace and grant.scope_namespace != document.namespace:
        return False
    if grant.doc_key_prefix and not document.doc_key.startswith(grant.doc_key_prefix):
        return False
    return True


# ---------------------------------------------------------------- admission
def likely_base64(content: str) -> bool:
    """Detect a base64 payload that should use the blob endpoint instead.

    Prose with spaces removed can look like base64, so require the original
    text to have almost no whitespace as well as a base64-heavy alphabet.
    """
    if len(content.encode("utf-8")) < settings.blob_base64_min_bytes:
        return False
    non_space = [char for char in content if not char.isspace()]
    if not non_space:
        return False
    if len(non_space) / len(content) < 0.97:
        return False
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    ratio = sum(1 for char in non_space if char in alphabet) / len(non_space)
    return ratio > settings.blob_base64_ratio


async def check_document_admission(
    session: AsyncSession,
    principal: Principal,
    agent: str,
    content: str,
) -> None:
    if likely_base64(content):
        raise ApiError(
            422,
            "base64_document_rejected",
            "binary/base64 content must be uploaded as a blob, not stored as a document",
            details={"action": "upload_blob", "endpoint": "/v1/blobs",
                     "field": "content"},
        )
    if principal.is_human or principal.is_commander:
        return
    since = _utcnow() - timedelta(hours=24)
    count, total = (await session.execute(
        select(func.count(SharedDocument.id),
               func.coalesce(func.sum(func.octet_length(SharedDocument.content)), 0))
        .where(SharedDocument.workspace_id == principal.workspace.id,
               SharedDocument.created_by == agent,
               SharedDocument.created_at >= since)
    )).one()
    if int(count or 0) >= settings.agent_daily_document_limit:
        raise ApiError(
            429,
            "agent_document_quota_exceeded",
            "daily document creation quota reached; ask the owner for a larger quota",
            details={"limit": settings.agent_daily_document_limit, "used": int(count or 0),
                     "window_hours": 24},
        )
    if int(total or 0) >= settings.agent_daily_document_bytes:
        raise ApiError(
            429,
            "agent_document_quota_exceeded",
            "daily document byte quota reached; ask the owner for a larger quota",
            details={"limit_bytes": settings.agent_daily_document_bytes,
                     "used_bytes": int(total or 0), "window_hours": 24},
        )


# ---------------------------------------------------------------- migration
async def migrate_part_documents(
    session: AsyncSession,
    principal: Principal,
    *,
    file_name: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Reassemble base64 ``file_artifact_part`` documents into one blob.

    This is deliberately narrow: only base64 parts are handled. gzip+base64
    and ad-hoc encodings are reported and left untouched.
    """
    require_permission(principal, "doc:write")
    docs = (await session.execute(
        select(SharedDocument).where(
            SharedDocument.workspace_id == principal.workspace.id,
            SharedDocument.frontmatter["kind"].astext == "file_artifact_part",
            SharedDocument.frontmatter["file_name"].astext == file_name,
        ).order_by(SharedDocument.doc_key)
    )).scalars().all()
    if not docs:
        raise NotFound("artifact parts", f"no parts for '{file_name}'")
    parts: list[tuple[int, SharedDocument]] = []
    for doc in docs:
        meta = doc.frontmatter or {}
        if meta.get("encoding") != "base64":
            raise ApiError(
                422, "unsupported_part_encoding",
                f"part {doc.doc_key} uses {meta.get('encoding')!r}; only base64 is supported",
            )
        try:
            index = int(meta.get("part_index"))
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_part", f"part {doc.doc_key} has no part_index") from exc
        parts.append((index, doc))
    parts.sort(key=lambda item: item[0])
    expected_count = (parts[0][1].frontmatter or {}).get("part_count")
    if expected_count is not None and len(parts) != int(expected_count):
        raise ApiError(409, "parts_incomplete",
                       f"expected {expected_count} parts, found {len(parts)}",
                       details={"file_name": file_name, "found": len(parts)})
    chunks: list[bytes] = []
    for _, doc in parts:
        try:
            chunks.append(base64.b64decode("".join(doc.content.split()), validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ApiError(422, "invalid_base64_part",
                           f"part {doc.doc_key} is not valid base64") from exc
    data = b"".join(chunks)
    expected_sha = (parts[0][1].frontmatter or {}).get("file_sha256")
    actual_sha = _sha(data)
    report: dict[str, Any] = {
        "file_name": file_name,
        "parts": len(parts),
        "bytes": len(data),
        "sha256": actual_sha,
        "expected_sha256": expected_sha,
        "verified": expected_sha == actual_sha,
        "applied": False,
    }
    if not report["verified"]:
        raise ApiError(409, "sha256_mismatch",
                       "reassembled bytes do not match file_sha256",
                       details={"expected": expected_sha, "actual": actual_sha})
    if not apply:
        return report
    media_type = (parts[0][1].frontmatter or {}).get(
        "media_type", "application/octet-stream")
    blob = await create_blob(
        session, principal, data, file_name=file_name, media_type=media_type,
    )
    for _, doc in parts:
        doc.archived_at = _utcnow()
        doc.archived_by = principal.agent_signature
        doc.archive_reason = f"migrated to blob {blob.id}"
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="BLOB_MIGRATION",
        payload={"file_name": file_name, "blob_id": str(blob.id),
                 "parts": len(parts), "bytes": len(data), "sha256": actual_sha},
    )
    await session.commit()
    report.update({"applied": True, "blob": blob_out(blob),
                   "archived_documents": [doc.doc_key for _, doc in parts]})
    # Keep a human-readable migration ledger next to the parts.  Prefer the
    # project namespace when it exists; a disposable test workspace only has
    # 'default'.
    log_namespace = "agentgraph"
    known = await session.scalar(text(
        "SELECT 1 FROM namespaces WHERE workspace_id = :ws AND namespace = :ns"
    ), {"ws": principal.workspace.id, "ns": log_namespace})
    if not known:
        log_namespace = "default"
    log_key = f"{log_namespace}/blob-migration-log" if log_namespace != "default" \
        else "blob-migration-log"
    existing = await session.scalar(select(SharedDocument).where(
        SharedDocument.workspace_id == principal.workspace.id,
        SharedDocument.doc_key == log_key,
    ))
    entry = (
        f"\n## {_utcnow().isoformat()}\n\n"
        f"- file: `{file_name}`\n- parts: {len(parts)}\n"
        f"- bytes: {len(data)}\n- sha256: `{actual_sha}`\n"
        f"- blob: `{blob.id}`\n- archived documents: {len(parts)}\n"
    )
    content = (existing.content if existing else "# Blob migration log\n") + entry
    try:
        from app.schemas import DocumentUpsertRequest
        from app.services.documents import upsert_document

        await upsert_document(
            session, principal, log_key,
            DocumentUpsertRequest(
                content=content,
                title="Blob migration log",
                expected_version=existing.version if existing else 0,
                namespace=log_namespace,
            ),
        )
        report["migration_log"] = f"doc://{log_key}"
    except Exception as exc:  # noqa: BLE001 - a ledger write must not undo the migration
        report["migration_log_error"] = str(exc)
    return report


logger = logging.getLogger("agentgraph.blobs")


async def blob_gc_loop(interval_seconds: int = 3600) -> None:
    """Reclaim unreferenced blobs on a slow background cadence."""
    from app.database import async_session_factory

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with async_session_factory() as session:
                result = await gc_blobs(session)
            if result["purged"]:
                logger.info("blob GC purged %s blob(s), reclaimed %s bytes",
                            result["purged"], result["reclaimed_bytes"])
        except Exception:  # noqa: BLE001 - GC must never stop the API
            logger.exception("blob GC failed")

