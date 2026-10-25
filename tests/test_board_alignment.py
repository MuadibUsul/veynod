"""Board, onboarding and REST alignment fixes (2026-10-24 playtest, TASK-999A4453,
batch C/D part 1): what claimable means, task-key filters and shapes, cancel,
and the listing and duplicate warnings.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


async def _agent(client, admin_headers, workspace, role="EXECUTOR", namespaces=None):
    signature = f"b_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": role,
              "allowed_namespaces": namespaces or ["*"]},
    )
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}, signature


async def _task(client, headers, **body):
    body.setdefault("title", "t")
    response = await client.post("/v1/tasks", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _keys(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["task_key"] for item in response.json()["items"]}


def _codes(body: dict) -> dict[str, dict]:
    return {item["code"]: item for item in body.get("warnings") or []}


async def test_claimable_means_what_claim_task_accepts(client, admin_headers, workspace):
    owner = workspace["headers"]
    open_task = await _task(client, owner, namespace="board", task_key="OPEN-1")
    await _task(client, owner, namespace="elsewhere", task_key="AWAY-1")
    await _task(client, owner, namespace="board", task_key="AUD-1", assigned_role="AUDITOR")

    # #12: an EXECUTOR writing only "board" is offered only what it can claim.
    executor, _ = await _agent(client, admin_headers, workspace, "EXECUTOR", ["board"])
    listed = await client.get("/v1/tasks", headers=executor,
                              params={"claimable_by_me": "true"})
    assert _keys(listed) == {"OPEN-1"}
    pack = (await client.get("/v1/orientation", headers=executor)).json()
    assert pack["claimable_tasks"]["count"] == 1

    # A SCOUT has no task:claim, so nothing is claimable, whatever it can read.
    scout, _ = await _agent(client, admin_headers, workspace, "SCOUT")
    assert _keys(await client.get("/v1/tasks", headers=scout,
                                  params={"claimable_by_me": "true"})) == set()
    pack = (await client.get("/v1/orientation", headers=scout)).json()
    assert pack["claimable_tasks"]["count"] == 0
    assert pack["claimable_tasks"]["top"] == []
    refused = await client.post(f"/v1/tasks/{open_task['task_key']}/claim",
                                headers=scout, json={})
    assert refused.status_code == 403, refused.text


async def test_task_key_filter_and_shape(client, admin_headers, workspace):
    owner = workspace["headers"]
    first = await _task(client, owner, namespace="board", task_key="KEY-1")
    await _task(client, owner, namespace="board", task_key="KEY-2")
    await _task(client, owner, namespace="board", task_key="KEY-3")

    # #11: ?task_key= filters (keys or canonical ids, comma-separated).
    picked = await client.get("/v1/tasks", headers=owner, params={"task_key": "KEY-1,KEY-3"})
    assert _keys(picked) == {"KEY-1", "KEY-3"}
    if first.get("canonical_id"):
        by_id = await client.get("/v1/tasks", headers=owner,
                                 params={"task_key": first["canonical_id"]})
        assert _keys(by_id) == {"KEY-1"}
    assert _keys(await client.get("/v1/tasks", headers=owner,
                                  params={"task_key": "NOPE"})) == set()

    # #9: a '/' would break /v1/tasks/{key}/claim; the error suggests a key.
    slashed = await client.post("/v1/tasks", headers=owner,
                                json={"title": "t", "namespace": "board", "task_key": "a/b"})
    assert slashed.status_code == 422, slashed.text
    assert "a-b" in slashed.text


async def test_cancel_by_creator_or_person(client, admin_headers, workspace):
    owner = workspace["headers"]
    creator, _ = await _agent(client, admin_headers, workspace, "EXECUTOR", ["board"])
    other, _ = await _agent(client, admin_headers, workspace, "EXECUTOR", ["board"])

    mine = await _task(client, creator, namespace="board", task_key="MINE-1")
    no_reason = await client.post("/v1/tasks/MINE-1/cancel", headers=creator, json={})
    assert no_reason.status_code == 422, no_reason.text
    stranger = await client.post("/v1/tasks/MINE-1/cancel", headers=other,
                                 json={"reason": "not mine to cancel"})
    assert stranger.status_code == 403, stranger.text
    cancelled = await client.post("/v1/tasks/MINE-1/cancel", headers=creator,
                                  json={"reason": "duplicate of another task",
                                        "expected_version": mine["version"]})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "FAILED"
    again = await client.post("/v1/tasks/MINE-1/cancel", headers=creator,
                              json={"reason": "twice"})
    assert again.status_code == 409, again.text

    # Held by someone else: the creator is told who, a person may still cancel.
    await _task(client, creator, namespace="board", task_key="HELD-1")
    claimed = await client.post("/v1/tasks/HELD-1/claim", headers=other, json={})
    assert claimed.status_code == 200, claimed.text
    held = await client.post("/v1/tasks/HELD-1/cancel", headers=creator,
                             json={"reason": "no longer needed"})
    assert held.status_code == 409, held.text
    assert held.json()["error"]["code"] == "task_claimed"
    by_owner = await client.post("/v1/tasks/HELD-1/cancel", headers=owner,
                                 json={"reason": "no longer needed"})
    assert by_owner.status_code == 200, by_owner.text
    # The holder's lease ends with the task.
    active = await client.get("/v1/leases", headers=other, params={"state": "active"})
    assert active.status_code == 200, active.text
    lease_id = claimed.json()["lease"]["lease_id"]
    assert lease_id not in {item["lease_id"] for item in active.json()["items"]}


async def test_listing_and_duplicate_warnings(client, admin_headers, workspace):
    owner = workspace["headers"]
    writer, _ = await _agent(client, admin_headers, workspace, "EXECUTOR", ["notes"])
    reader, _ = await _agent(client, admin_headers, workspace, "EXECUTOR", ["notes"])

    draft = await client.put("/v1/documents/notes/draft", headers=writer,
                             json={"namespace": "notes", "content": "a draft",
                                   "expected_version": 0})
    assert draft.status_code in (200, 201), draft.text

    # #7: the legacy listing says what its trust filter hid.
    listed = await client.get("/v1/documents", headers=reader, params={"namespace": "notes"})
    assert listed.status_code == 200, listed.text
    hidden = _codes(listed.json()).get("listing_hides_documents")
    assert hidden is not None and hidden["hidden"] >= 1
    everything = await client.get("/v1/documents", headers=reader, params={
        "namespace": "notes", "include_unverified": "true", "include_tainted": "true"})
    assert "listing_hides_documents" not in _codes(everything.json())
    assert "listing_hides_documents" not in _codes(
        (await client.get("/v1/documents", headers=owner,
                          params={"namespace": "notes"})).json())

    # #18: the same text under a second key in the namespace is flagged.
    twin = await client.put("/v1/documents/notes/draft-copy", headers=writer,
                            json={"namespace": "notes", "content": "a draft",
                                  "expected_version": 0})
    assert twin.status_code in (200, 201), twin.text
    duplicate = _codes(twin.json()).get("duplicate_content")
    assert duplicate is not None and duplicate["existing"] == "notes/draft"
    elsewhere = await client.put("/v1/documents/other/draft", headers=owner,
                                 json={"namespace": "other", "content": "a draft",
                                       "expected_version": 0})
    assert elsewhere.status_code in (200, 201), elsewhere.text
    assert "duplicate_content" not in _codes(elsewhere.json())
