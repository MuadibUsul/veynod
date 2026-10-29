"""Review, merge and supervise steps: the Loop turns without a person (runner-3 follow-up)."""

from __future__ import annotations

import uuid

import pytest

from tests.test_scheduler import REPO, _placements, _schedule, _sql, _task, _task_row, _workspace

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
BRIEF = {"summary": "done", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}


async def _host(client, ws: dict, name: str, *, role: str = "EXECUTOR",
                capabilities: list[str] | None = None, slots: int = 2) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": role, "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    named = await client.post("/v1/agents/register", headers=headers, json={
        "agent_id": name, "capabilities": capabilities or []})
    assert named.status_code in (200, 201), named.text
    hello = await client.post("/v1/runner/hello", headers=headers, json={"host": {
        "os": "linux", "arch": "x64", "max_concurrency": slots, "repos": [REPO],
        "runtimes": [{"id": "claude", "available": True}]}})
    assert hello.status_code == 200, hello.text
    return {"headers": headers, "agent_id": name, "host_id": hello.json()["host_id"],
            "assignee": f"agent:{name}"}


async def _placed(client, host: dict) -> list[dict]:
    """The task.placed pokes a host received, newest last."""
    inbox = (await client.get("/v1/inbox", headers=host["headers"],
                              params={"status": "all", "limit": 200})).json()["items"]
    return [item for item in inbox if item["kind"] == "task.placed"]


def _chunk(key: str, layer: str = "implementation", deps: tuple = ()) -> dict:
    return {"chunk_key": key, "title": key, "scope": f"build {key}", "layer": layer,
            "icd": {"exports": [f"{key}()"]}, "done_criteria": [f"{key} tests pass"],
            "depends_on": list(deps), "allowed_write_paths": [f"src/{key}/"]}


async def _open(client, ws: dict, key: str) -> dict | None:
    items = await _placements(client, ws, key)
    return next((item for item in items if item["status"] in ("offered", "claimed")), None)


