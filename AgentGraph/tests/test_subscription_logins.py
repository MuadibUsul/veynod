"""Signing a server's agents in to the owner's own subscription from the console (v2 P1-D).

A Python client stands in for the computer's messenger (cli/bin/login.mjs is
tested in the CLI): what is under test is the server's part, the relay. It
must pass on only provider sign-in links and one-time codes, only to the
person who started the sign-in, and only between that person and that
computer.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tests.test_nerve_dispatch import _connect
from tests.test_scheduler import ADMIN, REPO, _workspace

pytestmark = pytest.mark.integration

CLAUDE_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a"
              "&response_type=code&scope=user%3Ainference&code_challenge=abc&state=xyz")


async def _computer(client, ws: dict, name: str, report: dict | None) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    named = await client.post("/v1/agents/register", headers=headers, json={"agent_id": name})
    assert named.status_code in (200, 201), named.text
    host = {"os": "linux", "arch": "x64", "max_concurrency": 1, "repos": [REPO],
            "runtimes": [{"id": "claude", "available": True}, {"id": "codex", "available": True}]}
    if report is not None:
        host["capabilities_report"] = report
    hello = await client.post("/v1/runner/hello", headers=headers, json={"host": host})
    assert hello.status_code == 200, hello.text
    return {"headers": headers, "agent_id": name, "host_id": hello.json()["host_id"],
            "key": minted.json()["api_key"]}


async def _frame(socket, frame_type: str) -> dict:
    """The next frame of this type (pongs and notices in between are skipped)."""
    while True:
        frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
        if frame["type"] == frame_type:
            return frame


async def _say(socket, payload: dict) -> None:
    await socket.send(json.dumps({"type": "login.update", "frame_id": uuid.uuid4().hex,
                                  "payload": payload}))
    await asyncio.sleep(0.1)


CAN = {"mcp_pinning": True, "runtime": "claude", "logins": ["claude", "codex"], "signed_in": []}


async def test_claude_code_signs_in_through_the_console_and_the_login_stays_on_the_computer(
        api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    computer = await _computer(client, ws, f"srv-{uuid.uuid4().hex[:4]}", CAN)
    socket = await _connect(api_base_url, computer)
    base = f"/v1/runner/hosts/{computer['agent_id']}/logins"
    try:
        started = await client.post(base, headers=ws["owner"], json={"runtime": "claude"})
        assert started.status_code == 201, started.text
        login = started.json()
        assert login["state"] == "starting" and login["subscription"] == "Claude Pro or Max"
        asked = await _frame(socket, "login.start")
        assert asked["payload"] == {"login_id": login["login_id"], "runtime": "claude"}

        await _say(socket, {"login_id": login["login_id"], "state": "enter_code",
                            "url": CLAUDE_URL})
        shown = (await client.get(f"{base}/{login['login_id']}", headers=ws["owner"])).json()
        assert (shown["state"], shown["url"]) == ("enter_code", CLAUDE_URL)

        sent = await client.post(f"{base}/{login['login_id']}/code", headers=ws["owner"],
                                 json={"code": "  dGhpc2lzYWNvZGU#xyz  "})
        assert sent.status_code == 200, sent.text
        assert sent.json()["state"] == "verifying"
        relayed = await _frame(socket, "login.code")
        assert relayed["payload"] == {"login_id": login["login_id"], "code": "dGhpc2lzYWNvZGU#xyz"}

        await _say(socket, {"login_id": login["login_id"], "state": "verifying"})
        await _say(socket, {"login_id": login["login_id"], "state": "connected", "plan": "max",
                            "token": "sk-ant-oat01-never-kept"})
        done = (await client.get(f"{base}/{login['login_id']}", headers=ws["owner"])).json()
        assert (done["state"], done["plan"], done["url"]) == ("connected", "max", None)
        assert "sk-ant" not in json.dumps(done), "nothing but the listed fields is kept"
        # A finished sign-in does not move again.
        await _say(socket, {"login_id": login["login_id"], "state": "failed", "message": "late"})
        again = (await client.get(f"{base}/{login['login_id']}", headers=ws["owner"])).json()
        assert again["state"] == "connected"

        host = (await client.get(f"/v1/runner/hosts/{computer['agent_id']}",
                                 headers=ws["owner"])).json()
        assert host["capabilities_report"]["logins"] == ["claude", "codex"]
    finally:
        await socket.close()


async def test_codex_shows_a_device_code_and_a_new_sign_in_replaces_the_old(
        api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    computer = await _computer(client, ws, f"srv-{uuid.uuid4().hex[:4]}", CAN)
    socket = await _connect(api_base_url, computer)
    base = f"/v1/runner/hosts/{computer['agent_id']}/logins"
    try:
        first = (await client.post(base, headers=ws["owner"], json={"runtime": "codex"})).json()
        await _frame(socket, "login.start")
        await _say(socket, {"login_id": first["login_id"], "state": "open_url",
                            "url": "https://auth.openai.com/codex/device",
                            "user_code": "318M-3SF5H", "expires_in_s": 900})
        shown = (await client.get(f"{base}/{first['login_id']}", headers=ws["owner"])).json()
        assert (shown["state"], shown["user_code"]) == ("open_url", "318M-3SF5H")
        assert shown["expires_at"]
        # Claude's code step is not Codex's.
        wrong = await client.post(f"{base}/{first['login_id']}/code", headers=ws["owner"],
                                  json={"code": "abcdefgh"})
        assert wrong.status_code == 409

        second = (await client.post(base, headers=ws["owner"], json={"runtime": "codex"})).json()
        assert (await _frame(socket, "login.cancel"))["payload"] == {"login_id": first["login_id"]}
        assert (await _frame(socket, "login.start"))["payload"]["login_id"] == second["login_id"]
        old = (await client.get(f"{base}/{first['login_id']}", headers=ws["owner"])).json()
        assert old["state"] == "cancelled"

        stopped = await client.post(f"{base}/{second['login_id']}/cancel", headers=ws["owner"])
        assert stopped.json()["state"] == "cancelled"
        assert (await _frame(socket, "login.cancel"))["payload"] == {"login_id": second["login_id"]}
    finally:
        await socket.close()


async def test_only_provider_links_from_that_computer_reach_the_person_who_asked(
        api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    computer = await _computer(client, ws, f"srv-{uuid.uuid4().hex[:4]}", CAN)
    other = await _computer(client, ws, f"srv-{uuid.uuid4().hex[:4]}", CAN)
    socket = await _connect(api_base_url, computer)
    intruder = await _connect(api_base_url, other)
    base = f"/v1/runner/hosts/{computer['agent_id']}/logins"
    try:
        # A computer's own key cannot start one: only a person, the owner or an admin.
        refused = await client.post(base, headers=computer["headers"], json={"runtime": "claude"})
        assert refused.status_code == 403
        unknown = await client.post(base, headers=ws["owner"], json={"runtime": "gemini"})
        assert unknown.status_code == 422

        login = (await client.post(base, headers=ws["owner"], json={"runtime": "claude"})).json()
        await _frame(socket, "login.start")
        # Another computer of the same workspace cannot move this sign-in.
        await _say(intruder, {"login_id": login["login_id"], "state": "enter_code",
                              "url": CLAUDE_URL})
        still = (await client.get(f"{base}/{login['login_id']}", headers=ws["owner"])).json()
        assert (still["state"], still["url"]) == ("starting", None)
        # A link anywhere but the provider's own sign-in is never shown.
        await _say(socket, {"login_id": login["login_id"], "state": "enter_code",
                            "url": "https://claude.com.evil.example/oauth/authorize?x=1"})
        phished = (await client.get(f"{base}/{login['login_id']}", headers=ws["owner"])).json()
        assert phished["state"] == "failed" and phished["url"] is None
        assert "not the provider's own" in phished["message"]

        # Another workspace cannot see it at all.
        stranger = await _workspace(client, "full")
        hidden = await client.get(f"{base}/{login['login_id']}", headers=stranger["owner"])
        assert hidden.status_code == 404
    finally:
        await socket.close()
        await intruder.close()


async def test_an_offline_or_older_computer_says_what_to_do(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    old = await _computer(client, ws, f"old-{uuid.uuid4().hex[:4]}",
                          {"mcp_pinning": True, "runtime": "claude"})
    offline = await client.post(f"/v1/runner/hosts/{old['agent_id']}/logins", headers=ws["owner"],
                                json={"runtime": "claude"})
    assert offline.status_code == 409
    assert offline.json()["error"]["code"] == "computer_offline"
    socket = await _connect(api_base_url, old)
    try:
        older = await client.post(f"/v1/runner/hosts/{old['agent_id']}/logins",
                                  headers=ws["owner"], json={"runtime": "claude"})
        assert older.status_code == 409
        assert older.json()["error"]["code"] == "login_unavailable"
        assert "0.8.0" in older.json()["error"]["message"]
    finally:
        await socket.close()
    windows = await _computer(client, ws, f"win-{uuid.uuid4().hex[:4]}",
                              {**CAN, "logins": ["codex"]})
    socket = await _connect(api_base_url, windows)
    try:
        no_terminal = await client.post(f"/v1/runner/hosts/{windows['agent_id']}/logins",
                                        headers=ws["owner"], json={"runtime": "claude"})
        assert no_terminal.status_code == 409
        assert "no terminal" in no_terminal.json()["error"]["message"]
    finally:
        await socket.close()
