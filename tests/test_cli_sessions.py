"""One approval mints every identity `veynod up` asks for (v3.64, WAKE_AND_DISPATCH §14.5)."""

from __future__ import annotations

import pytest

from tests.test_growth import _keypair, _open, _signup

pytestmark = pytest.mark.integration


async def _start(client, public: str, identities: list[dict]) -> dict:
    started = await client.post("/v1/auth/cli/sessions", json={
        "public_key": public, "client_label": "box", "clients": ["claude-code"],
        "identities": identities})
    assert started.status_code == 201, started.text
    return started.json()


async def test_one_approval_mints_identities(client):
    owner, _ = await _signup(client)
    private, public = _keypair()
    wanted = [{"name": "box", "role_scope": "EXECUTOR", "why": "this computer's agent"},
              {"name": "box-review", "role_scope": "AUDITOR", "why": "reviews what box built"}]
    flow = await _start(client, public, wanted)

    view = (await client.get(f"/v1/auth/cli/sessions/{flow['user_code']}",
                             headers=owner)).json()
    assert [(item["name"], item["role_scope"]) for item in view["identities"]] == [
        ("box", "EXECUTOR"), ("box-review", "AUDITOR")]

    unknown = await client.post(f"/v1/auth/cli/sessions/{flow['user_code']}/approve",
                                headers=owner, json={"agent_signature": "box", "also": [
                                    {"index": 2, "agent_signature": "box-other"}]})
    assert unknown.status_code == 422, unknown.text
    approved = await client.post(f"/v1/auth/cli/sessions/{flow['user_code']}/approve",
                                 headers=owner, json={"agent_signature": "box", "also": [
                                     {"index": 1, "agent_signature": "box-review"}]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["also"] == [{"agent_signature": "box-review", "role_scope": "AUDITOR"}]

    collected = (await client.post("/v1/auth/cli/poll",
                                   json={"device_code": flow["device_code"]})).json()
    assert collected["status"] == "approved" and collected["agent_signature"] == "box"
    [extra] = collected["identities"]
    assert (extra["name"], extra["agent_signature"], extra["role_scope"]) == (
        "box-review", "box-review", "AUDITOR")
    keys = {"box": _open(collected["sealed_key"], private),
            "box-review": _open(extra["sealed_key"], private)}
    assert keys["box"] != keys["box-review"]
    # Collected once: the sealed copies are gone.
    again = await client.post("/v1/auth/cli/poll", json={"device_code": flow["device_code"]})
    assert again.status_code == 410

    # Each key is its own agent with the role the terminal asked for.
    for name, role in (("box", "EXECUTOR"), ("box-review", "AUDITOR")):
        me = await client.get("/v1/whoami", headers={"Authorization": f"Bearer {keys[name]}"})
        assert me.status_code == 200, me.text
        assert (me.json()["agent_signature"], me.json()["role"]) == (name, role)

    # An extra the owner leaves unticked is not minted.
    private2, public2 = _keypair()
    flow2 = await _start(client, public2, [
        {"name": "lap", "role_scope": "EXECUTOR"}, {"name": "lap-review", "role_scope": "AUDITOR"}])
    only = await client.post(f"/v1/auth/cli/sessions/{flow2['user_code']}/approve",
                             headers=owner, json={"agent_signature": "lap"})
    assert only.json()["also"] == []
    collected2 = (await client.post("/v1/auth/cli/poll",
                                    json={"device_code": flow2["device_code"]})).json()
    assert collected2["identities"] == []
    assert _open(collected2["sealed_key"], private2).startswith("agt_")


async def test_a_terminal_cannot_ask_for_a_super_admin_or_duplicate_names(client):
    _, public = _keypair()
    for identities in ([{"name": "box", "role_scope": "SUPER_ADMIN"}],
                       [{"name": "box"}, {"name": "BOX", "role_scope": "AUDITOR"}]):
        refused = await client.post("/v1/auth/cli/sessions", json={
            "public_key": public, "identities": identities})
        assert refused.status_code == 422, refused.text
