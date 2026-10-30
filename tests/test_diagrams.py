"""Diagrams and chunked dispatch (agentgraph/spec-diagram-dispatch v3).

Covers spec A-01 (end to end), A-02 (frozen diagram, changes), A-03 (gate and
merge separation), A-04 (command view basics), A-05 (replay), A-06 (all of it
by hand, no automation), A-07 (self-check) and section 16: A-16 (fast lane),
A-17 (super admin for the owner, then a person's override), A-18 (no
injection into chunks in flight). Automation stage: A-08 (contract first,
topology unlock with pokes), A-09 (context slice, write-scope veto, file
locks), A-10 (deterministic gate first, focused review, triptych).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
BRIEF = {"summary": "done", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}
PASSWORD = "correct-horse-battery"


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"dg-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"dg-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"},
            "person": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _agent(client, ws, role="EXECUTOR") -> dict:
    signature = f"{role.lower()}-{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": signature, "display_name": signature,
                                     "role_scope": role, "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"},
            "signature": signature}


def _chunk(key, layer="implementation", deps=(), **extra):
    return {"chunk_key": key, "title": f"chunk {key}", "scope": f"build {key}",
            "layer": layer, "icd": {"exports": [f"{key}()"]},
            "done_criteria": [f"pytest tests/test_{key}.py"], "depends_on": list(deps),
            "allowed_write_paths": [f"src/{key}.py", f"tests/test_{key}.py"], **extra}


async def _deliver(client, executor, task_key):
    """The implementer claims the chunk's task and submits it for the gate."""
    claimed = await client.post(f"/v1/tasks/{task_key}/claim", headers=executor["headers"],
                                json={})
    assert claimed.status_code == 200, claimed.text
    handed = await client.post(f"/v1/tasks/{task_key}/handoff", headers=executor["headers"],
                               json={"next_status": "REVIEWING", "brief": BRIEF,
                                     "lease_epoch": claimed.json()["task"]["lease_epoch"]})
    assert handed.status_code == 200, handed.text


async def _frozen(client, ws, architect, chunks, key=None):
    key = key or f"dg-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": key, "title": "Payments", "namespace": "default", "chunks": chunks,
        "self_check": {"big": True, "stable": True, "machine_checkable": True, "long": True}})
    assert made.status_code == 201, made.text
    refused = await client.post(f"/v1/diagrams/{key}/confirm", headers=architect["headers"])
    assert refused.status_code == 403  # the gate is a person's
    early = await client.post(f"/v1/diagrams/{key}/chunks/{chunks[0]['chunk_key']}/dispatch",
                              headers=architect["headers"])
    assert early.status_code == 409 and early.json()["error"]["code"] == "not_frozen"
    assert (await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["person"])
            ).status_code == 200
    frozen = await client.post(f"/v1/diagrams/{key}/freeze", headers=ws["person"], json={})
    assert frozen.status_code == 200, frozen.text
    return key, frozen.json()


