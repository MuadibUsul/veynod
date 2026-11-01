"""Incremental re-embedding (2026-10-24 playtest cost item, R6).

A new version of a large document embeds only the chunks whose text changed;
unchanged chunks keep their vectors. Changing the title changes every chunk's
embedded text, so everything is embedded again; so does a rebuild.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _body(sections: dict[str, str]) -> str:
    return "\n\n".join(f"## {heading}\n\n{text}" for heading, text in sections.items())


async def _chunks(doc_key: str) -> dict[str, tuple[str, str]]:
    conn = await asyncpg.connect(_db())
    try:
        rows = await conn.fetch(
            "SELECT heading, embed_hash, embedding::text AS vector FROM document_chunks "
            "WHERE doc_key = $1 ORDER BY chunk_index", doc_key)
    finally:
        await conn.close()
    return {row["heading"]: (row["embed_hash"], row["vector"]) for row in rows}


async def _write(client, headers, key, content, version, title="Runbook"):
    response = await client.put(f"/v1/documents/{key}", headers=headers, json={
        "namespace": "ops", "title": title, "content": content,
        "expected_version": version})
    assert response.status_code in (200, 201), response.text
    return response.json()


async def test_only_changed_sections_are_embedded_again(client, workspace):
    headers = workspace["headers"]
    key = f"ops/runbook-{uuid.uuid4().hex[:6]}"
    sections = {name: f"{name} " + ("step by step instructions. " * 40)
                for name in ("Deploy", "Rollback", "Incidents", "Backups")}
    first = await _write(client, headers, key, _body(sections), 0)
    full_cost = first["usage"]["embedding_tokens"]
    before = await _chunks(key)
    assert len(before) == 4 and all(hash_ for hash_, _ in before.values())

    sections["Rollback"] = "Rollback " + ("revert the release, then verify. " * 40)
    second = await _write(client, headers, key, _body(sections), 1)
    after = await _chunks(key)
    # One of four sections changed: about a quarter of the first write's cost.
    assert 0 < second["usage"]["embedding_tokens"] < full_cost / 2, (second["usage"], full_cost)
    for heading in ("Deploy", "Incidents", "Backups"):
        assert after[heading] == before[heading], heading
    assert after["Rollback"][0] != before["Rollback"][0]

    # The title is part of every chunk's embedded text: all of it is new.
    third = await _write(client, headers, key, _body(sections), 2, title="Ops runbook")
    renamed = await _chunks(key)
    assert third["usage"]["embedding_tokens"] >= full_cost * 0.9
    assert all(renamed[heading][0] != after[heading][0] for heading in renamed)


async def test_a_rebuild_embeds_everything_again(client, workspace, monkeypatch):
    from app.services import retrieval

    headers = workspace["headers"]
    key = f"ops/rebuild-{uuid.uuid4().hex[:6]}"
    await _write(client, headers, key, _body({"One": "first " * 50, "Two": "second " * 50}), 0)
    calls: list[int] = []
    provider = retrieval.get_embedding_provider()
    original = provider.embed

    async def counting(texts):
        calls.append(len(texts))
        return await original(texts)

    monkeypatch.setattr(provider, "embed", counting)
    async with _session() as session:
        from sqlalchemy import select

        from app.models import SharedDocument

        document = await session.scalar(select(SharedDocument).where(
            SharedDocument.doc_key == key))
        # A new version (default): nothing changed, nothing is embedded.
        await retrieval.reindex_document_chunks(session, document)
        assert calls == []
        # A rebuild (what the index worker sets for an indexed version): all of it.
        token = retrieval.REUSE_VECTORS.set(False)
        try:
            await retrieval.reindex_document_chunks(session, document)
        finally:
            retrieval.REUSE_VECTORS.reset(token)
        assert calls == [2]
        await session.rollback()


def _session():
    from app.database import async_session_factory

    return async_session_factory()
