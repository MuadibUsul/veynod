"""One worker role, and a hand-off that ends the task (docs/LOOP_SIMPLE_MODEL.md).

Under full automation an agent's hand-off to review finishes the task and
releases what depends on it. What a person or an AUDITOR should still see is a
flag with the reasons, which holds nothing back. Outside full automation, in a
namespace kept reviewed and for tasks with a repository, the review step stays.
"""

from __future__ import annotations

import uuid

import pytest

from tests.test_loop_reliability import _fetch
from tests.test_scheduler import ADMIN, _workspace

pytestmark = pytest.mark.integration

BRIEF = {"summary": "done", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}


async def _agent(client, ws: dict, role: str = "EXECUTOR") -> dict:
    name = f"w-{uuid.uuid4().hex[:6]}"
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": role, "allowed_namespaces": ["*"], "agent_signature": name})
    assert minted.status_code == 201, minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"}, "name": name}


async def _task(client, ws: dict, **fields) -> str:
    key = f"tr-{uuid.uuid4().hex[:8]}"
    made = await client.post("/v1/tasks", headers=ws["owner"], json={
        "task_key": key, "title": "A piece of work", "assigned_role": "EXECUTOR",
        "namespace": "default", **fields})
    assert made.status_code == 201, made.text
    return key


async def _work(client, agent: dict, key: str, brief: dict | None = None) -> dict:
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=agent["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    done = await client.post(f"/v1/tasks/{key}/handoff", headers=agent["headers"], json={
        "next_status": "REVIEWING", "brief": brief or BRIEF,
        "fencing_token": claimed.json()["task"]["fencing_token"]})
    assert done.status_code == 200, done.text
    return done.json()


async def _row(client, ws: dict, key: str) -> dict:
    return (await client.get(f"/v1/tasks/{key}", headers=ws["owner"])).json()


async def test_a_hand_off_ends_the_task_and_releases_what_waits_on_it(client) -> None:
    ws = await _workspace(client, "full")
    worker = await _agent(client, ws)
    first = await _task(client, ws)
    second = await _task(client, ws, depends_on=[first], status="TODO")
    assert (await _row(client, ws, second))["status"] == "TODO"

    result = await _work(client, worker, first)
    assert result["status"] == "DONE", result
    row = await _row(client, ws, first)
    assert row["status"] == "DONE"
    # Nothing waits for a reviewer: the next task is free at once.
    assert (await _row(client, ws, second))["status"] == "READY"
    # No review was queued for it.
    placements = (await client.get("/v1/runner/placements", headers=ws["owner"],
                                   params={"task_key": first})).json()["items"]
    assert [item for item in placements if item["step"] == "review"] == []
    events = await _fetch(
        "SELECT payload FROM timeline_streams WHERE workspace_id = $1::uuid "
        "AND event_type = 'TASK_HANDOFF' AND payload->>'task_key' = $2", ws["id"], first)
    assert '"trusted": true' in events[0]["payload"]


async def test_what_should_be_looked_at_is_flagged_with_the_reason_and_holds_nothing(
        client) -> None:
    ws = await _workspace(client, "full")
    worker = await _agent(client, ws)
    risky = await _task(client, ws)
    after = await _task(client, ws, depends_on=[risky], status="TODO")
    manual = await _task(client, ws, acceptance=[
        {"id": "look", "text": "the owner reads the copy", "kind": "MANUAL"}])
    money = await _task(client, ws)

    await _work(client, worker, risky, {**BRIEF, "risk_flag": True,
                                        "key_metrics": {"acceptance_not_met": ["AC-1"]}})
    await _work(client, worker, manual)
    await _work(client, worker, money, {**BRIEF, "owner_topic": "money"})

    row = await _row(client, ws, risky)
    assert (row["status"], row["needs_attention"]) == ("DONE", True)
    assert row["attention_reasons"][0].startswith("risk_flag")
    assert row["attention_reasons"][1].startswith("acceptance_not_met")
    assert (await _row(client, ws, after))["status"] == "READY", "a flag is not a gate"
    assert (await _row(client, ws, manual))["attention_reasons"] == ["manual_acceptance: look"]
    assert (await _row(client, ws, money))["attention_reasons"] == ["owner_topic: money"]

    queue = (await client.get("/v1/tasks", headers=ws["owner"],
                              params={"needs_attention": "true"})).json()["items"]
    assert {item["task_key"] for item in queue} == {risky, manual, money}
    # The owner sees them on the Loop page, with the reasons, beside what blocks work.
    look = (await client.get("/v1/loop/overview", headers=ws["owner"])).json()["needs_you"]["look"]
    assert {item["key"] for item in look} == {risky, manual, money}
    assert next(item for item in look if item["key"] == money)["reasons"] == ["owner_topic: money"]


