"""Live blackboard (agentgraph/spec-live-blackboard) and presence states (blueprint L2).

The test server's reaper runs every 5 seconds; presence that goes stale is
simulated by moving ``seen_at`` back past the 90-second window.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import httpx
import pytest

pytestmark = pytest.mark.integration

BRIEF = {"summary": "done", "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_db())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _agent(client, admin_headers, workspace, name=None):
    signature = name or f"bb_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": "EXECUTOR",
              "allowed_namespaces": ["*"]})
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['api_key']}"}
    registered = await client.post("/v1/agents/register", headers=headers,
                                   json={"capabilities": ["testing"]})
    assert registered.status_code == 201, registered.text
    return headers, signature, registered.json()["agent_id"]


def _board_url(workspace, suffix=""):
    return f"/v1/admin/workspaces/{workspace['workspace']['id']}/blackboard{suffix}"


async def _board(client, admin_headers, workspace, **params):
    response = await client.get(_board_url(workspace), headers=admin_headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _avatar(board, agent_id):
    return next(item for item in board["agents"] if item["agent_id"] == agent_id)


async def _stream_since(base_url, workspace, admin_headers, cursor, want, timeout=15):
    """Replay the console stream from a cursor and collect `want` events."""
    events: list[dict] = []
    headers = {**admin_headers, "Last-Event-ID": str(cursor)}
    url = (f"/v1/admin/workspaces/{workspace['workspace']['id']}/stream"
           "?tables=collab_tasks,timeline_streams")

    async def read() -> None:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as http:
            async with http.stream("GET", url, headers=headers) as response:
                assert response.status_code == 200
                event, data = "message", ""
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                    elif line == "":
                        if event == "workspace":
                            events.append(json.loads(data))
                            if want(events):
                                return
                        event, data = "message", ""

    try:
        await asyncio.wait_for(read(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    return events


async def test_snapshot_columns_resolved_limit_and_archive(client, admin_headers, workspace):
    owner = workspace["headers"]
    agent, signature, agent_id = await _agent(client, admin_headers, workspace)
    keys = []
    for index in range(14):
        made = await client.post("/v1/tasks", headers=owner, json={
            "title": f"bb task {index}", "namespace": "default"})
        assert made.status_code == 201, made.text
        keys.append(made.json()["task_key"])

    # One in progress, held by the agent.
    claimed = await client.post(f"/v1/tasks/{keys[0]}/claim", headers=agent, json={})
    assert claimed.status_code == 200, claimed.text
    # Twelve resolved (DONE, oldest first), one cancelled; the newest 10 DONE
    # stay on the board, the rest go to the archive.
    ws_id = workspace["workspace"]["id"]
    for offset, key in enumerate(keys[1:13]):
        await _sql("UPDATE collab_tasks SET status = 'DONE', updated_at = NOW() - "
                   "make_interval(mins => $3), claimed_by = NULL, lease_owner = NULL, "
                   "handed_off_by = $4 WHERE workspace_id = $1::uuid AND task_key = $2",
                   ws_id, key, 12 - offset, signature)
    await _sql("UPDATE collab_tasks SET status = 'CANCELLED' WHERE workspace_id = $1::uuid "
               "AND task_key = $2", ws_id, keys[13])

    board = await _board(client, admin_headers, workspace)
    columns = board["columns"]
    assert [card["task_key"] for card in columns["in_progress"]] == [keys[0]]
    assert columns["in_progress"][0]["holder_agent"] == agent_id
    resolved = [card["task_key"] for card in columns["resolved"]]
    assert resolved == list(reversed(keys[3:13]))  # newest 10 DONE, newest first
    assert board["archived"] == 3  # two older DONE + one CANCELLED
    assert board["cursor"] > 0
    # A-03: the working agent is paired with its task.
    avatar = _avatar(board, agent_id)
    assert avatar["working_on"] == [keys[0]]

    archive_url = _board_url(workspace, "/archive")
    archived = (await client.get(archive_url, headers=admin_headers)).json()["items"]
    assert {card["task_key"] for card in archived} == {keys[1], keys[2], keys[13]}
    by_agent = (await client.get(archive_url, headers=admin_headers,
                                 params={"agent": f"agent:{agent_id}"})).json()["items"]
    assert {card["task_key"] for card in by_agent} == {keys[1], keys[2]}
    by_word = (await client.get(archive_url, headers=admin_headers,
                                params={"q": "bb task 13"})).json()["items"]
    assert [card["task_key"] for card in by_word] == [keys[13]]
    future = (await client.get(archive_url, headers=admin_headers,
                               params={"since": "2999-01-01T00:00:00Z"})).json()["items"]
    assert future == []
    paged = (await client.get(archive_url, headers=admin_headers,
                              params={"limit": 2})).json()
    assert len(paged["items"]) == 2 and paged["next_cursor"]
    rest = (await client.get(archive_url, headers=admin_headers,
                             params={"limit": 2, "cursor": paged["next_cursor"]})).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None

    # A signed-in person reads the same board.
    email = f"bb-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{ws_id}/human-users", headers=admin_headers,
                      json={"email": email, "display_name": "Board Owner", "role": "OWNER",
                            "password": "correct-horse-battery"})
    login = await client.post("/v1/auth/login",
                              json={"email": email, "password": "correct-horse-battery"})
    person = {"Authorization": f"Bearer {login.json()['token']}"}
    human = await client.get("/v1/human/blackboard", headers=person)
    assert human.status_code == 200, human.text
    assert human.json()["counts"] == board["counts"]

    # Cards refresh one by one, and every card traces back to its events.
    subset = (await client.get(_board_url(workspace), headers=admin_headers,
                               params={"task_keys": f"{keys[0]},nope"})).json()
    assert [card["task_key"] for card in subset["cards"]] == [keys[0]]
    assert subset["missing"] == ["nope"]
    events = (await client.get(_board_url(workspace, f"/tasks/{keys[0]}/events"),
                               headers=admin_headers)).json()["events"]
    assert {"task.created", "task.claimed"} <= {event["type"] for event in events}


async def test_snapshot_then_stream_is_consistent(client, admin_headers, workspace,
                                                  api_base_url):
    """A-07: whatever changes after the snapshot arrives on the stream from its cursor."""
    owner = workspace["headers"]
    agent, _, agent_id = await _agent(client, admin_headers, workspace)
    board = await _board(client, admin_headers, workspace)
    made = await client.post("/v1/tasks", headers=owner, json={
        "title": "after snapshot", "namespace": "default"})
    key = made.json()["task_key"]
    await client.post(f"/v1/tasks/{key}/claim", headers=agent, json={})

    def has_claim(events):
        return any(event.get("task_key") == key and event.get("type") == "task.claimed"
                   for event in events)

    events = await _stream_since(api_base_url, workspace, admin_headers, board["cursor"],
                                 has_claim)
    assert has_claim(events)
    created = next(event for event in events if event.get("task_key") == key)
    assert created["type"] in ("task.created", "task.assigned")
    # The card the client refreshes after those events matches the server.
    card = (await client.get(_board_url(workspace), headers=admin_headers,
                             params={"task_keys": key})).json()["cards"][0]
    assert card["column"] == "in_progress" and card["holder_agent"] == agent_id


async def test_presence_states_and_announcements(client, admin_headers, workspace,
                                                 api_base_url):
    owner = workspace["headers"]
    agent, _, agent_id = await _agent(client, admin_headers, workspace)

    # No stream, no heartbeat, no wake URL: unwakeable, and the board says so.
    entry = (await client.get(f"/v1/agents/{agent_id}", headers=owner)).json()
    assert entry["presence"]["state"] == "unwakeable"
    assert entry["presence"]["wakeable"] is False
    board = await _board(client, admin_headers, workspace)
    assert _avatar(board, agent_id)["activity"] == "offline"
    cursor = board["cursor"]

    # A loop heartbeat brings it online at once, and that change is announced.
    beat = await client.post("/v1/presence/heartbeat", headers=agent)
    assert beat.status_code == 200, beat.text
    assert beat.json()["offline_after_s"] == 90
    listed = (await client.get("/v1/presence", headers=owner)).json()
    mine = next(item for item in listed["items"] if item["agent_id"] == agent_id)
    assert mine["state"] == "online" and mine["transport"] == "heartbeat"
    assert _avatar(await _board(client, admin_headers, workspace), agent_id)["activity"] == "idle"

    def announced(state):
        def check(events):
            return any(event.get("event_type") == "AGENT_PRESENCE_CHANGED"
                       and (event.get("details") or {}).get("agent_id") == agent_id
                       and (event.get("details") or {}).get("state") == state
                       for event in events)
        return check

    events = await _stream_since(api_base_url, workspace, admin_headers, cursor,
                                 announced("online"))
    assert announced("online")(events)

    # A wake URL makes it wakeable: when the loop goes quiet it is offline,
    # not unwakeable.
    wake = await client.put("/v1/agents/me/wake", headers=agent,
                            json={"url": "https://example.com/wake"})
    assert wake.status_code == 200, wake.text
    await _sql("UPDATE agent_presence SET seen_at = NOW() - INTERVAL '120 seconds' "
               "WHERE workspace_id = $1::uuid", workspace["workspace"]["id"])
    state = None
    for _ in range(40):  # the reaper (every 5 s here) announces the silence
        entry = (await client.get(f"/v1/agents/{agent_id}", headers=owner)).json()
        state = entry["presence"]["state"]
        stored = (await _sql("SELECT presence_state FROM agent_directory WHERE agent_id = $1 "
                             "AND workspace_id = $2::uuid", agent_id,
                             workspace["workspace"]["id"]))[0]["presence_state"]
        if state == "offline" and stored == "offline":
            break
        await asyncio.sleep(0.5)
    assert state == "offline"
    events = await _stream_since(api_base_url, workspace, admin_headers, cursor,
                                 announced("offline"))
    assert announced("offline")(events)

    # A failing wake channel degrades it.
    await _sql("UPDATE webhook_rules SET consecutive_failures = 3 WHERE workspace_id = $1::uuid "
               "AND name = $2", workspace["workspace"]["id"], f"wake:{agent_id}")
    entry = (await client.get(f"/v1/agents/{agent_id}", headers=owner)).json()
    assert entry["presence"]["state"] == "degraded"
    assert entry["presence"]["wake"]["failing"] is True


async def test_stream_keeps_events_committed_out_of_id_order(client, admin_headers, workspace,
                                                             api_base_url):
    """Concurrent writers commit in their own order; none of their events is dropped."""
    owner = workspace["headers"]
    board = await _board(client, admin_headers, workspace)
    titles = [f"burst {index}" for index in range(12)]
    created: list[str] = []

    async def burst():
        await asyncio.sleep(1.0)  # let the stream connect first
        responses = await asyncio.gather(*[
            client.post("/v1/tasks", headers=owner, json={"title": title, "namespace": "default"})
            for title in titles])
        created.extend(response.json()["task_key"] for response in responses)

    def all_seen(events):
        keys = {event.get("task_key") for event in events}
        return len(created) == len(titles) and set(created) <= keys

    # Live delivery (no Last-Event-ID): the stream is open while the burst commits.
    writer = asyncio.create_task(burst())
    events: list[dict] = []
    headers = dict(admin_headers)
    url = (f"/v1/admin/workspaces/{workspace['workspace']['id']}/stream"
           "?tables=collab_tasks")

    async def read() -> None:
        async with httpx.AsyncClient(base_url=api_base_url, timeout=20) as http:
            async with http.stream("GET", url, headers=headers) as response:
                event, data = "message", ""
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                    elif line == "":
                        if event == "workspace":
                            events.append(json.loads(data))
                            if all_seen(events):
                                return
                        event, data = "message", ""

    try:
        await asyncio.wait_for(read(), timeout=20)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    await writer
    missing = set(created) - {event.get("task_key") for event in events}
    assert not missing, f"dropped live: {sorted(missing)}"
    # And a replay from the snapshot cursor has them all too.
    replayed = await _stream_since(api_base_url, workspace, admin_headers, board["cursor"],
                                   all_seen)
    assert set(created) <= {event.get("task_key") for event in replayed}


async def test_delivery_rates_and_presence_history(client, admin_headers, workspace):
    owner = workspace["headers"]
    agent, _, agent_id = await _agent(client, admin_headers, workspace)
    await client.post("/v1/presence/heartbeat", headers=agent)
    made = await client.post("/v1/tasks", headers=owner, json={
        "title": "addressed", "assignee": f"agent:{agent_id}", "namespace": "default"})
    assert made.status_code == 201, made.text

    report = (await client.get("/v1/presence/delivery", headers=owner)).json()
    mine = next(item for item in report["items"] if item["agent_id"] == agent_id)
    assert mine["addressed"] >= 1 and mine["read"] == 0 and mine["unread"] >= 1
    assert mine["state"] == "online"
    inbox = (await client.get("/v1/inbox", headers=agent)).json()
    seq = max(item["event_seq"] for item in inbox["items"])
    await client.post("/v1/inbox/ack", headers=agent, json={"up_to_event_seq": seq})
    mine = next(item for item in (await client.get("/v1/presence/delivery", headers=owner))
                .json()["items"] if item["agent_id"] == agent_id)
    assert mine["read"] == mine["addressed"] and mine["read_rate"] == 1.0
    bad = await client.get("/v1/presence/delivery", headers=owner, params={"window": "2y"})
    assert bad.status_code == 422

    history = (await client.get("/v1/presence/history", headers=owner,
                                params={"agent": agent_id})).json()["items"]
    assert history and history[0]["agent_id"] == agent_id and history[0]["state"] == "online"
    console = await client.get(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/presence/delivery",
        headers=admin_headers)
    assert console.status_code == 200, console.text


async def test_stuck_task_alert_and_wake_coverage(client, admin_headers, workspace):
    """Blueprint L6: work left by a holder that died is raised within the hour."""
    owner = workspace["headers"]
    ws_id = workspace["workspace"]["id"]
    agent, _, agent_id = await _agent(client, admin_headers, workspace)
    made = await client.post("/v1/tasks", headers=owner, json={"title": "left behind",
                                                               "namespace": "default"})
    key = made.json()["task_key"]
    # The holder's lease expired 40 minutes ago and nobody picked the task up.
    await _sql("UPDATE collab_tasks SET orphaned_from = 'gone-agent', orphan_count = 1, "
               "updated_at = NOW() - INTERVAL '40 minutes' WHERE workspace_id = $1::uuid "
               "AND task_key = $2", ws_id, key)
    found = None
    for _ in range(90):
        alerts = (await client.get(f"/v1/admin/workspaces/{ws_id}/alerts",
                                   headers=admin_headers)).json()["items"]
        found = next((item for item in alerts if item["rule"] == "stuck_task"), None)
        if found:
            break
        await asyncio.sleep(1)
    assert found is not None and found["severity"] == "high"
    assert found["subject"]["ref"] == key

    coverage = (await client.get("/v1/presence", headers=owner)).json()["wake_coverage"]
    assert coverage == {"agents": 1, "with_wake_channel": 0, "rate": 0.0}
    await client.put("/v1/agents/me/wake", headers=agent, json={"url": "https://example.com/w"})
    coverage = (await client.get("/v1/presence", headers=owner)).json()["wake_coverage"]
    assert coverage["rate"] == 1.0, agent_id


async def test_an_agent_working_through_calls_is_online(client, admin_headers, workspace):
    """No stream and no heartbeat, but recent REST/MCP calls: online over api."""
    owner = workspace["headers"]
    agent, _, agent_id = await _agent(client, admin_headers, workspace)
    await _sql("UPDATE agent_directory SET last_seen_at = NULL WHERE agent_id = $1 "
               "AND workspace_id = $2::uuid", agent_id, workspace["workspace"]["id"])
    working = await client.get("/v1/tasks", headers=agent)
    assert working.status_code == 200, working.text

    entry = (await client.get(f"/v1/agents/{agent_id}", headers=owner)).json()
    assert entry["presence"]["state"] == "online", entry["presence"]
    assert entry["presence"]["transport"] == "api"
    listed = (await client.get("/v1/presence", headers=owner)).json()
    mine = next(item for item in listed["items"] if item["agent_id"] == agent_id)
    assert mine["state"] == "online"

    # Ten quiet minutes and it is no longer online.
    await _sql("UPDATE agent_presence SET seen_at = NOW() - INTERVAL '11 minutes' "
               "WHERE workspace_id = $1::uuid", workspace["workspace"]["id"])
    entry = (await client.get(f"/v1/agents/{agent_id}", headers=owner)).json()
    assert entry["presence"]["state"] == "unwakeable"


async def test_a_card_goes_to_the_agent_whose_current_name_matches(client, admin_headers,
                                                                   workspace):
    """An old name another entry once carried never steals a holder's card."""
    worker, signature, worker_id = await _agent(client, admin_headers, workspace)
    _, _, other_id = await _agent(client, admin_headers, workspace,
                                  name="aa_" + uuid.uuid4().hex[:6])
    await _sql("UPDATE agent_directory SET legacy_names = array_append(legacy_names, $1) "
               "WHERE agent_id = $2 AND workspace_id = $3::uuid", signature, other_id,
               workspace["workspace"]["id"])
    key = f"bb-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/tasks", headers=workspace["headers"], json={
        "task_key": key, "title": "held by the worker", "assigned_role": "EXECUTOR"})
    assert made.status_code == 201, made.text
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=worker, json={})
    assert claimed.status_code == 200, claimed.text

    board = await _board(client, admin_headers, workspace)
    assert key in _avatar(board, worker_id)["working_on"]
    assert key not in _avatar(board, other_id)["working_on"]


