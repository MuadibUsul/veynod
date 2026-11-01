"""Blob storage acceptance (v3.40): upload, download, attachments, lifecycle,
admission, cleanup grants and the 17-part artifact migration."""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.database import async_session_factory
from app.models import Blob, SharedDocument

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


async def _agent(client, workspace_id: str) -> dict:
    signature = f"blob-{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace_id}/agent-tokens",
        headers=ADMIN,
        json={"agent_signature": signature, "role_scope": "EXECUTOR",
              "allowed_namespaces": ["*"]},
    )
    assert response.status_code in (200, 201), response.text
    return {"headers": {"Authorization": f"Bearer {response.json()['api_key']}"},
            "signature": signature}


async def _put(client, headers, key: str, content: str, **extra) -> dict:
    response = await client.put(
        f"/v1/documents/{key}", headers=headers,
        json={"content": content, "expected_version": 0, **extra},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


async def test_blob_upload_download_attachment_and_force_delete(client, workspace):
    data = os.urandom(1024 * 1024)
    digest = hashlib.sha256(data).hexdigest()
    uploaded = await client.post(
        "/v1/blobs", headers=workspace["headers"], content=data,
        params={"file_name": "bundle.zip", "media_type": "application/zip"},
    )
    assert uploaded.status_code == 201, uploaded.text
    blob = uploaded.json()
    assert blob["byte_size"] == len(data) and blob["sha256"] == digest

    # A-01: binary bytes are not documents, and base64 text is not searchable.
    docs = await client.get("/v1/documents", headers=workspace["headers"])
    assert docs.status_code == 200
    assert all("bundle.zip" not in item["doc_key"] for item in docs.json()["items"])

    # A-02: the bytes and their digest survive a round trip.
    downloaded = await client.get(f"/v1/blobs/{blob['id']}",
                                  headers=workspace["headers"])
    assert downloaded.status_code == 200
    assert downloaded.content == data
    assert downloaded.headers["x-content-sha256"] == digest

    # A-03: attaching a blob does not change document trust/verification.
    await _put(client, workspace["headers"], "attachment-doc", "the body")
    before = (await client.get("/v1/documents/attachment-doc",
                               headers=workspace["headers"])).json()
    attached = await client.post(
        "/v1/documents/attachment-doc/blobs", headers=workspace["headers"],
        json={"blob_id": blob["id"], "file_name": "bundle.zip"},
    )
    assert attached.status_code == 201, attached.text
    after = (await client.get("/v1/documents/attachment-doc",
                              headers=workspace["headers"])).json()
    assert after["verification_status"] == before["verification_status"]
    assert after["tainted"] == before["tainted"]

    # A-04: referenced blobs refuse deletion; commander force-delete breaks the link.
    refused = await client.delete(f"/v1/blobs/{blob['id']}",
                                  headers=workspace["headers"])
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["documents"] == ["attachment-doc"]
    forced = await client.delete(
        f"/v1/blobs/{blob['id']}", headers=workspace["headers"],
        params={"force": "true", "reason": "owner requested purge"},
    )
    assert forced.status_code == 200, forced.text
    gone = await client.get(f"/v1/blobs/{blob['id']}", headers=workspace["headers"])
    assert gone.status_code == 410
    refs = await client.get("/v1/documents/attachment-doc/blobs",
                            headers=workspace["headers"])
    assert refs.status_code == 200
    assert refs.json()["items"][0]["broken"] is True


async def test_blob_gc_cleanup_grant_and_base64_admission(client, workspace):
    # A-05: an unreferenced blob past TTL is reclaimed.
    uploaded = await client.post(
        "/v1/blobs", headers=workspace["headers"], content=os.urandom(2048),
        params={"file_name": "stale.bin"},
    )
    assert uploaded.status_code == 201, uploaded.text
    stale_id = uploaded.json()["id"]
    async with async_session_factory() as session:
        await session.execute(
            update(Blob).where(Blob.id == uuid.UUID(stale_id)).values(
                created_at=datetime.now(timezone.utc) - timedelta(days=31)
            )
        )
        await session.commit()
    gc = await client.post("/v1/blobs/gc", headers=workspace["headers"])
    assert gc.status_code == 200, gc.text
    assert gc.json()["purged"] >= 1
    assert (await client.get(f"/v1/blobs/{stale_id}",
                             headers=workspace["headers"])).status_code == 410

    # A-06: a scoped grant lets an agent archive someone else's document, but
    # only inside the grant, and not after revocation.
    agent = await _agent(client, workspace["workspace"]["id"])
    await _put(client, workspace["headers"], "grant-victim", "remove me")
    await _put(client, workspace["headers"], "grant-other", "leave me")
    granted = await client.post(
        "/v1/cleanup-grants", headers=workspace["headers"],
        json={"grantee": f"agent:{agent['signature']}", "namespace": "default",
              "doc_key_prefix": "grant-victim", "expires_hours": 1},
    )
    assert granted.status_code == 201, granted.text
    grant_id = granted.json()["id"]
    archived = await client.post(
        "/v1/documents/grant-victim/archive", headers=agent["headers"],
        json={"reason": "delegated cleanup"},
    )
    assert archived.status_code == 200, archived.text
    outside = await client.post(
        "/v1/documents/grant-other/archive", headers=agent["headers"],
        json={"reason": "outside the grant"},
    )
    assert outside.status_code == 403
    revoked = await client.delete(f"/v1/cleanup-grants/{grant_id}",
                                  headers=workspace["headers"])
    assert revoked.status_code == 200, revoked.text
    await _put(client, workspace["headers"], "grant-victim-2", "remove me too")
    after_revoke = await client.post(
        "/v1/documents/grant-victim-2/archive", headers=agent["headers"],
        json={"reason": "grant is gone"},
    )
    assert after_revoke.status_code == 403

    # A-07: a large base64 body is redirected to the blob endpoint.
    payload = base64.b64encode(os.urandom(70_000)).decode("ascii")
    rejected = await client.put(
        "/v1/documents/base64-doc", headers=agent["headers"],
        json={"content": payload, "expected_version": 0},
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "base64_document_rejected"


async def test_seventeen_part_artifact_migrates_to_one_blob(client, workspace):
    raw = os.urandom(17 * 100)
    digest = hashlib.sha256(raw).hexdigest()
    keys = []
    async with async_session_factory() as session:
        for index in range(17):
            content = base64.b64encode(raw[index * 100:(index + 1) * 100]).decode("ascii")
            key = f"legacy-part-{index + 1:02d}"
            keys.append(key)
            session.add(SharedDocument(
                workspace_id=uuid.UUID(workspace["workspace"]["id"]),
                doc_key=key,
                title=key,
                content=content,
                frontmatter={
                    "kind": "file_artifact_part", "encoding": "base64",
                    "file_name": "legacy.zip", "file_bytes": len(raw),
                    "media_type": "application/zip", "part_count": 17,
                    "part_index": index + 1, "byte_length": 100,
                    "byte_offset": index * 100, "file_sha256": digest,
                },
                verification_status="DRAFT",
                trust_level="UNTRUSTED",
                namespace="default",
                version=1,
                created_by="fixture",
                updated_by="fixture",
            ))
        await session.commit()

    migrated = await client.post(
        "/v1/blobs/migrate-parts", headers=workspace["headers"],
        json={"file_name": "legacy.zip", "apply": True},
    )
    assert migrated.status_code == 200, migrated.text
    report = migrated.json()
    assert report["verified"] is True and report["parts"] == 17
    assert report["blob"]["sha256"] == digest
    assert len(report["archived_documents"]) == 17
    assert report["migration_log"] == "doc://blob-migration-log"
    ledger = await client.get("/v1/documents/blob-migration-log",
                              headers=workspace["headers"])
    assert ledger.status_code == 200, ledger.text
    assert "legacy.zip" in ledger.json()["content"]
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(SharedDocument).where(
                SharedDocument.workspace_id == uuid.UUID(workspace["workspace"]["id"]),
                SharedDocument.doc_key.in_(keys),
            )
        )).scalars().all()
        assert len(rows) == 17 and all(row.archived_at is not None for row in rows)