async def test_end_to_end_lightweight_path(client):
    """A-01 / A-03 / A-04 / A-05 / A-06 / A-07."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")

    # M-06: a chunk without a contract or criteria is refused, naming what is missing.
    bad = await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": "bad-one", "title": "x",
        "chunks": [{"chunk_key": "a", "title": "a"}]})
    assert bad.status_code == 422
    assert bad.json()["error"]["details"]["chunks"]["a"]["missing"] == ["icd", "done_criteria"]
    cyclic = await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": "cyclic", "title": "x",
        "chunks": [_chunk("a", deps=["b"]), _chunk("b", deps=["a"])]})
    assert cyclic.json()["error"]["code"] == "dependency_cycle"
    # A-07: two "no" answers are a risk, shown, not blocking.
    risky = await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": f"risky-{uuid.uuid4().hex[:4]}", "title": "x", "chunks": [_chunk("a")],
        "self_check": {"big": True, "stable": False, "machine_checkable": True}})
    assert risky.status_code == 201 and risky.json()["self_check"]["risk"] is True

    chunks = [_chunk("api", layer="interface"), _chunk("core", deps=["api"]),
              _chunk("ui", deps=["core"]), _chunk("docs", layer="interface")]
    key, view = await _frozen(client, ws, architect, chunks)
    assert view["status"] == "frozen" and view["baseline"]["version"] == view["version"]
    assert view["self_check"]["risk"] is False
    document = await client.get(f"/v1/documents/diagrams%2F{key}", headers=ws["key"],
                                params={"include_unverified": "true"})
    assert document.status_code == 200, document.text
    assert "### core" in document.json()["content"]

    # Dispatch: api and docs go out; core is blocked on api, with the reason.
    api = (await client.post(f"/v1/diagrams/{key}/chunks/api/dispatch",
                             headers=architect["headers"])).json()
    docs = (await client.post(f"/v1/diagrams/{key}/chunks/docs/dispatch",
                              headers=architect["headers"])).json()
    core = (await client.post(f"/v1/diagrams/{key}/chunks/core/dispatch",
                              headers=architect["headers"])).json()
    assert api["state"] == "dispatched" and docs["state"] == "dispatched"
    assert core["state"] == "blocked" and "api" in core["block_reason"]
    task = (await client.get(f"/v1/tasks/{api['task_key']}", headers=ws["key"])).json()
    assert task["input_context"]["chunk"] == "api"
    assert task["input_context"]["allowed_write_paths"] == ["src/api.py", "tests/test_api.py"]
    assert [item["text"] for item in task["acceptance"]] == ["pytest tests/test_api.py"]

    # The implementer delivers; a failed gate sends it back with the evidence.
    await _deliver(client, executor, api["task_key"])
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert {c["chunk_key"]: c["status"] for c in shown["chunks"]}["api"] == "awaiting_gate"
    own = await client.post(f"/v1/diagrams/{key}/chunks/api/gate", headers=executor["headers"],
                            json={"result": "pass"})
    assert own.status_code == 403
    no_evidence = await client.post(f"/v1/diagrams/{key}/chunks/api/gate",
                                    headers=reviewer["headers"], json={"result": "fail"})
    assert no_evidence.status_code == 422
    failed = await client.post(f"/v1/diagrams/{key}/chunks/api/gate", headers=reviewer["headers"],
                               json={"result": "fail", "evidence": "2 tests failed: test_refund"})
    assert failed.json()["status"] == "rework"
    early_merge = await client.post(f"/v1/diagrams/{key}/chunks/api/merge",
                                    headers=ws["person"], json={})
    assert early_merge.json()["error"]["code"] == "gate_not_passed"
    passed = await client.post(f"/v1/diagrams/{key}/chunks/api/gate", headers=reviewer["headers"],
                               json={"result": "pass", "checks": ["pytest: 12 passed"]})
    assert passed.json()["status"] == "ready_to_merge"

    # The implementer may not merge; someone else does, and core is released.
    by_implementer = await client.post(f"/v1/diagrams/{key}/chunks/api/merge",
                                       headers=executor["headers"], json={})
    assert by_implementer.status_code == 403
    assert by_implementer.json()["error"]["code"] == "merge_by_implementer"
    merged = await client.post(f"/v1/diagrams/{key}/chunks/api/merge", headers=ws["person"],
                               json={"artifact": "commit abc123"})
    assert merged.status_code == 200, merged.text
    assert [item["chunk_key"] for item in merged.json()["unblocked"]] == ["core"]
    # The docs chunk is picked up and in progress.
    await client.post(f"/v1/tasks/{docs['task_key']}/claim", headers=executor["headers"],
                      json={})

    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    statuses = {c["chunk_key"]: c["status"] for c in shown["chunks"]}
    assert statuses == {"api": "merged", "core": "dispatched", "ui": "pending",
                        "docs": "in_progress"}
    dispatched_ui = (await client.post(f"/v1/diagrams/{key}/chunks/ui/dispatch",
                                       headers=architect["headers"])).json()
    assert dispatched_ui["state"] == "blocked" and "core" in dispatched_ui["block_reason"]
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert shown["progress"]["distribution"] == {"merged": 1, "dispatched": 1, "blocked": 1,
                                                 "in_progress": 1}
    assert shown["progress"]["blocked"][0]["chunk_key"] == "ui"
    assert shown["board_consistency"]["ok"] is True

    # A-05: the chain replays without a gap, for the diagram and for one chunk.
    chain = (await client.get(f"/v1/diagrams/{key}/replay", headers=ws["key"])).json()
    types = [event["type"] for event in chain["events"]]
    for expected in ("timeline.diagram_created", "timeline.diagram_confirmed",
                     "timeline.diagram_frozen", "timeline.chunk_dispatched",
                     "timeline.chunk_blocked", "timeline.chunk_gated",
                     "timeline.chunk_merged", "timeline.chunk_unblocked"):
        assert expected in types, (expected, types)
    one = (await client.get(f"/v1/diagrams/{key}/replay", headers=ws["key"],
                            params={"chunk": "api"})).json()
    assert [e["type"] for e in one["events"]] == [
        "timeline.chunk_dispatched", "timeline.chunk_gated", "timeline.chunk_trust_charged",
        "timeline.chunk_gated", "timeline.chunk_merged"]
    assert any(e["type"] == "task.claimed" for e in one["task_events"])


async def test_frozen_diagram_changes_and_section_16(client):
    """A-02 and section 16: A-16, A-17, A-18."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    owner_proxy = await _agent(client, ws, "SUPER_ADMIN")
    key, view = await _frozen(client, ws, architect,
                              [_chunk("api", layer="interface"), _chunk("core", deps=["api"])])
    base = view["baseline"]

    # A-02: the frozen diagram is not edited directly.
    edit = await client.put(f"/v1/diagrams/{key}", headers=architect["headers"],
                            json={"chunks": [_chunk("api", layer="interface")]})
    assert edit.status_code == 403 and edit.json()["error"]["code"] == "diagram_frozen"
    # A change to the interface: rejected, nothing moves.
    change = (await client.post(f"/v1/diagrams/{key}/changes", headers=architect["headers"],
                                json={"summary": "api returns a cursor",
                                      "patch": {"update": [{"chunk_key": "api",
                                                            "icd": {"exports": ["api(cursor)"]}}]}})
              ).json()
    assert change["impact"]["interfaces"] == ["api"] and change["impact"]["downstream"] == ["core"]
    by_agent = await client.post(f"/v1/diagrams/{key}/changes/{change['change_key']}/decide",
                                 headers=executor["headers"], json={"decision": "approve"})
    assert by_agent.status_code == 403
    rejected = await client.post(f"/v1/diagrams/{key}/changes/{change['change_key']}/decide",
                                 headers=ws["person"], json={"decision": "reject"})
    assert rejected.json()["status"] == "rejected"
    still = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert still["version"] == view["version"] and still["baseline"] == base
    # Filed again and approved: version + 1, and the baseline follows the interface.
    again = (await client.post(f"/v1/diagrams/{key}/changes", headers=architect["headers"],
                               json={"summary": "api returns a cursor",
                                     "patch": {"update": [{"chunk_key": "api",
                                                           "icd": {"exports": ["api(cursor)"]}}]}})
             ).json()
    approved = await client.post(f"/v1/diagrams/{key}/changes/{again['change_key']}/decide",
                                 headers=ws["person"], json={"decision": "approve"})
    assert approved.json()["status"] == "approved"
    after = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert after["version"] == view["version"] + 1
    assert after["baseline"]["version"] == after["version"]
    assert after["baseline"]["interface_hash"] != base["interface_hash"]

    # A-18: once api is in flight, a change cannot rewrite it ...
    api = (await client.post(f"/v1/diagrams/{key}/chunks/api/dispatch",
                             headers=architect["headers"])).json()
    await client.post(f"/v1/tasks/{api['task_key']}/claim", headers=executor["headers"], json={})
    injected = await client.post(f"/v1/diagrams/{key}/changes", headers=architect["headers"],
                                 json={"summary": "add pagination to api",
                                       "patch": {"update": [{"chunk_key": "api",
                                                             "scope": "also paginate"}]}})
    assert injected.status_code == 409
    assert injected.json()["error"]["code"] == "chunk_in_flight"

    # ... A-16: the fast lane turns a person's request into a new chunk on top of it,
    # a super admin approves it for the owner, and it is dispatched.
    lane = await client.post(f"/v1/diagrams/{key}/increments", headers=architect["headers"],
                             json={"request_text": "老板说：列表接口要能分页",
                                   "target_chunk": "api",
                                   "chunk": _chunk("api-paging", deps=[])})
    assert lane.status_code == 201, lane.text
    lane = lane.json()
    assert lane["source"] == "human_request" and lane["impact"]["chunks"] == ["api-paging"]
    decided = await client.post(f"/v1/diagrams/{key}/changes/{lane['change_key']}/decide",
                                headers=owner_proxy["headers"], json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    decided = decided.json()
    assert decided["decision_kind"] == "super_admin_for_owner"
    assert decided["dispatched"][0]["chunk_key"] == "api-paging"
    # api-paging depends on api, which is not merged: blocked, not injected into api.
    assert decided["dispatched"][0]["state"] == "blocked"
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    paging = next(c for c in shown["chunks"] if c["chunk_key"] == "api-paging")
    assert paging["depends_on"] == ["api"] and paging["increment_of"] == "api"
    original = next(c for c in shown["chunks"] if c["chunk_key"] == "api")
    assert original["scope"] == "build api" and original["status"] == "in_progress"
    assert shown["version"] == after["version"] + 1

    # A-17: a person overrides the super admin's decision; the chunk is retired.
    super_revoke = await client.post(
        f"/v1/diagrams/{key}/changes/{lane['change_key']}/revoke",
        headers=owner_proxy["headers"], json={})
    assert super_revoke.status_code == 403
    revoked = await client.post(f"/v1/diagrams/{key}/changes/{lane['change_key']}/revoke",
                                headers=ws["person"], json={"note": "not this sprint"})
    assert revoked.json()["status"] == "revoked"
    final = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    # The view lists the latest decisions, the revoked one first, for the console.
    assert final["pending_changes"] == []
    assert final["recent_changes"][0]["change_key"] == lane["change_key"]
    assert final["recent_changes"][0]["status"] == "revoked"
    assert "api-paging" not in final["progress"]["distribution"] or all(
        c["status"] == "retired" for c in final["chunks"] if c["chunk_key"] == "api-paging")
    chain = (await client.get(f"/v1/diagrams/{key}/replay", headers=ws["key"])).json()
    decisions = [e["details"] for e in chain["events"]
                 if e["type"] == "timeline.diagram_change_decided"]
    assert {d["decision_kind"] for d in decisions} == {"person", "super_admin_for_owner"}
    assert any(e["type"] == "timeline.diagram_change_revoked"
               and e["details"]["overrode"] == "super_admin_for_owner" for e in chain["events"])


async def test_contract_first_and_topology_unlock(client):
    """A-08: one contract and two implementations; nothing implements before the contract."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    watcher = await _agent(client, ws)
    watcher_id = {"agent_id": watcher["signature"]}  # minting registers it

    # An implementation chunk with nothing upstream cannot be confirmed (M-13).
    key = f"cf-{uuid.uuid4().hex[:6]}"
    await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": key, "title": "x", "chunks": [_chunk("impl")]})
    refused = await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["person"])
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "contract_first"
    assert refused.json()["error"]["details"]["implementation_roots"] == ["impl"]

    key = f"cf-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": key, "title": "Ledger", "watchers": [f"agent:{watcher_id['agent_id']}"],
        "chunks": [_chunk("schema", layer="interface"), _chunk("writer", deps=["schema"]),
                   _chunk("reader", deps=["schema"])]})
    assert made.status_code == 201, made.text
    await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["person"])
    await client.post(f"/v1/diagrams/{key}/freeze", headers=ws["person"], json={})
    for chunk in ("writer", "reader"):
        early = (await client.post(f"/v1/diagrams/{key}/chunks/{chunk}/dispatch",
                                   headers=architect["headers"])).json()
        assert early["state"] == "blocked" and early["block_kind"] == "dependency"
    schema = (await client.post(f"/v1/diagrams/{key}/chunks/schema/dispatch",
                                headers=architect["headers"])).json()
    await _deliver(client, executor, schema["task_key"])
    await client.post(f"/v1/diagrams/{key}/chunks/schema/gate", headers=reviewer["headers"],
                      json={"result": "pass"})
    merged = (await client.post(f"/v1/diagrams/{key}/chunks/schema/merge", headers=ws["person"],
                                json={"artifact": "commit schema1"})).json()
    # Both implementations are dispatched by the graph itself, nobody re-dispatches.
    assert sorted(item["chunk_key"] for item in merged["unblocked"]) == ["reader", "writer"]
    inbox = (await client.get("/v1/inbox", headers=watcher["headers"])).json()["items"]
    kinds = [item.get("kind") or item.get("type") for item in inbox]
    assert sum(1 for kind in kinds if "chunk_unblocked" in str(kind)) == 2, kinds
    assert any("chunk_merged" in str(kind) for kind in kinds), kinds


async def _sql(query: str, *args):
    conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1))
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def test_block_reasons_are_current_and_merges_leave_circuits_alone(client):
    """DX-5: a merge re-checks every graph block, so a chunk still waiting names only
    what it still waits on, in the view and stored; a circuit-broken chunk is a
    person's to lift, never re-dispatched by an unrelated merge."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    chunks = [_chunk("a", layer="interface"), _chunk("b", layer="interface"),
              _chunk("c", deps=["a", "b"]), _chunk("d", deps=["a"])]
    key, _ = await _frozen(client, ws, architect, chunks)
    dispatched = {}
    for chunk_key in ("a", "b", "c", "d"):
        dispatched[chunk_key] = (await client.post(
            f"/v1/diagrams/{key}/chunks/{chunk_key}/dispatch", headers=architect["headers"])).json()
    assert dispatched["c"]["block_reason"] == "waiting on a (dispatched), b (dispatched)"

    # d is past a's merge in the plan; break its circuit as three rejections would.
    await _sql("UPDATE diagram_chunks SET block_kind = 'circuit', block_reason = 'trust 20' "
               "WHERE chunk_key = 'd' AND diagram_id = (SELECT id FROM diagrams "
               "WHERE diagram_key = $1)", key)

    await _deliver(client, executor, dispatched["a"]["task_key"])
    gated = await client.post(f"/v1/diagrams/{key}/chunks/a/gate", headers=reviewer["headers"],
                              json={"result": "pass"})
    assert gated.status_code == 200, gated.text
    merged = await client.post(f"/v1/diagrams/{key}/chunks/a/merge", headers=ws["person"],
                               json={"artifact": "commit a1"})
    assert merged.status_code == 200, merged.text
    assert merged.json()["unblocked"] == []

    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    by_key = {chunk["chunk_key"]: chunk for chunk in shown["chunks"]}
    assert by_key["c"]["block_reason"] == "waiting on b (dispatched)"
    assert {item["chunk_key"]: item["reason"] for item in shown["progress"]["blocked"]}["c"] == (
        "waiting on b (dispatched)")
    stored = await _sql("SELECT chunk_key, state, block_kind, block_reason, task_key FROM "
                        "diagram_chunks WHERE diagram_id = (SELECT id FROM diagrams WHERE "
                        "diagram_key = $1) ORDER BY chunk_key", key)
    rows = {row["chunk_key"]: row for row in stored}
    assert rows["c"]["block_reason"] == "waiting on b (dispatched)"
    # The circuit-broken chunk stays blocked, with no new task.
    assert (rows["d"]["state"], rows["d"]["block_kind"], rows["d"]["task_key"]) == (
        "blocked", "circuit", None)


