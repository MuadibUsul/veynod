"""Blob storage, attachments and cleanup-grant routes (v3.40)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_principal
from app.database import get_db
from app.errors import Forbidden
from app.schemas_blobs import (
    BlobAttachRequest,
    BlobMigrationRequest,
    BlobUploadCreateRequest,
    CleanupGrantRequest,
)
from app.services import blobs

router = APIRouter(prefix="/v1", tags=["blobs"])


@router.post("/blobs", status_code=201, summary="Upload a binary blob")
async def upload_blob(
    request: Request,
    file_name: str = Query(min_length=1, max_length=512),
    media_type: str = Query(default="application/octet-stream", max_length=256),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    data = await request.body()
    blob = await blobs.create_blob(
        session, principal, data, file_name=file_name, media_type=media_type
    )
    await session.commit()
    return blobs.blob_out(blob)


@router.get("/blobs", summary="List blob metadata and storage totals")
async def list_blobs(
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.list_blobs(session, principal, limit=limit)


@router.get("/blobs/{blob_id}", summary="Download a blob by id")
async def download_blob(
    blob_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    blob = await blobs.read_blob(session, principal, blob_id)
    filename = quote(blob.file_name, safe="")
    return Response(
        content=blob.content or b"",
        media_type=blob.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-SHA256": blob.sha256,
            "Cache-Control": "no-store",
        },
    )


@router.delete("/blobs/{blob_id}", summary="Delete or owner-force-delete a blob")
async def delete_blob(
    blob_id: str,
    force: bool = Query(default=False),
    reason: str | None = Query(default=None, max_length=4000),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.delete_blob(
        session, principal, blob_id, force=force, reason=reason
    )


@router.post("/blobs/{blob_id}/restore", summary="Restore a recoverable blob delete")
async def restore_blob(
    blob_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.restore_blob(session, principal, blob_id)


@router.post("/blobs/gc", summary="Run the unreferenced-blob GC (commander)")
async def gc_blobs(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    if not principal.is_commander:
        raise Forbidden("only a workspace owner or commander can run blob GC")
    return await blobs.gc_blobs(session)


@router.post("/blobs/migrate-parts", summary="Reassemble base64 part documents into a blob")
async def migrate_parts(
    body: BlobMigrationRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    if not principal.is_commander:
        raise Forbidden("only a workspace owner or commander can run artifact migration")
    return await blobs.migrate_part_documents(
        session, principal, file_name=body.file_name, apply=body.apply
    )


@router.post("/blobs/uploads", status_code=201, summary="Start a resumable blob upload")
async def start_upload(
    body: BlobUploadCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    upload = await blobs.start_upload(
        session, principal, file_name=body.file_name,
        media_type=body.media_type, total_parts=body.total_parts,
    )
    return {
        "upload_id": str(upload.id), "file_name": upload.file_name,
        "media_type": upload.media_type, "total_parts": upload.total_parts,
        "expires_at": upload.expires_at,
    }


@router.put("/blobs/uploads/{upload_id}/parts/{part_index}", summary="Upload one part")
async def put_part(
    upload_id: str,
    part_index: int,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    data = await request.body()
    return await blobs.put_upload_part(
        session, principal, upload_id, part_index, data
    )


@router.post("/blobs/uploads/{upload_id}/complete", status_code=201,
             summary="Finish a resumable upload and create the blob")
async def complete_upload(
    upload_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    blob = await blobs.complete_upload(session, principal, upload_id)
    return blobs.blob_out(blob)


@router.delete("/blobs/uploads/{upload_id}", summary="Cancel a resumable upload")
async def cancel_upload(
    upload_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.cancel_upload(session, principal, upload_id)


@router.get("/documents/{doc_key:path}/blobs", summary="List a document's blob attachments")
async def list_document_blobs(
    doc_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.document_blobs(session, principal, doc_key)


@router.post("/documents/{doc_key:path}/blobs", status_code=201,
             summary="Attach a blob to a document")
async def attach_blob(
    doc_key: str,
    body: BlobAttachRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.attach_blob(
        session, principal, doc_key, str(body.blob_id), file_name=body.file_name
    )


@router.delete("/documents/{doc_key:path}/blobs/{blob_id}",
              summary="Detach a blob from a document")
async def detach_blob(
    doc_key: str,
    blob_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.detach_blob(session, principal, doc_key, blob_id)


@router.post("/cleanup-grants", status_code=201,
             summary="Delegate scoped document cleanup to an agent")
async def create_cleanup_grant(
    body: CleanupGrantRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    grant = await blobs.create_cleanup_grant(
        session, principal, grantee=body.grantee,
        scope_namespace=body.namespace, doc_key_prefix=body.doc_key_prefix,
        expires_hours=body.expires_hours,
    )
    return {"id": str(grant.id), "grantee": grant.grantee,
            "namespace": grant.scope_namespace, "doc_key_prefix": grant.doc_key_prefix,
            "expires_at": grant.expires_at}


@router.get("/cleanup-grants", summary="List cleanup grants")
async def list_cleanup_grants(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.list_cleanup_grants(session, principal)


@router.delete("/cleanup-grants/{grant_id}", summary="Revoke a cleanup grant")
async def revoke_cleanup_grant(
    grant_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return await blobs.revoke_cleanup_grant(session, principal, grant_id)