async def test_a_diagram_goes_from_freeze_to_merged_without_a_person(client):
    ws = await _workspace(client, "full")
    worker = await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}", capabilities=["review"])
    reviewer = await _host(client, ws, f"rv-{uuid.uuid4().hex[:4]}", role="AUDITOR")
    key = f"loop-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/diagrams", headers=ws["owner"], json={
        "diagram_key": key, "title": "Loop", "repo": REPO,
        "chunks": [_chunk("api", "interface"), _chunk("ui", deps=("api",))],
        "self_check": {"big": True, "stable": True, "machine_checkable": True, "long": True}})
    assert made.status_code == 201, made.text
    assert made.json()["repo"] == REPO
    confirmed = await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["owner"])
    assert confirmed.status_code == 200, confirmed.text
    frozen = await client.post(f"/v1/diagrams/{key}/freeze", headers=ws["owner"], json={})
    assert frozen.status_code == 200, frozen.text
    states = {chunk["chunk_key"]: chunk for chunk in frozen.json()["chunks"]}
    # Full automation dispatches the first chunk with the freeze; ui waits on api.
    assert states["api"]["status"] == "dispatched" and states["ui"]["status"] == "blocked"

    for chunk_key in ("api", "ui"):
        view = (await client.get(f"/v1/diagrams/{key}", headers=ws["owner"])).json()
        task_key = next(c["task_key"] for c in view["chunks"] if c["chunk_key"] == chunk_key)
        task = await _task_row(client, ws, task_key)
        assert task["repo"] == REPO

        # Work goes to the worker.
        await _schedule(client, ws)
        assert (await _task_row(client, ws, task_key))["assignee"] == worker["assignee"]
        claimed = await client.post(f"/v1/tasks/{task_key}/claim", headers=worker["headers"],
                                    json={})
        assert claimed.status_code == 200, claimed.text
        handed = await client.post(f"/v1/tasks/{task_key}/handoff", headers=worker["headers"],
                                   json={"next_status": "REVIEWING", "brief": BRIEF,
                                         "lease_epoch": claimed.json()["task"]["lease_epoch"]})
        assert handed.status_code == 200, handed.text

        # Review goes to the reviewer, never back to the worker who built it.
        await _schedule(client, ws)
        review = await _open(client, ws, task_key)
        assert review is not None and review["step"] == "review", review
        assert review["host_id"] == reviewer["host_id"]
        assert (review["diagram_key"], review["chunk_key"]) == (key, chunk_key)
        note = (await _placed(client, reviewer))[-1]["note"]
        assert note == (f"placement {review['placement_id']} step review "
                        f"diagram {key} chunk {chunk_key}")
        gated = await client.post(f"/v1/diagrams/{key}/chunks/{chunk_key}/gate",
                                  headers=reviewer["headers"],
                                  json={"result": "pass", "checks": ["pytest: 12 passed"]})
        assert gated.status_code == 200, gated.text
        reported = await client.post(
            f"/v1/runner/placements/{review['placement_id']}/report",
            headers=reviewer["headers"], json={"outcome": "finished"})
        assert reported.status_code == 200, reported.text

        # Merge goes to a computer that did not build it.
        await _schedule(client, ws)
        merge = await _open(client, ws, task_key)
        assert merge is not None and merge["step"] == "merge", merge
        assert merge["host_id"] == reviewer["host_id"]
        merged = await client.post(f"/v1/diagrams/{key}/chunks/{chunk_key}/merge",
                                   headers=reviewer["headers"], json={"artifact": "abc123"})
        assert merged.status_code == 200, merged.text

    await _schedule(client, ws)
    view = (await client.get(f"/v1/diagrams/{key}", headers=ws["owner"])).json()
    assert {chunk["chunk_key"]: chunk["status"] for chunk in view["chunks"]} == {
        "api": "merged", "ui": "merged"}
    for chunk in view["chunks"]:
        steps = [(item["step"], item["status"]) for item in await _placements(
            client, ws, chunk["task_key"])]
        assert sorted(steps) == [("merge", "delivered"), ("review", "delivered"),
                                 ("work", "delivered")], steps


async def test_a_plain_task_in_review_goes_to_a_reviewer_and_moves_when_it_stalls(client):
    ws = await _workspace(client, "full")
    worker = await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}")
    first = await _host(client, ws, f"ra-{uuid.uuid4().hex[:4]}", role="AUDITOR")
    second = await _host(client, ws, f"rb-{uuid.uuid4().hex[:4]}", role="AUDITOR", slots=1)
    key = await _task(client, ws)
    await _schedule(client, ws)
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=worker["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    handed = await client.post(f"/v1/tasks/{key}/handoff", headers=worker["headers"], json={
        "next_status": "REVIEWING", "brief": BRIEF,
        "lease_epoch": claimed.json()["task"]["lease_epoch"]})
    assert handed.status_code == 200, handed.text

    await _schedule(client, ws)
    review = await _open(client, ws, key)
    assert review["step"] == "review" and review["host_id"] == first["host_id"]
    assert review["chunk_key"] is None

    # The worker cannot report on someone else's placement.
    foreign = await client.post(f"/v1/runner/placements/{review['placement_id']}/report",
                                headers=worker["headers"], json={"outcome": "finished"})
    assert foreign.status_code == 403, foreign.text

    # Nothing happens for too long: the review moves to the other reviewer.
    await _sql("UPDATE runner_placements SET created_at = NOW() - interval '2 hours' "
               "WHERE placement_id = $1", review["placement_id"])
    await _schedule(client, ws)
    history = {item["placement_id"]: item for item in await _placements(client, ws, key)}
    assert history[review["placement_id"]]["status"] == "failed"
    assert history[review["placement_id"]]["reason"] == "timed_out"
    moved = await _open(client, ws, key)
    assert moved["step"] == "review" and moved["host_id"] == second["host_id"]

    # A finished report with no verdict counts as a failure after the grace time.
    await client.post(f"/v1/runner/placements/{moved['placement_id']}/report",
                      headers=second["headers"], json={"outcome": "finished"})
    await _sql("UPDATE runner_placements SET reported_at = NOW() - interval '5 minutes' "
               "WHERE placement_id = $1", moved["placement_id"])
    await _schedule(client, ws)
    history = {item["placement_id"]: item for item in await _placements(client, ws, key)}
    assert history[moved["placement_id"]]["reason"] == "no_progress"