async def test_merge_needs_a_record_and_a_chunk_can_be_repointed(client):
    """DX-2: nothing merges without a record of what it delivered; a chunk whose
    duplicate dispatch failed is re-pointed to its DONE task, with a fix hint first."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    key, _ = await _frozen(client, ws, architect, [_chunk("p0", layer="interface")])
    first = (await client.post(f"/v1/diagrams/{key}/chunks/p0/dispatch",
                               headers=architect["headers"])).json()
    await _deliver(client, executor, first["task_key"])
    # The work is reviewed DONE on the board, then gated.
    done = await client.post(f"/v1/tasks/{first['task_key']}/review", headers=ws["person"],
                             json={"decision": "approve", "feedback": "ok"})
    assert done.status_code == 200, done.text
    await client.post(f"/v1/diagrams/{key}/chunks/p0/gate", headers=reviewer["headers"],
                      json={"result": "pass"})
    bare = await client.post(f"/v1/diagrams/{key}/chunks/p0/merge", headers=ws["person"],
                             json={})
    assert bare.status_code == 409, bare.text
    assert bare.json()["error"]["code"] == "merge_without_deliverable"

    # A duplicate dispatch is made and fails: the personal-website P0 case.
    duplicate = f"{first['task_key']}-2"
    made = await client.post("/v1/tasks", headers=ws["person"], json={
        "task_key": duplicate, "title": "duplicate", "assigned_role": "EXECUTOR",
        "namespace": "default"})
    assert made.status_code == 201, made.text
    await _sql("UPDATE collab_tasks SET status = 'FAILED' WHERE task_key = $1 "
               "AND workspace_id = $2::uuid", duplicate, ws["id"])
    merged = await client.post(f"/v1/diagrams/{key}/chunks/p0/merge", headers=ws["person"],
                               json={"artifact": "doc://artifact/p0/delivery"})
    assert merged.status_code == 200, merged.text
    await _sql("UPDATE diagram_chunks SET task_key = $1 WHERE chunk_key = 'p0' AND "
               "diagram_id = (SELECT id FROM diagrams WHERE diagram_key = $2)", duplicate, key)

    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    [mismatch] = shown["board_consistency"]["mismatches"]
    assert mismatch["problem"] == "merged, but its task is FAILED"
    assert mismatch["fix"]["task_key"] == first["task_key"]
    assert 'action="repoint"' in mismatch["fix"]["call"]

    by_executor = await client.post(f"/v1/diagrams/{key}/chunks/p0/repoint",
                                    headers=executor["headers"],
                                    json={"task_key": first["task_key"]})
    assert by_executor.status_code == 403
    stranger = await client.post(f"/v1/diagrams/{key}/chunks/p0/repoint", headers=ws["person"],
                                 json={"task_key": "some-other-task"})
    assert stranger.json()["error"]["code"] == "not_chunk_lineage"
    assert sorted(stranger.json()["error"]["details"]["lineage"]) == sorted(
        [first["task_key"], duplicate])
    not_done = await client.post(f"/v1/diagrams/{key}/chunks/p0/repoint", headers=ws["person"],
                                 json={"task_key": duplicate})
    assert not_done.json()["error"]["code"] == "task_not_done"
    fixed = await client.post(f"/v1/diagrams/{key}/chunks/p0/repoint",
                              headers=architect["headers"],
                              json={"task_key": first["task_key"]})
    assert fixed.status_code == 200, fixed.text
    assert fixed.json() == {"chunk_key": "p0", "task_key": first["task_key"],
                            "previous": duplicate}
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert shown["board_consistency"]["ok"] is True


async def test_downstream_claims_carry_what_interface_chunks_delivered(client):
    """DX-1: a merged interface chunk hands its delivered documents (pinned) and files
    to the chunks after it; an implementation chunk hands over nothing (M-16)."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    suffix = uuid.uuid4().hex[:6]
    contract_doc, core_doc = f"artifact/api-{suffix}/contract", f"artifact/core-{suffix}/notes"
    for doc_key, content in ((contract_doc, "tokens: --color-accent"), (core_doc, "core code")):
        stored = await client.put(f"/v1/documents/{doc_key}", headers=ws["key"],
                                  json={"content": content, "expected_version": 0})
        assert stored.status_code in (200, 201), stored.text
    key, _ = await _frozen(client, ws, architect, [
        _chunk("api", layer="interface"), _chunk("core", deps=["api"]),
        _chunk("ui", deps=["core"])])
    api = (await client.post(f"/v1/diagrams/{key}/chunks/api/dispatch",
                             headers=architect["headers"])).json()
    for later in ("core", "ui"):
        await client.post(f"/v1/diagrams/{key}/chunks/{later}/dispatch",
                          headers=architect["headers"])
    await _deliver(client, executor, api["task_key"])
    await client.post(f"/v1/diagrams/{key}/chunks/api/gate", headers=reviewer["headers"],
                      json={"result": "pass"})
    merged = await client.post(f"/v1/diagrams/{key}/chunks/api/merge", headers=ws["person"], json={
        "artifact": "commit api1",
        "deliverables": {"docs": [f"doc://{contract_doc}", "doc://artifact/none/missing"],
                         "files": [{"path": "src/api.py", "sha256": "ab" * 32}]}})
    assert merged.status_code == 200, merged.text
    delivered = merged.json()["merge"]["delivered"]
    assert delivered["files"] == [{"path": "src/api.py", "sha256": "ab" * 32}]
    assert delivered["docs"][0]["doc_key"] == contract_doc and delivered["docs"][0]["version"] == 1
    assert delivered["docs"][1] == {"uri": "doc://artifact/none/missing", "missing": True}
    [core] = merged.json()["unblocked"]

    # The contract changes after the merge: the downstream claim still reads what was merged.
    await client.put(f"/v1/documents/{contract_doc}", headers=ws["key"],
                     json={"content": "tokens: changed later", "expected_version": 1})
    task = (await client.get(f"/v1/tasks/{core['task_key']}", headers=ws["key"])).json()
    assert task["input_context"]["upstream_interfaces"]["api"]["docs"] == [f"doc://{contract_doc}"]
    assert task["input_context"]["upstream_interfaces"]["api"]["files"] == delivered["files"]
    claimed = await client.post(f"/v1/tasks/{core['task_key']}/claim", headers=executor["headers"],
                                json={})
    assert claimed.status_code == 200, claimed.text
    [context] = [item for item in claimed.json()["context"] if not item.get("is_continuation")]
    assert context["doc_ref"]["doc_key"] == contract_doc and context["version"] == 1
    assert context["content"] == "tokens: --color-accent"

    # core is implementation: its deliverables never reach ui (M-16).
    handed = await client.post(f"/v1/tasks/{core['task_key']}/handoff", headers=executor["headers"],
                               json={"next_status": "REVIEWING", "brief": BRIEF,
                                     "lease_epoch": claimed.json()["task"]["lease_epoch"]})
    assert handed.status_code == 200, handed.text
    await client.post(f"/v1/diagrams/{key}/chunks/core/gate", headers=reviewer["headers"],
                      json={"result": "pass"})
    merged = await client.post(f"/v1/diagrams/{key}/chunks/core/merge", headers=ws["person"],
                               json={"artifact": f"doc://{core_doc}"})
    assert merged.status_code == 200, merged.text
    [ui] = merged.json()["unblocked"]
    task = (await client.get(f"/v1/tasks/{ui['task_key']}", headers=ws["key"])).json()
    assert "docs" not in task["input_context"]["upstream_interfaces"]["core"]
    assert task["context_docs"] == []


