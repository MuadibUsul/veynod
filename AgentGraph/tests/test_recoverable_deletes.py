"""Loop A-06: under automation an agent's delete can be undone.

spec agentgraph/spec-auto-loop-mode v2: irreversible operations are backed up
first and can be rolled back. With nobody approving an agent's delete (semi
or full automation), a deleted blob keeps its bytes for the retention window:
reads fail as for any deleted blob, a person or the deleter can restore it,
and the GC purges the bytes once the window has passed. Manual mode and
people's own deletes behave as before.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import update

from app.database import async_session_factory
from app.models import Blob

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


async def _workspace(client: httpx.AsyncClient, mode: str) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"undo-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    made = await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    assert made.status_code in (200, 201), made.text
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    owner = {"Authorization": f"Bearer {login.json()['token']}"}
    changed = await client.put("/v1/human/automation", headers=owner, json={
        "mode": mode, "auto_deploy_allowed": False, "reason": "test"})
    assert changed.status_code == 200, changed.text
    minted = await client.post(f"/v1/admin/workspaces/{wid}/agent-tokens", headers=ADMIN, json={
        "agent_signature": f"agent-{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR",
        "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    return {"key": {"Authorization": f"Bearer {created['api_key']}"}, "owner": owner,
            "agent": {"Authorization": f"Bearer {minted.json()['api_key']}"}}


async def _upload(client: httpx.AsyncClient, headers: dict) -> tuple[str, bytes]:
    data = os.urandom(4096)
    uploaded = await client.post("/v1/blobs", headers=headers, content=data,
                                 params={"file_name": "report.bin"})
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["id"], data


async def test_an_agents_delete_under_automation_can_be_restored(client):
    ws = await _workspace(client, "full")
    blob_id, data = await _upload(client, ws["agent"])

    deleted = await client.delete(f"/v1/blobs/{blob_id}", headers=ws["agent"])
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["recoverable_until"]
    gone = await client.get(f"/v1/blobs/{blob_id}", headers=ws["agent"])
    assert gone.status_code == 410  # deleted as far as readers are concerned

    restored = await client.post(f"/v1/blobs/{blob_id}/restore", headers=ws["owner"])
    assert restored.status_code == 200, restored.text
    back = await client.get(f"/v1/blobs/{blob_id}", headers=ws["agent"])
    assert back.status_code == 200 and back.content == data

    again = await client.post(f"/v1/blobs/{blob_id}/restore", headers=ws["owner"])
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "blob_not_deleted"


async def test_the_gc_purges_a_recoverable_delete_after_the_window(client):
    ws = await _workspace(client, "semi")
    blob_id, _ = await _upload(client, ws["agent"])
    assert (await client.delete(f"/v1/blobs/{blob_id}", headers=ws["agent"])).status_code == 200
    async with async_session_factory() as session:
        await session.execute(update(Blob).where(Blob.id == uuid.UUID(blob_id)).values(
            deleted_at=datetime.now(timezone.utc) - timedelta(days=8)))
        await session.commit()
    gc = await client.post("/v1/blobs/gc", headers=ws["key"])
    assert gc.status_code == 200, gc.text
    assert gc.json()["expired_deletes"] >= 1
    late = await client.post(f"/v1/blobs/{blob_id}/restore", headers=ws["owner"])
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "blob_not_recoverable"


async def test_manual_mode_and_peoples_deletes_are_immediate(client):
    ws = await _workspace(client, "manual")
    blob_id, _ = await _upload(client, ws["agent"])
    deleted = await client.delete(f"/v1/blobs/{blob_id}", headers=ws["agent"])
    assert deleted.status_code == 200 and "recoverable_until" not in deleted.json()
    assert (await client.post(f"/v1/blobs/{blob_id}/restore", headers=ws["owner"])
            ).json()["error"]["code"] == "blob_not_recoverable"

    full = await _workspace(client, "full")
    blob_id, _ = await _upload(client, full["owner"])
    by_owner = await client.delete(f"/v1/blobs/{blob_id}", headers=full["owner"])
    assert by_owner.status_code == 200 and "recoverable_until" not in by_owner.json()
