"""Write conflicts (2026-10-24 playtest bug 17, R5): what a 409 tells you, section
patches that merge, and errors that say "resend with these flags" instead of
"give up".
"""

from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration

BODY = "# Plan\n\n## Goals\n\nship it\n\n## Risks\n\nnone yet\n"


async def _agent(client, admin_headers, workspace, role="EXECUTOR"):
    signature = f"w_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": role, "allowed_namespaces": ["*"]},
    )
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


async def test_hidden_document_says_resend_not_give_up(client, admin_headers, workspace):
    writer = await _agent(client, admin_headers, workspace)
    reader = await _agent(client, admin_headers, workspace)
    key = f"plans/hidden-{uuid.uuid4().hex[:6]}"
    made = await client.put(f"/v1/documents/{key}", headers=writer, json={
        "namespace": "plans", "content": "draft", "expected_version": 0})
    assert made.status_code in (200, 201), made.text

    hidden = await client.post("/v1/dereference", headers=reader, json={"uri": f"doc://{key}"})
    assert hidden.status_code == 404
    error = hidden.json()["error"]
    assert error["code"] == "not_found" and error["details"]["retry_with"]
    assert error["class"] == "fix_request" and error["action"] == "resend_with_retry_with"
    # A plain miss still says give up.
    missing = await client.post("/v1/dereference", headers=reader,
                                json={"uri": "doc://plans/never-written"})
    assert missing.json()["error"]["class"] == "abandon"


async def test_section_patches_merge_unless_the_section_changed(
        client, admin_headers, workspace, api_base_url):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    first = await _agent(client, admin_headers, workspace)
    second = await _agent(client, admin_headers, workspace)
    key = f"plans/shared-{uuid.uuid4().hex[:6]}"
    made = await client.put(f"/v1/documents/{key}", headers=first, json={
        "namespace": "plans", "content": BODY, "expected_version": 0})
    assert made.status_code in (200, 201), made.text
    # The first agent changes Risks: version 2.
    risks = await client.post(f"/v1/documents/{key}/patch-section", headers=first, json={
        "section_heading": "Risks", "new_markdown": "the lease may expire",
        "expected_version": 1})
    assert risks.status_code == 200, risks.text

    # REST keeps its default: a stale version is refused outright.
    stale = await client.post(f"/v1/documents/{key}/patch-section", headers=second, json={
        "section_heading": "Goals", "new_markdown": "ship it today", "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"

    async with httpx.AsyncClient(headers=second, timeout=60) as http:
        async with streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (
                read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # The second agent read version 1 and edits Goals: merged.
                goals = await session.call_tool("patch_doc_section", {
                    "doc_key": key, "section_heading": "Goals",
                    "new_markdown": "ship it today", "expected_version": 1})
                assert not goals.isError, goals.content
                assert goals.structuredContent["rebased"] is True
                assert goals.structuredContent["document"]["version"] == 3
                # Its stale view of Risks is not: the section itself changed.
                clash = await session.call_tool("patch_doc_section", {
                    "doc_key": key, "section_heading": "Risks",
                    "new_markdown": "none", "expected_version": 1})
                assert clash.isError
                error = clash.structuredContent
                assert error["code"] == "rebase_conflict"
                assert error["class"] == "refresh_and_retry"
                # rebase=false asks for the strict check.
                strict = await session.call_tool("patch_doc_section", {
                    "doc_key": key, "section_heading": "Goals", "new_markdown": "x",
                    "expected_version": 1, "rebase": False})
                assert strict.isError
                assert strict.structuredContent["code"] == "version_conflict"

    final = (await client.get(f"/v1/documents/{key}", headers=first,
                              params={"include_unverified": "true"})).json()
    assert "ship it today" in final["content"] and "the lease may expire" in final["content"]