async def test_context_slice_write_scope_and_file_locks(client):
    """A-09."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    chunks = [_chunk("contract", layer="interface", allowed_write_paths=["api/schema.py"]),
              _chunk("a", deps=["contract"], allowed_write_paths=["src/a.py", "tests/a/*"],
                     read_only_interfaces=["api/schema.py"]),
              _chunk("b", deps=["contract"], allowed_write_paths=["src/shared.py"]),
              _chunk("c", deps=["contract"], allowed_write_paths=["src/*.py"])]
    key, view = await _frozen(client, ws, architect, chunks)
    assert view["baseline"]["frozen_files"] == ["api/schema.py"]
    for chunk in ("a", "b", "c"):  # dispatched, waiting on the contract
        waiting = (await client.post(f"/v1/diagrams/{key}/chunks/{chunk}/dispatch",
                                     headers=architect["headers"])).json()
        assert waiting["block_kind"] == "dependency"
    contract = (await client.post(f"/v1/diagrams/{key}/chunks/contract/dispatch",
                                  headers=architect["headers"])).json()
    await _deliver(client, executor, contract["task_key"])
    reviewer = await _agent(client, ws, "AUDITOR")
    await client.post(f"/v1/diagrams/{key}/chunks/contract/gate", headers=reviewer["headers"],
                      json={"result": "pass"})
    released = (await client.post(f"/v1/diagrams/{key}/chunks/contract/merge",
                                  headers=ws["person"],
                                  json={"artifact": "commit contract1"})).json()["unblocked"]
    by_key = {item["chunk_key"]: item for item in released}
    assert set(by_key) == {"a", "b"}, released
    # a and b go out; c wants src/*.py, which overlaps both: blocked on the lock, naming it.
    assert by_key["a"]["state"] == "dispatched" and by_key["b"]["state"] == "dispatched"
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    c_chunk = next(item for item in shown["chunks"] if item["chunk_key"] == "c")
    assert c_chunk["status"] == "blocked" and c_chunk["block_kind"] == "file_lock"
    assert "held by" in c_chunk["block_reason"] and "src/" in c_chunk["block_reason"]

    # The executor receives exactly three things (M-16).
    task = (await client.get(f"/v1/tasks/{by_key['a']['task_key']}", headers=ws["key"])).json()
    assert task["context_docs"] == []
    assert set(task["input_context"]["upstream_interfaces"]) == {"contract"}
    assert task["input_context"]["allowed_write_paths"] == ["src/a.py", "tests/a/*"]
    a_chunk = next(item for item in shown["chunks"] if item["chunk_key"] == "a")
    assert "repository" in a_chunk["context_slice"]["not_injected"]

    # A file outside the white list vetoes the submission: rework, no review (M-17).
    claimed = await client.post(f"/v1/tasks/{by_key['a']['task_key']}/claim",
                                headers=executor["headers"], json={})
    assert claimed.status_code == 200
    vetoed = (await client.post(f"/v1/diagrams/{key}/chunks/a/submit",
                                headers=executor["headers"],
                                json={"commit": "c1", "files": ["src/a.py", "src/shared.py"]})
              ).json()
    assert vetoed["accepted"] is False and vetoed["status"] == "rework"
    assert vetoed["violations"] == [{"path": "src/shared.py", "why": "outside allowed_write_paths"}]
    no_review = await client.post(f"/v1/diagrams/{key}/chunks/a/gate",
                                  headers=reviewer["headers"],
                                  json={"result": "pass", "semantic": {}})
    assert no_review.json()["error"]["code"] == "submission_rejected"
    frozen = (await client.post(f"/v1/diagrams/{key}/chunks/a/submit",
                                headers=executor["headers"],
                                json={"commit": "c2", "files": ["tests/a/api/schema.py",
                                                                "src/a.py"]})).json()
    assert frozen["accepted"] is True  # a test file that merely shares a name is fine
    other = await client.post(f"/v1/diagrams/{key}/chunks/a/submit",
                              headers=reviewer["headers"],
                              json={"commit": "c3", "files": ["src/a.py"]})
    assert other.status_code == 403  # only the implementer submits


async def test_deterministic_gate_then_focused_review(client):
    """A-10: a red sandbox never wakes review; review answers three rules; triptych."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    ci_runner = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    key, _ = await _frozen(client, ws, architect, [
        _chunk("api", layer="interface",
               commands={"compile": "tsc --noEmit", "test": "pytest -q"},
               semantic_rules=["no_breaking_rename"])])
    api = (await client.post(f"/v1/diagrams/{key}/chunks/api/dispatch",
                             headers=architect["headers"])).json()
    task = (await client.get(f"/v1/tasks/{api['task_key']}", headers=ws["key"])).json()
    assert task["input_context"]["commands"] == {"compile": "tsc --noEmit", "test": "pytest -q"}
    await client.post(f"/v1/tasks/{api['task_key']}/claim", headers=executor["headers"], json={})
    submitted = (await client.post(f"/v1/diagrams/{key}/chunks/api/submit",
                                   headers=executor["headers"],
                                   json={"commit": "abc", "files": ["src/api.py"],
                                         "diff": "+def api(): ..."})).json()
    assert submitted["status"] == "awaiting_ci"
    early = await client.post(f"/v1/diagrams/{key}/chunks/api/gate", headers=reviewer["headers"],
                              json={"result": "pass", "semantic": {}})
    assert early.json()["error"]["code"] == "ci_not_green"

    stderr = "src/api.py:3: error TS2322: Type 'string' is not assignable to type 'number'."
    own = await client.post(f"/v1/diagrams/{key}/chunks/api/ci", headers=executor["headers"],
                            json={"results": {"compile": {"exit_code": 0},
                                              "test": {"exit_code": 0}}})
    assert own.status_code == 403
    red = (await client.post(f"/v1/diagrams/{key}/chunks/api/ci", headers=ci_runner["headers"],
                             json={"commit": "abc", "results": {
                                 "compile": {"exit_code": 2, "stderr": stderr},
                                 "test": {"exit_code": 0}}})).json()
    assert red["review_woken"] is False and red["status"] == "rework"
    # The implementer gets stderr verbatim, in its inbox.
    events = (await client.get(f"/v1/diagrams/{key}/replay", headers=ws["key"],
                               params={"chunk": "api"})).json()["events"]
    failed = next(e for e in events if e["type"] == "timeline.chunk_ci_failed")
    assert failed["details"]["stderr"]["compile"] == stderr
    inbox = (await client.get("/v1/inbox", headers=executor["headers"])).json()["items"]
    assert any("chunk_ci_failed" in str(item.get("kind") or item.get("type")) for item in inbox)
    blocked_review = await client.post(f"/v1/diagrams/{key}/chunks/api/gate",
                                       headers=reviewer["headers"],
                                       json={"result": "pass", "semantic": {}})
    assert blocked_review.json()["error"]["code"] == "ci_not_green"

    # Fixed and green: the review is woken, and answers its rules, nothing more.
    await client.post(f"/v1/diagrams/{key}/chunks/api/submit", headers=executor["headers"],
                      json={"commit": "def", "files": ["src/api.py"],
                            "diff": "+def api(): return 1"})
    green = (await client.post(f"/v1/diagrams/{key}/chunks/api/ci", headers=ci_runner["headers"],
                               json={"results": {"compile": {"exit_code": 0},
                                                 "lint": {"exit_code": 0},
                                                 "test": {"exit_code": 0, "coverage": 0.91}}})
             ).json()
    assert green["review_woken"] is True and green["status"] == "awaiting_review"
    panes = (await client.get(f"/v1/diagrams/{key}/chunks/api/triptych",
                              headers=reviewer["headers"])).json()
    assert panes["left"]["contract"] == {"exports": ["api()"]}
    assert panes["middle"]["commit"] == "def" and "return 1" in panes["middle"]["diff"]
    assert panes["right"]["green"] is True
    assert panes["left"]["review_rules"] == ["no_cheating", "contract_compliance",
                                             "no_architecture_rot", "no_breaking_rename"]
    rules = {rule: {"result": "pass"} for rule in panes["left"]["review_rules"]}
    scope_creep = await client.post(f"/v1/diagrams/{key}/chunks/api/gate",
                                    headers=reviewer["headers"],
                                    json={"result": "pass",
                                          "semantic": {**rules, "compiles": {"result": "pass"}}})
    assert scope_creep.json()["error"]["code"] == "review_out_of_scope"
    no_pane = await client.post(f"/v1/diagrams/{key}/chunks/api/gate",
                                headers=reviewer["headers"],
                                json={"result": "fail", "evidence": "assert deleted",
                                      "semantic": {**rules, "no_cheating": {"result": "fail"}}})
    assert no_pane.status_code == 422
    rejected = (await client.post(
        f"/v1/diagrams/{key}/chunks/api/gate", headers=reviewer["headers"],
        json={"result": "fail", "evidence": "the diff deletes the refund assertion the contract "
                                            "requires; coverage is inflated",
              "inconsistent_pane": "middle",
              "semantic": {**rules, "no_cheating": {"result": "fail",
                                                    "note": "assertion removed"}}})).json()
    assert rejected["status"] == "rework"
    assert rejected["gate"]["inconsistent_pane"] == "middle"
    assert set(rejected["gate"]["semantic"]) == set(rules)

    # Resubmitted, green, approved: ready to merge.
    await client.post(f"/v1/diagrams/{key}/chunks/api/submit", headers=executor["headers"],
                      json={"commit": "fff", "files": ["src/api.py"]})
    await client.post(f"/v1/diagrams/{key}/chunks/api/ci", headers=ci_runner["headers"],
                      json={"results": {"compile": {"exit_code": 0}, "test": {"exit_code": 0}}})
    approved = (await client.post(f"/v1/diagrams/{key}/chunks/api/gate",
                                  headers=reviewer["headers"],
                                  json={"result": "pass", "semantic": rules})).json()
    assert approved["status"] == "queued"  # M-27: approval queues, it does not merge
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert shown["progress"]["merge_queue"][0]["chunk_key"] == "api"


