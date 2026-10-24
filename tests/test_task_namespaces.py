"""Tasks land where their people can see them (2026-10-24).

The namespace is inferred when omitted, an assignee that cannot read the
namespace is refused, hidden assignments are counted, and new inbox items
ride on the next MCP tool result.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest

pytestmark = pytest.mark.integration


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _agent(client, admin_headers, workspace, namespaces, role="EXECUTOR"):
    signature = f"ns_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": role,
              "allowed_namespaces": namespaces},
    )
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}, signature


def _codes(body: dict) -> dict[str, dict]:
    return {item["code"]: item for item in body.get("warnings") or []}


async def test_namespace_is_inferred_and_defaults_are_flagged(client, admin_headers, workspace):
    owner = workspace["headers"]
    narrow, _ = await _agent(client, admin_headers, workspace, ["ideas"])

    # The creator's only writable namespace.
    made = await client.post("/v1/tasks", headers=narrow, json={"title": "from narrow"})
    assert made.status_code == 201, made.text
    assert made.json()["namespace"] == "ideas"
    assert _codes(made.json())["task_namespace_inferred"]["source"] == "creator"

    # The one namespace all context documents share.
    await client.put("/v1/documents/research/brief", headers=owner,
                     json={"namespace": "research", "content": "brief", "expected_version": 0})
    from_docs = await client.post("/v1/tasks", headers=owner, json={
        "title": "from docs",
        "context_docs": [{"namespace": "research", "doc_key": "research/brief"}]})
    assert from_docs.status_code == 201, from_docs.text
    assert from_docs.json()["namespace"] == "research"

    # Nothing to go on: "default", said out loud.
    loose = await client.post("/v1/tasks", headers=owner, json={"title": "loose"})
    assert loose.json()["namespace"] == "default"
    assert "task_namespace_defaulted" in _codes(loose.json())

    explicit = await client.post("/v1/tasks", headers=owner,
                                 json={"title": "explicit", "namespace": "goals"})
    assert explicit.json()["namespace"] == "goals"
    assert not {"task_namespace_inferred", "task_namespace_defaulted"} & set(
        _codes(explicit.json()))


async def test_assignee_must_reach_the_namespace(client, admin_headers, workspace):
    owner = workspace["headers"]
    _, signature = await _agent(client, admin_headers, workspace, ["ideas"])
    assignee = f"agent:{signature}"

    # Omitted: the task follows the assignee.
    followed = await client.post("/v1/tasks", headers=owner,
                                 json={"title": "for you", "assignee": assignee})
    assert followed.status_code == 201, followed.text
    assert followed.json()["namespace"] == "ideas"
    assert _codes(followed.json())["task_namespace_inferred"]["source"] == "assignee"

    refused = await client.post("/v1/tasks", headers=owner, json={
        "title": "out of reach", "assignee": assignee, "namespace": "default"})
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "assignee_cannot_reach_namespace"
    assert error["details"]["assignee_namespaces"] == ["ideas"]


async def test_hidden_assignments_are_counted(client, admin_headers, workspace):
    owner = workspace["headers"]
    agent, signature = await _agent(client, admin_headers, workspace, ["ideas"])
    made = await client.post("/v1/tasks", headers=owner, json={
        "title": "moved away", "assignee": f"agent:{signature}"})
    assert made.status_code == 201, made.text
    conn = await asyncpg.connect(_db())
    try:  # e.g. a task created before this check existed
        await conn.execute("UPDATE collab_tasks SET namespace = 'default' WHERE task_key = $1",
                           made.json()["task_key"])
    finally:
        await conn.close()

    listed = await client.get("/v1/tasks", headers=agent, params={"assignee": "me"})
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    warning = _codes(listed.json())["assigned_tasks_out_of_reach"]
    assert warning["namespaces"] == {"default": 1}


async def test_new_inbox_items_ride_on_the_next_tool_result(
        client, admin_headers, workspace, api_base_url):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    agent, signature = await _agent(client, admin_headers, workspace, ["ideas"])
    made = await client.post("/v1/tasks", headers=workspace["headers"], json={
        "title": "poke", "assignee": f"agent:{signature}"})
    assert made.status_code == 201, made.text
    task_key = made.json()["task_key"]

    async with httpx.AsyncClient(headers=agent, timeout=60) as http:
        async with streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (
                read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                first = await session.call_tool("list_namespaces", {})
                inbox = first.structuredContent["inbox"]
                assert inbox["unacked_count"] >= 1
                assert task_key in [item.get("task_key") for item in inbox["new"]]
                assert inbox["next_call"]["tool"] == "list_inbox"
                # Shown once: the next result does not repeat it.
                again = await session.call_tool("list_namespaces", {})
                assert "inbox" not in again.structuredContent