async def test_a_look_clears_the_flag_or_opens_a_follow_up(client) -> None:
    ws = await _workspace(client, "full")
    worker = await _agent(client, ws)
    auditor = await _agent(client, ws, "AUDITOR")
    key = await _task(client, ws)
    other = await _task(client, ws)
    for item in (key, other):
        await _work(client, worker, item, {**BRIEF, "risk_flag": True})

    # Whoever built it does not judge it; a look needs a decision the API knows.
    own = await client.post(f"/v1/tasks/{key}/attention", headers=worker["headers"],
                            json={"decision": "ok"})
    assert own.status_code == 403, own.text
    assert (await client.post(f"/v1/tasks/{key}/attention", headers=auditor["headers"],
                              json={"decision": "redo"})).status_code == 422

    fine = await client.post(f"/v1/tasks/{key}/attention", headers=auditor["headers"],
                             json={"decision": "ok"})
    assert fine.status_code == 200, fine.text
    row = await _row(client, ws, key)
    assert (row["status"], row["needs_attention"], row["reviewed_by"]) == (
        "DONE", False, auditor["name"])
    again = await client.post(f"/v1/tasks/{key}/attention", headers=auditor["headers"],
                              json={"decision": "ok"})
    assert again.status_code == 409

    # A finished task is not reopened: the fix is a new task that says what to fix.
    redo = await client.post(f"/v1/tasks/{other}/attention", headers=ws["owner"],
                             json={"decision": "redo", "note": "the totals are wrong"})
    assert redo.status_code == 200, redo.text
    follow = await _row(client, ws, redo.json()["follow_up"])
    assert follow["status"] == "READY" and "the totals are wrong" in follow["description"]
    assert (await _row(client, ws, other))["status"] == "DONE"


async def test_the_review_step_stays_where_it_was_asked_for(client) -> None:
    # Not full automation: a hand-off waits for its review as before.
    semi = await _workspace(client, "semi")
    worker = await _agent(client, semi)
    key = await _task(client, semi)
    assert (await _work(client, worker, key))["status"] == "REVIEWING"
    assert (await _row(client, semi, key))["needs_attention"] is False

    # Full automation, but a task with a repository keeps its gate.
    full = await _workspace(client, "full")
    builder = await _agent(client, full)
    code = await _task(client, full, repo="acme/site")
    assert (await _work(client, builder, code))["status"] == "REVIEWING"
    # A person finishing a task is a person's decision, not trust.
    plain = await _task(client, full)
    claimed = await client.post(f"/v1/tasks/{plain}/claim", headers=builder["headers"], json={})
    explicit = await client.post(f"/v1/tasks/{plain}/handoff", headers=builder["headers"], json={
        "next_status": "HANDOFF", "brief": BRIEF,
        "fencing_token": claimed.json()["task"]["fencing_token"]})
    assert explicit.json()["status"] == "HANDOFF", "passing work on is not finishing it"


async def test_an_executor_and_an_analyst_take_each_others_tasks(client) -> None:
    ws = await _workspace(client, "full")
    executor = await _agent(client, ws, "EXECUTOR")
    analyst = await _agent(client, ws, "ANALYST")
    research = await _task(client, ws, assigned_role="ANALYST")
    build = await _task(client, ws, assigned_role="EXECUTOR")
    strict = await _task(client, ws, assigned_role="ANALYST", required_role="ANALYST")
    audit = await _task(client, ws, assigned_role="AUDITOR")

    listed = (await client.get("/v1/tasks", headers=executor["headers"],
                               params={"claimable_by_me": "true"})).json()["items"]
    keys = {item["task_key"] for item in listed}
    assert research in keys and build in keys
    assert strict not in keys and audit not in keys

    assert (await client.post(f"/v1/tasks/{research}/claim", headers=executor["headers"],
                              json={})).status_code == 200
    assert (await client.post(f"/v1/tasks/{build}/claim", headers=analyst["headers"],
                              json={})).status_code == 200
    # A role its author required, and work that is not a worker's, stay closed.
    assert (await client.post(f"/v1/tasks/{strict}/claim", headers=executor["headers"],
                              json={})).status_code == 403
    assert (await client.post(f"/v1/tasks/{audit}/claim", headers=executor["headers"],
                              json={})).status_code == 403