# ------------------------------------------------------------ stages 3 and 4


async def _with_contract(client, ws, architect, chunks, key=None):
    """Freeze a diagram, merge its contract through the lightweight path, and return
    the chunks the merge released (each dispatched beforehand, waiting on it)."""
    key, view = await _frozen(client, ws, architect,
                              [_chunk("contract", layer="interface"), *chunks], key)
    for chunk in chunks:
        await client.post(f"/v1/diagrams/{key}/chunks/{chunk['chunk_key']}/dispatch",
                          headers=architect["headers"])
    contract = (await client.post(f"/v1/diagrams/{key}/chunks/contract/dispatch",
                                  headers=architect["headers"])).json()
    builder = await _agent(client, ws)
    gatekeeper = await _agent(client, ws, "AUDITOR")
    await _deliver(client, builder, contract["task_key"])
    await client.post(f"/v1/diagrams/{key}/chunks/contract/gate", headers=gatekeeper["headers"],
                      json={"result": "pass"})
    merged = (await client.post(f"/v1/diagrams/{key}/chunks/contract/merge",
                                headers=ws["person"], json={"artifact": "commit contract1"})).json()
    return key, {item["chunk_key"]: item for item in merged["unblocked"]}


def _rules(extra=()):
    return {rule: {"result": "pass"} for rule in
            ("no_cheating", "contract_compliance", "no_architecture_rot", *extra)}


async def _submit_green(client, key, chunk_key, executor, ci, commit):
    submitted = await client.post(f"/v1/diagrams/{key}/chunks/{chunk_key}/submit",
                                  headers=executor["headers"],
                                  json={"commit": commit, "files": [f"src/{chunk_key}.py"]})
    assert submitted.status_code == 200 and submitted.json()["accepted"] is True, submitted.text
    green = await client.post(f"/v1/diagrams/{key}/chunks/{chunk_key}/ci", headers=ci["headers"],
                              json={"results": {"compile": {"exit_code": 0},
                                                "test": {"exit_code": 0}}})
    assert green.json()["review_woken"] is True, green.text


