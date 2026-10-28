"""Diagrams and chunked dispatch, lightweight stage (agentgraph/spec-diagram-dispatch v3).

Covers spec A-01 (end to end), A-02 (frozen diagram, changes), A-03 (gate and
merge separation), A-04 (command view basics), A-05 (replay), A-06 (all of it
by hand, no automation), A-07 (self-check) and section 16: A-16 (fast lane),
A-17 (super admin for the owner, then a person's override), A-18 (no
injection into chunks in flight).
"""

from __future__ import annotations

import uuid

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
              _chunk("ui", deps=["core"]), _chunk("docs")]
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
        "timeline.chunk_dispatched", "timeline.chunk_gated", "timeline.chunk_gated",
        "timeline.chunk_merged"]
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
    assert "api-paging" not in final["progress"]["distribution"] or all(
        c["status"] == "retired" for c in final["chunks"] if c["chunk_key"] == "api-paging")
    chain = (await client.get(f"/v1/diagrams/{key}/replay", headers=ws["key"])).json()
    decisions = [e["details"] for e in chain["events"]
                 if e["type"] == "timeline.diagram_change_decided"]
    assert {d["decision_kind"] for d in decisions} == {"person", "super_admin_for_owner"}
    assert any(e["type"] == "timeline.diagram_change_revoked"
               and e["details"]["overrode"] == "super_admin_for_owner" for e in chain["events"])
