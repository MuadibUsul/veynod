"""Public share-link acceptance (v3.40): public access, expiry, latest version,
isolation, anti-copy rendering, revocation and access logging."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.database import async_session_factory
from app.models import ShareLink

pytestmark = pytest.mark.integration


async def _put(client, headers, key: str, content: str, *, title: str | None = None,
               expected_version: int = 0) -> dict:
    body = {"content": content, "expected_version": expected_version}
    if title:
        body["title"] = title
    response = await client.put(f"/v1/documents/{key}", headers=headers, json=body)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def test_public_share_link_follows_latest_version_and_revokes(client, workspace):
    await _put(client, workspace["headers"], "share-source",
               "# Secret Plan\n\nthe first body", title="Secret Plan")
    created = await client.post(
        "/v1/documents/share-source/share-links", headers=workspace["headers"],
        json={"expiry": "24h"},
    )
    assert created.status_code == 201, created.text
    link = created.json()
    token = link["token"]
    assert token and link["url"].endswith(f"/s/{token}")
    assert link["expires_at"] is not None and link["permanent"] is False

    # A-01: no authentication required; the HTML has only the image, not markdown.
    page = await client.get(f"/s/{token}")
    assert page.status_code == 200, page.text
    html = page.text
    assert "<img" in html and f"/s/{token}/image" in html
    assert "the first body" not in html and "Secret Plan" in html
    assert "user-select:none" in html and "contextmenu" in html
    assert workspace["workspace"]["id"] not in html

    # A-07: the server-rendered image carries the document and a watermark.
    image = await client.get(f"/s/{token}/image")
    assert image.status_code == 200, image.text
    assert image.headers["content-type"].startswith("image/svg+xml")
    svg = image.text
    assert "the first body" in svg and "Veynod" in svg and "opacity=\"0.16\"" in svg

    # A-05: the same link resolves the current document version, not a snapshot.
    await _put(client, workspace["headers"], "share-source",
               "# Secret Plan\n\nthe second body", expected_version=1)
    updated = await client.get(f"/s/{token}/image")
    assert updated.status_code == 200
    assert "the second body" in updated.text

    # A-06: no other document or workspace metadata leaks into the page.
    await _put(client, workspace["headers"], "other-doc", "do not leak this")
    assert "do not leak this" not in html and "other-doc" not in html
    bad = await client.get(f"/s/{token}x")
    assert bad.status_code == 404

    # A-08: access is logged, revocation is immediate, and the log is queryable.
    accesses = await client.get(
        f"/v1/share-links/{link['id']}/accesses", headers=workspace["headers"]
    )
    assert accesses.status_code == 200, accesses.text
    assert accesses.json()["count"] >= 1
    revoked = await client.delete(
        f"/v1/share-links/{link['id']}", headers=workspace["headers"]
    )
    assert revoked.status_code == 200, revoked.text
    after = await client.get(f"/s/{token}")
    assert after.status_code == 410
    assert "no longer available" in after.text
    after_log = await client.get(
        f"/v1/share-links/{link['id']}/accesses", headers=workspace["headers"]
    )
    assert after_log.json()["count"] > accesses.json()["count"]


async def test_share_link_expiry_tiers_custom_validation_and_permanent(client, workspace):
    await _put(client, workspace["headers"], "tier-source", "# T\n\nbody")
    created_links = {}
    for expiry in ("24h", "7d", "30d", "permanent"):
        response = await client.post(
            "/v1/documents/tier-source/share-links", headers=workspace["headers"],
            json={"expiry": expiry},
        )
        assert response.status_code == 201, response.text
        created_links[expiry] = response.json()
    now = datetime.now(timezone.utc)
    for expiry, hours in (("24h", 24), ("7d", 168), ("30d", 720)):
        expires = datetime.fromisoformat(created_links[expiry]["expires_at"].replace("Z", "+00:00"))
        assert timedelta(hours=hours - 1) <= expires - now <= timedelta(hours=hours + 1)
    assert created_links["permanent"]["expires_at"] is None
    assert created_links["permanent"]["permanent"] is True

    custom = await client.post(
        "/v1/documents/tier-source/share-links", headers=workspace["headers"],
        json={"expiry": "custom", "custom_hours": 2},
    )
    assert custom.status_code == 201, custom.text
    custom_days = await client.post(
        "/v1/documents/tier-source/share-links", headers=workspace["headers"],
        json={"expiry": "custom", "custom_days": 2},
    )
    assert custom_days.status_code == 201, custom_days.text
    invalid = await client.post(
        "/v1/documents/tier-source/share-links", headers=workspace["headers"],
        json={"expiry": "custom"},
    )
    assert invalid.status_code == 422
    invalid_negative = await client.post(
        "/v1/documents/tier-source/share-links", headers=workspace["headers"],
        json={"expiry": "custom", "custom_hours": -1},
    )
    assert invalid_negative.status_code == 422

    # A-02: force an expiry and confirm the page has no document body.
    token = created_links["24h"]["token"]
    async with async_session_factory() as session:
        await session.execute(
            update(ShareLink).where(ShareLink.id == uuid.UUID(created_links["24h"]["id"]))
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await session.commit()
    expired = await client.get(f"/s/{token}")
    assert expired.status_code == 410
    assert "expired" in expired.text.lower()
    assert "the first body" not in expired.text