async def test_a_chunk_without_commands_goes_from_submission_to_review(client):
    """DX-4: no commands, no CI step; the implementer's own checks travel with the
    submission. A chunk that names commands still waits for a green sandbox run."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    key, _ = await _frozen(client, ws, architect, [
        _chunk("design", layer="interface"),
        _chunk("build", layer="interface", commands={"compile": "tsc --noEmit",
                                                     "test": "pytest -q"})])
    for chunk_key in ("design", "build"):
        made = (await client.post(f"/v1/diagrams/{key}/chunks/{chunk_key}/dispatch",
                                  headers=architect["headers"])).json()
        claimed = await client.post(f"/v1/tasks/{made['task_key']}/claim",
                                    headers=executor["headers"], json={})
        assert claimed.status_code == 200, claimed.text

    bad = await client.post(f"/v1/diagrams/{key}/chunks/design/submit",
                            headers=executor["headers"],
                            json={"commit": "d1", "files": ["src/design.py"],
                                  "self_check": [{"cmd": "node check.js", "exit_code": "x"}]})
    assert bad.status_code == 422, bad.text
    check = {"cmd": "node check.js design.html", "exit_code": 0, "output": "7/7 blocks"}
    submitted = await client.post(f"/v1/diagrams/{key}/chunks/design/submit",
                                  headers=executor["headers"],
                                  json={"commit": "d1", "files": ["src/design.py"],
                                        "self_check": [check]})
    assert submitted.json()["status"] == "awaiting_review", submitted.text
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    design = {chunk["chunk_key"]: chunk for chunk in shown["chunks"]}["design"]
    assert design["status"] == "awaiting_review"
    assert design["submission"]["self_check"] == [check]
    panes = (await client.get(f"/v1/diagrams/{key}/chunks/design/triptych",
                              headers=reviewer["headers"])).json()
    assert panes["right"]["ci_required"] is False and panes["right"]["self_check"] == [check]
    reviewed = await client.post(f"/v1/diagrams/{key}/chunks/design/gate",
                                 headers=reviewer["headers"],
                                 json={"result": "pass", "semantic": _rules()})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "queued"

    built = await client.post(f"/v1/diagrams/{key}/chunks/build/submit",
                              headers=executor["headers"],
                              json={"commit": "b1", "files": ["src/build.py"]})
    assert built.json()["status"] == "awaiting_ci", built.text
    early = await client.post(f"/v1/diagrams/{key}/chunks/build/gate",
                              headers=reviewer["headers"],
                              json={"result": "pass", "semantic": _rules()})
    assert early.status_code == 409 and early.json()["error"]["code"] == "ci_not_green"


async def test_a_diagram_without_a_repo_keeps_submitted_files_and_hands_them_on(client):
    """DX-3: files submitted with their content are kept byte for byte, with their
    sha256, beside the diagram; out-of-scope files are vetoed and never stored; the
    merge hands the kept file to the next chunk (with DX-1, DX-2 and DX-4)."""
    import hashlib

    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    key, view = await _frozen(client, ws, architect, [
        _chunk("design", layer="interface", allowed_write_paths=["design.html"]),
        _chunk("page", deps=["design"], allowed_write_paths=["index.html"])])
    design = (await client.post(f"/v1/diagrams/{key}/chunks/design/dispatch",
                                headers=architect["headers"])).json()
    await client.post(f"/v1/diagrams/{key}/chunks/page/dispatch", headers=architect["headers"])
    claimed = await client.post(f"/v1/tasks/{design['task_key']}/claim",
                                headers=executor["headers"], json={})
    assert claimed.status_code == 200, claimed.text

    html = ("<!doctype html>\n<html><body><h1>Name</h1>\n"
            "<table><tr><td>B1</td><td>nav</td></tr></table>\n</body></html>\n")
    stray = await client.post(f"/v1/diagrams/{key}/chunks/design/submit",
                              headers=executor["headers"],
                              json={"files": [{"path": "other.html", "content": html}]})
    assert stray.json()["accepted"] is False and stray.json()["stored"] == []
    doc_base = f"diagrams/{key}/design"
    def encoded(doc_key: str) -> str:
        return "/v1/documents/" + doc_key.replace("/", "%2F")

    missing = await client.get(encoded(f"{doc_base}/other.html"), headers=ws["key"],
                               params={"include_unverified": "true"})
    assert missing.status_code == 404, missing.text

    submitted = await client.post(f"/v1/diagrams/{key}/chunks/design/submit",
                                  headers=executor["headers"],
                                  json={"files": [{"path": "design.html", "content": html}]})
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    digest = hashlib.sha256(html.encode()).hexdigest()
    assert body["accepted"] is True and body["status"] == "awaiting_review"
    assert body["commit"].startswith("content:")
    assert body["stored"] == [{"path": "design.html", "sha256": digest,
                               "doc": f"doc://{doc_base}/design.html", "version": 1}]
    kept = await client.get(encoded(f"{doc_base}/design.html"), headers=ws["key"],
                            params={"include_unverified": "true"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["content"] == html and kept.json()["content_hash"] == digest

    reviewed = await client.post(f"/v1/diagrams/{key}/chunks/design/gate",
                                 headers=reviewer["headers"],
                                 json={"result": "pass", "semantic": _rules()})
    assert reviewed.json()["status"] == "queued", reviewed.text
    verified = await client.post(f"/v1/diagrams/{key}/chunks/design/queue-check",
                                 headers=reviewer["headers"], json={"result": "pass"})
    assert verified.json()["status"] == "verified", verified.text
    merged = await client.post(f"/v1/diagrams/{key}/chunks/design/merge", headers=ws["person"],
                               json={})
    assert merged.status_code == 200, merged.text
    assert merged.json()["merge"]["artifact"] == body["commit"]
    assert merged.json()["merge"]["delivered"]["files"] == [{"path": "design.html",
                                                             "sha256": digest}]
    [page] = merged.json()["unblocked"]
    claimed = await client.post(f"/v1/tasks/{page['task_key']}/claim",
                                headers=executor["headers"], json={})
    [context] = [item for item in claimed.json()["context"] if not item.get("is_continuation")]
    assert context["doc_ref"]["doc_key"] == f"{doc_base}/design.html"
    assert context["content"] == html


def test_acceptance_kinds():
    """DX-6: a criterion only a person can settle is MANUAL."""
    from app.services.diagrams import acceptance_kind

    for manual in ("owner 在 M1 确认通过", "The owner approves the copy", "人工验收",
                   "Signed off by a person"):
        assert acceptance_kind(manual) == "MANUAL", manual
    for auto in ("设计稿覆盖全部 7 个页面区块", "pytest tests/test_api.py", "LCP <= 2.5s"):
        assert acceptance_kind(auto) == "AUTO", auto


async def test_confirm_names_manual_and_unchecked_criteria(client):
    """DX-6: confirming shows what a person settles, what nothing checks, and an
    unanswered self-check; the dispatched task carries the right kinds."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    key = f"dx6-{uuid.uuid4().hex[:6]}"
    design = _chunk("design", layer="interface")
    design["done_criteria"] = ["设计稿覆盖全部 7 个页面区块", "owner 在 M1 确认通过"]
    built = _chunk("build", layer="interface", commands={"test": "pytest -q"})
    made = await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": key, "title": "site", "chunks": [design, built],
        "self_check": {"big": True}})
    assert made.status_code == 201, made.text
    confirmed = await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["person"])
    assert confirmed.status_code == 200, confirmed.text
    warnings = confirmed.json()["confirm_warnings"]
    by_kind = {(item["kind"], item.get("chunk_key")): item for item in warnings}
    assert by_kind[("self_check", None)]["missing"] == ["stable", "machine_checkable", "long"]
    assert by_kind[("manual_criteria", "design")]["criteria"] == ["owner 在 M1 确认通过"]
    assert by_kind[("auto_without_commands", "design")]["criteria"] == [
        "设计稿覆盖全部 7 个页面区块"]
    assert ("auto_without_commands", "build") not in by_kind

    await client.post(f"/v1/diagrams/{key}/freeze", headers=ws["person"], json={})
    dispatched = (await client.post(f"/v1/diagrams/{key}/chunks/design/dispatch",
                                    headers=architect["headers"])).json()
    task = (await client.get(f"/v1/tasks/{dispatched['task_key']}", headers=ws["key"])).json()
    assert [(item["id"], item["kind"]) for item in task["acceptance"]] == [
        ("DC-1", "AUTO"), ("DC-2", "MANUAL")]