async def test_a_card_waiting_on_a_person_names_the_agent_that_asked(client, admin_headers,
                                                                     workspace):
    """An escalated card has no holder; the board says which agent asked."""
    worker, _, worker_id = await _agent(client, admin_headers, workspace)
    key = f"bb-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/tasks", headers=workspace["headers"], json={
        "task_key": key, "title": "needs a decision", "assigned_role": "EXECUTOR"})
    assert made.status_code == 201, made.text
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=worker, json={})
    assert claimed.status_code == 200, claimed.text
    escalated = await client.post(f"/v1/tasks/{key}/escalate", headers=worker,
                                  json={"reason": "which payment provider?"})
    assert escalated.status_code == 200, escalated.text

    board = await _board(client, admin_headers, workspace)
    cards = [card for column in board["columns"].values() for card in column]
    card = next(card for card in cards if card["task_key"] == key)
    assert card["asked_by_agent"] == worker_id

    other = f"bb-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/tasks", headers=workspace["headers"], json={
        "task_key": other, "title": "still open", "assigned_role": "EXECUTOR"})
    assert made.status_code == 201, made.text
    board = await _board(client, admin_headers, workspace)
    cards = [card for column in board["columns"].values() for card in column]
    assert next(card for card in cards if card["task_key"] == other)["asked_by_agent"] is None