async def test_a_routine_sample_is_flagged_as_such(client) -> None:
    from app.config import get_settings
    from app.services.quality import _sampled

    ws = await _workspace(client, "full")
    worker = await _agent(client, ws)
    sampled = plain = None
    for _ in range(60):
        key = await _task(client, ws)
        task_id = (await _fetch("SELECT id FROM collab_tasks WHERE task_key = $1", key))[0]["id"]
        if _sampled(task_id, get_settings().trust_sample_rate):
            sampled = sampled or key
        else:
            plain = plain or key
        if sampled and plain:
            break
    assert sampled and plain
    await _work(client, worker, sampled)
    await _work(client, worker, plain)
    assert (await _row(client, ws, sampled))["attention_reasons"] == [
        "sampled: a routine look, nothing was flagged"]
    assert (await _row(client, ws, plain))["needs_attention"] is False


# ------------------------------------------------------------------ acceptance checks


async def test_an_auto_check_judges_the_item_instead_of_a_person(client) -> None:
    ws = await _workspace(client, "full")
    worker = await _agent(client, ws)
    written = f"notes-{uuid.uuid4().hex[:6]}"
    put = await client.put(f"/v1/documents/{written}", headers=worker["headers"], json={
        "namespace": "default", "content": "# Plan\nship on Friday", "expected_version": 0})
    assert put.status_code in (200, 201), put.text
    good = await _task(client, ws, acceptance=[
        {"id": "plan", "text": "the plan is written", "kind": "AUTO",
         "check": {"type": "document_exists", "doc_key": written, "contains": "Friday"}}])
    bad = await _task(client, ws, acceptance=[
        {"id": "plan", "text": "the plan is written", "kind": "AUTO",
         "check": {"type": "document_exists", "doc_key": "never-written"}},
        {"id": "ci", "text": "CI is green", "kind": "AUTO",
         "check": {"type": "gate", "gate": "CI"}}])
    await _work(client, worker, good)
    await _work(client, worker, bad)

    row = await _row(client, ws, good)
    assert (row["status"], row["needs_attention"]) == ("DONE", False)
    assert row["check_results"] == [{"id": "plan", "type": "document_exists", "passed": True,
                                     "detail": f"document {written} is there"}]
    assert row["acceptance_state"] == [{"id": "plan", "status": "PASS"}]
    row = await _row(client, ws, bad)
    # Finished on trust, and flagged with what failed: nothing waits for a person.
    assert (row["status"], row["needs_attention"]) == ("DONE", True)
    assert row["attention_reasons"] == [
        "check_failed: plan: document never-written was not written",
        "check_failed: ci: no CI result was reported"]
    assert [item["status"] for item in row["acceptance_state"]] == ["FAIL", "FAIL"]


async def test_a_command_check_is_reported_by_the_computer_that_did_the_work(client) -> None:
    ws = await _workspace(client, "full")
    worker = await _agent(client, ws)
    other = await _agent(client, ws)
    key = await _task(client, ws, acceptance=[
        {"id": "tests", "text": "the tests pass", "kind": "AUTO",
         "check": {"type": "command", "run": "pytest -q"}}])
    await _work(client, worker, key)
    row = await _row(client, ws, key)
    assert row["check_results"] == [{"id": "tests", "type": "command", "passed": None,
                                     "detail": "waits for the computer that did the work"}]
    assert row["needs_attention"] is False and row["acceptance_state"][0]["status"] == "PASS"

    refused = await client.post(f"/v1/tasks/{key}/checks", headers=other["headers"],
                                json={"results": [{"id": "tests", "passed": True}]})
    assert refused.status_code == 403, refused.text
    unknown = await client.post(f"/v1/tasks/{key}/checks", headers=worker["headers"],
                                json={"results": [{"id": "nope", "passed": True}]})
    assert unknown.status_code == 422, unknown.text
    failed = await client.post(f"/v1/tasks/{key}/checks", headers=worker["headers"], json={
        "results": [{"id": "tests", "passed": False, "detail": "exit 1: 2 failed"}]})
    assert failed.status_code == 200, failed.text
    row = await _row(client, ws, key)
    assert (row["status"], row["needs_attention"]) == ("DONE", True)
    assert row["attention_reasons"] == ["check_failed: tests: exit 1: 2 failed"]
    assert row["acceptance_state"] == [{"id": "tests", "status": "FAIL"}]


async def test_a_check_belongs_to_an_auto_item_and_says_what_it_needs(client) -> None:
    ws = await _workspace(client, "full")
    for acceptance in (
        [{"id": "a", "text": "x", "kind": "MANUAL", "check": {"type": "gate", "gate": "CI"}}],
        [{"id": "a", "text": "x", "kind": "AUTO", "check": {"type": "command"}}],
        [{"id": "a", "text": "x", "kind": "AUTO", "check": {"type": "http", "url": "https://x"}}],
    ):
        made = await client.post("/v1/tasks", headers=ws["owner"], json={
            "title": "bad check", "namespace": "default", "acceptance": acceptance})
        assert made.status_code == 422, (acceptance, made.text)