async def test_serial_merge_queue(client):
    """A-11 / A-14: approval queues; the controller verifies the head; one at a time."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    ci = await _agent(client, ws)
    controller = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    names = ["p1", "p2", "p3", "p4", "p5"]
    key, released = await _with_contract(client, ws, architect,
                                         [_chunk(name, deps=["contract"]) for name in names])
    assert sorted(released) == names
    executors = {}
    for name in names:  # five parallel READY chunks, five executors
        executors[name] = await _agent(client, ws)
        claimed = await client.post(f"/v1/tasks/{released[name]['task_key']}/claim",
                                    headers=executors[name]["headers"], json={})
        assert claimed.status_code == 200, claimed.text
        await _submit_green(client, key, name, executors[name], ci, f"c-{name}")
        approved = (await client.post(f"/v1/diagrams/{key}/chunks/{name}/gate",
                                      headers=reviewer["headers"],
                                      json={"result": "pass", "semantic": _rules()})).json()
        assert approved["status"] == "queued"

    line = (await client.get(f"/v1/diagrams/{key}/queue", headers=ws["key"])).json()
    assert [item["chunk_key"] for item in line["items"]] == names
    assert [item["position"] for item in line["items"]] == [1, 2, 3, 4, 5]
    assert line["head"] == "p1" and all("waiting_seconds" in item for item in line["items"])
    # Not merged straight from approval, and not out of order.
    direct = await client.post(f"/v1/diagrams/{key}/chunks/p2/merge", headers=ws["person"],
                               json={})
    assert direct.json()["error"]["code"] == "queue_not_verified"
    assert direct.json()["error"]["details"]["position"] == 2
    jumped = await client.post(f"/v1/diagrams/{key}/chunks/p2/queue-check",
                               headers=controller["headers"], json={"result": "pass"})
    assert jumped.json()["error"]["code"] == "not_queue_head"
    by_owner = await client.post(f"/v1/diagrams/{key}/chunks/p1/queue-check",
                                 headers=executors["p1"]["headers"], json={"result": "pass"})
    assert by_owner.status_code == 403

    merged_order = []
    for name in names:
        if name == "p3":
            # A rebase conflict goes back to its implementer with the evidence.
            back = (await client.post(f"/v1/diagrams/{key}/chunks/p3/queue-check",
                                      headers=controller["headers"],
                                      json={"result": "rebase_conflict",
                                            "evidence": "CONFLICT in src/shared_util.py"})).json()
            assert back["status"] == "rework"
            continue
        verified = (await client.post(f"/v1/diagrams/{key}/chunks/{name}/queue-check",
                                      headers=controller["headers"],
                                      json={"result": "pass", "rebased_commit": f"r-{name}"})
                    ).json()
        assert verified["status"] == "verified"
        refused = await client.post(f"/v1/diagrams/{key}/chunks/{name}/merge",
                                    headers=executors[name]["headers"], json={})
        assert refused.status_code == 403
        merged = (await client.post(f"/v1/diagrams/{key}/chunks/{name}/merge",
                                    headers=ws["person"], json={})).json()
        assert merged["merge"]["via_queue"] is True and merged["merge"]["artifact"] == f"r-{name}"
        merged_order.append(name)
    assert merged_order == ["p1", "p2", "p4", "p5"]
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert shown["progress"]["merge_queue"] == []
    p3 = next(c for c in shown["chunks"] if c["chunk_key"] == "p3")
    assert p3["status"] == "rework" and p3["queue_check"]["result"] == "rebase_conflict"
    inbox = (await client.get("/v1/inbox", headers=executors["p3"]["headers"])).json()["items"]
    assert any("chunk_queue_returned" in str(item.get("kind") or item.get("type"))
               for item in inbox)


async def test_trust_circuit_breaker_and_upgrade(client):
    """A-12 / A-15 (trust): -10 per sandbox failure, -20 per rejection, warn at two,
    break at three, upgrade restarts at 80 with the escalation counted."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    ci = await _agent(client, ws)
    reviewer = await _agent(client, ws, "AUDITOR")
    executor = await _agent(client, ws)
    strong = await _agent(client, ws)
    key, released = await _with_contract(client, ws, architect, [_chunk("calc", deps=["contract"])])
    task_key = released["calc"]["task_key"]
    await client.post(f"/v1/tasks/{task_key}/claim", headers=executor["headers"], json={})

    await client.post(f"/v1/diagrams/{key}/chunks/calc/submit", headers=executor["headers"],
                      json={"commit": "x0", "files": ["src/calc.py"]})
    red = (await client.post(f"/v1/diagrams/{key}/chunks/calc/ci", headers=ci["headers"],
                             json={"results": {"compile": {"exit_code": 0},
                                               "test": {"exit_code": 1, "stderr": "1 failed"}}})
           ).json()
    assert red["status"] == "rework"
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert shown["progress"]["trust"]["calc"] == 90

    reject = {"result": "fail", "evidence": "hard-coded expected value",
              "inconsistent_pane": "middle",
              "semantic": {**_rules(), "no_cheating": {"result": "fail"}}}
    statuses = []
    for attempt in range(3):
        await _submit_green(client, key, "calc", executor, ci, f"x{attempt + 1}")
        result = (await client.post(f"/v1/diagrams/{key}/chunks/calc/gate",
                                    headers=reviewer["headers"], json=reject)).json()
        statuses.append((result["status"], result["review_reject_count"]))
        if result["status"] == "blocked":
            break
    # 90 -> 70 -> 50 (under 60: the circuit breaks at the second rejection already).
    assert statuses[0] == ("rework", 1)
    assert statuses[1][0] == "blocked"
    chunk = next(c for c in (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])
                             ).json()["chunks"] if c["chunk_key"] == "calc")
    assert chunk["block_kind"] == "circuit" and "trust" in chunk["block_reason"]
    task = (await client.get(f"/v1/tasks/{task_key}", headers=ws["key"])).json()
    assert task["status"] == "CANCELLED"  # the implementer lost the chunk
    assert [entry["cost"] for entry in chunk["trust_log"]] == [10, 20, 20]

    by_implementer = await client.post(f"/v1/diagrams/{key}/chunks/calc/escalate",
                                       headers=executor["headers"], json={"mode": "upgrade"})
    assert by_implementer.status_code == 403
    upgraded = (await client.post(f"/v1/diagrams/{key}/chunks/calc/escalate",
                                  headers=architect["headers"],
                                  json={"mode": "upgrade", "note": "stronger model",
                                        "assignee": f"agent:{strong['signature']}"})).json()
    assert upgraded["trust_score"] == 80 and upgraded["escalation_count"] == 1
    new_task = (await client.get(f"/v1/tasks/{upgraded['dispatch']['task_key']}",
                                 headers=ws["key"])).json()
    assert new_task["assignee"] == f"agent:{strong['signature']}"
    assert new_task["input_context"]["escalation"]["escalation"] == 1
    chunk = next(c for c in (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])
                             ).json()["chunks"] if c["chunk_key"] == "calc")
    assert chunk["review_reject_count"] == 0 and chunk["status"] == "dispatched"
    chain = [e["type"] for e in (await client.get(f"/v1/diagrams/{key}/replay", headers=ws["key"],
                                                  params={"chunk": "calc"})).json()["events"]]
    assert "timeline.chunk_circuit_broken" in chain and "timeline.chunk_escalated" in chain


async def test_three_rejections_break_and_warning_at_two(client):
    """A-12: with trust to spare, the second rejection warns and the third breaks."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    reviewer = await _agent(client, ws, "AUDITOR")
    executor = await _agent(client, ws)
    watcher = await _agent(client, ws)
    key = f"cb-{uuid.uuid4().hex[:6]}"
    await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": key, "title": "x", "watchers": [f"agent:{watcher['signature']}"],
        "chunks": [_chunk("solo", layer="interface")]})
    await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["person"])
    await client.post(f"/v1/diagrams/{key}/freeze", headers=ws["person"], json={})
    solo = (await client.post(f"/v1/diagrams/{key}/chunks/solo/dispatch",
                              headers=architect["headers"])).json()
    # The lightweight path counts rejections too; trust stays 100 - 20 * n.
    for attempt in range(3):
        await _deliver(client, executor, solo["task_key"]) if attempt == 0 else None
        result = (await client.post(f"/v1/diagrams/{key}/chunks/solo/gate",
                                    headers=reviewer["headers"],
                                    json={"result": "fail", "evidence": f"round {attempt}"})).json()
        if attempt < 2:
            assert result["status"] == "rework"
        if attempt == 1:
            inbox = (await client.get("/v1/inbox", headers=watcher["headers"])).json()["items"]
            assert any("chunk_rejection_warning" in str(i.get("kind") or i.get("type"))
                       for i in inbox)
            # Back for another round: the implementer resubmits.
            await client.post(f"/v1/tasks/{solo['task_key']}/claim", headers=executor["headers"],
                              json={})
    assert result["status"] == "blocked" and result["circuit"]["reason"].startswith("3 ")
    spec = (await client.post(f"/v1/diagrams/{key}/chunks/solo/escalate",
                              headers=ws["person"], json={"mode": "spec"})).json()
    assert spec["suspended"] == ["solo"]


async def test_token_budget_blocks_until_a_person_raises_it(client):
    """A-15 (tokens)."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    key, released = await _with_contract(client, ws, architect,
                                         [_chunk("big", deps=["contract"], token_budget=1000)])
    await client.post(f"/v1/tasks/{released['big']['task_key']}/claim",
                      headers=executor["headers"], json={})
    first = (await client.post(f"/v1/diagrams/{key}/chunks/big/usage",
                               headers=executor["headers"], json={"tokens": 600})).json()
    assert first["blocked"] is False
    over = (await client.post(f"/v1/diagrams/{key}/chunks/big/usage",
                              headers=executor["headers"], json={"tokens": 500})).json()
    assert over["blocked"] is True and over["token_consumed"] == 1100
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    big = next(c for c in shown["chunks"] if c["chunk_key"] == "big")
    assert big["status"] == "blocked" and big["block_kind"] == "budget"
    assert shown["progress"]["tokens"]["big"] == {"consumed": 1100, "budget": 1000}
    assert any(w["kind"] == "budget" for w in shown["warnings"])
    agent_renews = await client.post(f"/v1/diagrams/{key}/chunks/big/budget",
                                     headers=architect["headers"], json={"token_budget": 5000})
    assert agent_renews.status_code == 403  # never renewed automatically or by an agent
    renewed = (await client.post(f"/v1/diagrams/{key}/chunks/big/budget", headers=ws["person"],
                                 json={"token_budget": 5000})).json()
    assert renewed["resumed"] is True
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert next(c for c in shown["chunks"] if c["chunk_key"] == "big")["status"] != "blocked"