async def test_no_reviewer_is_said_in_plain_words(client):
    ws = await _workspace(client, "full")
    worker = await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}", capabilities=["review"])
    key = await _task(client, ws)
    await _schedule(client, ws)
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=worker["headers"], json={})
    await client.post(f"/v1/tasks/{key}/handoff", headers=worker["headers"], json={
        "next_status": "REVIEWING", "brief": BRIEF,
        "lease_epoch": claimed.json()["task"]["lease_epoch"]})
    await _schedule(client, ws)
    needs = (await client.get("/v1/runner/placements", headers=ws["owner"],
                              params={"open_only": "true"})).json()["needs_you"]
    needs = [item for item in needs if item["step"] != "supervise"]
    assert [(item["task_key"], item["step"], item["reason"]) for item in needs] == [
        (key, "review", "no_reviewer_host")]
    assert needs[0]["note"] == "No reviewer computer is online that did not build it."


async def test_the_supervisor_walks_the_floor_only_when_something_waits(client):
    ws = await _workspace(client, "full")
    # Nothing waits yet: no round, even without a supervisor.
    await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}")
    result = await _schedule(client, ws)
    assert result["rounds"] == 0
    assert (await client.get("/v1/runner/placements", headers=ws["owner"],
                             params={"step": "supervise"})).json()["items"] == []

    # A task no computer can reach waits: with no supervisor, the owner hears why.
    stuck = await _task(client, ws, repo="acme/unreachable")
    await _schedule(client, ws)
    rounds = (await client.get("/v1/runner/placements", headers=ws["owner"],
                               params={"step": "supervise"})).json()["items"]
    assert [(item["status"], item["reason"]) for item in rounds] == [
        ("unplaced", "no_supervisor_host")]
    needs = (await client.get("/v1/runner/placements", headers=ws["owner"],
                              params={"open_only": "true"})).json()["needs_you"]
    assert "no_supervisor_host" in {item["reason"] for item in needs}

    # A supervisor comes online: it gets one round with the agenda.
    boss = await _host(client, ws, f"sv-{uuid.uuid4().hex[:4]}", role="AUDITOR",
                       capabilities=["supervisor"])
    result = await _schedule(client, ws)
    assert result["rounds"] == 1
    round_ = next(item for item in (await client.get(
        "/v1/runner/placements", headers=ws["owner"], params={"step": "supervise"})).json()["items"]
        if item["status"] == "offered")
    assert round_["host_id"] == boss["host_id"]
    detail = await client.get(f"/v1/runner/placements/{round_['placement_id']}",
                              headers=boss["headers"])
    assert detail.status_code == 200, detail.text
    items = detail.json()["detail"]["agenda"]
    assert {"kind": "no_host", "task_key": stuck, "step": "work",
            "reason": "no_host_for_repo",
            "note": "No online runner can reach acme/unreachable."} in items
    note = (await _placed(client, boss))[-1]["note"]
    assert note == f"placement {round_['placement_id']} step supervise"

    # One round at a time; after it finishes, none again within the interval.
    assert (await _schedule(client, ws))["rounds"] == 0
    done = await client.post(f"/v1/runner/placements/{round_['placement_id']}/report",
                             headers=boss["headers"], json={"outcome": "finished",
                                                            "note": "asked the owner"})
    assert done.status_code == 200, done.text
    assert (await _schedule(client, ws))["rounds"] == 0
    latest = (await client.get("/v1/runner/placements", headers=ws["owner"],
                               params={"step": "supervise"})).json()["items"][0]
    assert latest["status"] == "delivered"