async def test_need_graph_mutation_suspends_and_replans(client):
    """A-13: the subgraph is suspended, merged work stays, a change resumes it."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    executor = await _agent(client, ws)
    key, released = await _with_contract(client, ws, architect, [
        _chunk("store", deps=["contract"]), _chunk("report", deps=["store"]),
        _chunk("export", deps=["contract"])])
    assert sorted(released) == ["export", "store"]
    await client.post(f"/v1/diagrams/{key}/chunks/report/dispatch", headers=architect["headers"])
    old_task = released["store"]["task_key"]
    await client.post(f"/v1/tasks/{old_task}/claim", headers=executor["headers"], json={})
    others = await client.post(f"/v1/diagrams/{key}/chunks/store/signal",
                               headers=architect["headers"], json={"reason": "x"})
    assert others.status_code == 403
    signalled = (await client.post(f"/v1/diagrams/{key}/chunks/store/signal",
                                   headers=executor["headers"],
                                   json={"reason": "the storage driver has no transactions"})
                 ).json()
    assert signalled["suspended"] == ["report", "store"]
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    statuses = {c["chunk_key"]: c["status"] for c in shown["chunks"]}
    assert statuses == {"contract": "merged", "store": "blocked_by_spec",
                        "report": "blocked_by_spec", "export": "dispatched"}
    submit = await client.post(f"/v1/diagrams/{key}/chunks/store/submit",
                               headers=executor["headers"],
                               json={"commit": "z", "files": ["src/store.py"]})
    assert submit.status_code == 409  # suspended work does not move

    # The architect re-plans the unfinished subgraph only.
    merged_edit = await client.post(f"/v1/diagrams/{key}/changes", headers=architect["headers"],
                                    json={"summary": "touch the contract",
                                          "patch": {"update": [{"chunk_key": "contract",
                                                                "scope": "x"}]}})
    assert merged_edit.json()["error"]["code"] == "chunk_in_flight"
    change = (await client.post(f"/v1/diagrams/{key}/changes", headers=architect["headers"],
                                json={"summary": "store keeps an append-only log instead",
                                      "patch": {"update": [{"chunk_key": "store",
                                                            "scope": "append-only log",
                                                            "icd": {"exports": ["append()"]}}],
                                                "add": [_chunk("compact", deps=["store"])]}})
              ).json()
    assert change["status"] == "pending", change
    decided = (await client.post(f"/v1/diagrams/{key}/changes/{change['change_key']}/decide",
                                 headers=ws["person"], json={"decision": "approve"})).json()
    resumed = {item["chunk_key"]: item for item in decided["dispatched"]}
    assert resumed["store"]["state"] == "dispatched"
    assert resumed["report"]["state"] == "blocked"  # waits on the new store
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    statuses = {c["chunk_key"]: c["status"] for c in shown["chunks"]}
    assert statuses["contract"] == "merged" and statuses["export"] == "dispatched"
    assert statuses["store"] == "dispatched" and statuses["compact"] == "blocked"
    store = next(c for c in shown["chunks"] if c["chunk_key"] == "store")
    assert store["task_key"] != old_task and store["graph_mutation_signal"] is None
    assert (await client.get(f"/v1/tasks/{old_task}", headers=ws["key"])).json()["status"] == \
        "CANCELLED"
    chain = [e["type"] for e in (await client.get(f"/v1/diagrams/{key}/replay",
                                                  headers=ws["key"])).json()["events"]]
    assert "timeline.subgraph_suspended" in chain and "timeline.subgraph_resumed" in chain


async def test_command_view_at_thirty_agents_and_board_alerts(client):
    """A-04: one read gives the distribution and the blocked list for a 30-chunk,
    30-agent diagram; a chunk that disagrees with the board raises an alert."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")
    names = [f"w{index:02d}" for index in range(29)]
    key, released = await _with_contract(client, ws, architect,
                                         [_chunk(name, deps=["contract"]) for name in names])
    assert len(released) == 29
    agents = [await _agent(client, ws) for _ in range(30)]

    async def claim(index):
        return await client.post(f"/v1/tasks/{released[names[index]]['task_key']}/claim",
                                 headers=agents[index]["headers"], json={})

    claimed = await asyncio.gather(*[claim(index) for index in range(20)])
    assert all(response.status_code == 200 for response in claimed)
    started = time.monotonic()
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    elapsed = time.monotonic() - started
    assert elapsed < 3, elapsed
    assert shown["progress"]["distribution"] == {"merged": 1, "in_progress": 20,
                                                 "dispatched": 9}
    assert shown["progress"]["total"] == 30 and shown["board_consistency"]["ok"] is True

    # The board is the truth: a chunk whose task vanished is flagged and alerted.
    conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute("UPDATE diagram_chunks SET task_key = 'GONE-1' WHERE chunk_key = 'w28' "
                           "AND workspace_id = $1::uuid", ws["id"])
    finally:
        await conn.close()
    shown = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    assert shown["board_consistency"]["ok"] is False
    assert any(w["kind"] == "board_mismatch" for w in shown["warnings"])
    found = None
    for _ in range(90):
        alerts = (await client.get(f"/v1/admin/workspaces/{ws['id']}/alerts",
                                   headers=ADMIN)).json()["items"]
        found = next((a for a in alerts if a["rule"] == "diagram_board_mismatch"), None)
        if found:
            break
        await asyncio.sleep(1)
    assert found is not None and found["subject"]["ref"] == f"{key}:w28"


async def test_diagram_mcp_tool(client):
    """Agents drive the same flow over MCP with one tool."""
    ws = await _workspace(client)
    architect = await _agent(client, ws, "ANALYST")

    async def call(headers, **arguments):
        response = await client.post("/mcp/", headers={
            **headers, "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "diagram", "arguments": arguments}})
        assert response.status_code == 200, response.text
        text = response.text
        if text.startswith("event:") or "data:" in text[:20]:
            text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
        return json.loads(text)["result"]

    key = f"mcp-{uuid.uuid4().hex[:6]}"
    created = await call(architect["headers"], action="create", args={
        "diagram_key": key, "title": "over MCP", "chunks": [_chunk("api", layer="interface")]})
    assert not created.get("isError"), created
    listed = await call(architect["headers"], action="list")
    assert key in str(listed["content"][0]["text"])
    wrong = await call(architect["headers"], action="explode", diagram_key=key)
    assert wrong.get("isError") is True
    await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["person"])
    await client.post(f"/v1/diagrams/{key}/freeze", headers=ws["person"], json={})
    dispatched = await call(architect["headers"], action="dispatch", diagram_key=key,
                            chunk_key="api")
    assert '"dispatched"' in dispatched["content"][0]["text"]
