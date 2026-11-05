"""Federation HTTP API (round 2): identity, handles and connections end to end
through a federation-enabled server (app/api/federation_routes.py)."""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="module")
async def fed(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    env = {
        "FEDERATION_ENABLED": "true",
        "FEDERATION_KEK": base64.b64encode(os.urandom(32)).decode(),
    }
    async with run_server(env, ".server-federation.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client: httpx.AsyncClient, name: str) -> dict:
    created = await client.post(
        "/v1/admin/workspaces", headers=ADMIN, json={"name": f"{name}-{uuid.uuid4().hex[:6]}"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    ws: dict = {
        "id": body["workspace"]["id"],
        "key": {"Authorization": f"Bearer {body['api_key']}"},
    }
    for role in ("OWNER", "REVIEWER"):
        email = f"{role.lower()}-{uuid.uuid4().hex[:8]}@example.test"
        made = await client.post(
            f"/v1/admin/workspaces/{ws['id']}/human-users",
            headers=ADMIN,
            json={"email": email, "display_name": role, "role": role, "password": PASSWORD},
        )
        assert made.status_code in (200, 201), made.text
        login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        ws[role.lower()] = {"Authorization": f"Bearer {login.json()['token']}"}
    return ws


def _refused(response: httpx.Response, status: int, code: str, federation_code: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code, error
    assert error["details"]["federation_code"] == federation_code, error


def _no_local_ids(response: httpx.Response, *local: str) -> None:
    blob = json.dumps(response.json())
    assert not re.search(r"\b(prn|tok|ten|agk)_[A-Za-z0-9]", blob), blob
    for value in local:
        assert value not in blob


async def test_disabled_deployment_answers_federation_disabled(client):
    ws = await _workspace(client, "off")
    response = await client.get("/v1/federation/me", headers=ws["owner"])
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "federation_disabled"


async def test_connection_lifecycle_over_http(fed):
    a, b = await _workspace(fed, "api-a"), await _workspace(fed, "api-b")
    assert (await fed.get("/v1/federation/me", headers=a["owner"])).json() == {"identity": None}

    # Only an owner-level person enables federation for a workspace.
    _refused(
        await fed.post("/v1/federation/enable", headers=a["reviewer"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    _refused(
        await fed.post("/v1/federation/enable", headers=a["key"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    enabled_a = await fed.post("/v1/federation/enable", headers=a["owner"])
    assert enabled_a.status_code == 200, enabled_a.text
    a_fid = enabled_a.json()["identity"]["federation_id"]
    _no_local_ids(enabled_a, a["id"])
    b_fid = (await fed.post("/v1/federation/enable", headers=b["owner"])).json()["identity"][
        "federation_id"
    ]

    # A handle is published by the owner and resolves to the federation id.
    handle = f"lab-{uuid.uuid4().hex[:8]}"
    published = await fed.put(
        "/v1/federation/me/alias", headers=b["owner"], json={"handle": handle}
    )
    assert published.status_code == 200, published.text
    resolved = await fed.get(
        "/v1/federation/resolve", headers=a["owner"], params={"workspace": handle}
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "unique"
    assert resolved.json()["matches"][0]["federation_id"] == b_fid
    looked_up = await fed.get(f"/v1/federation/identities/{b_fid}", headers=a["reviewer"])
    assert looked_up.json()["identity"]["display_alias"] == handle
    _no_local_ids(looked_up, b["id"])

    # A's owner asks; B's reviewer cannot approve; B's owner approves fewer scopes.
    asked = await fed.post(
        "/v1/federation/connections",
        headers=a["owner"],
        json={
            "peer_workspace": b_fid,
            "outbound_scopes": ["chat", "delegate"],
            "inbound_scopes": ["chat"],
            "reason": "joint research",
        },
    )
    assert asked.status_code == 201, asked.text
    cid = asked.json()["connection"]["connection_id"]
    _no_local_ids(asked, a["id"], b["id"])
    pending = (await fed.get("/v1/federation/connections", headers=b["reviewer"])).json()["items"]
    assert [(c["connection_id"], c["state"], c["peer_workspace"]) for c in pending] == [
        (cid, "REQUESTED", a_fid)
    ]
    _refused(
        await fed.post(f"/v1/federation/connections/{cid}/approve", headers=b["reviewer"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    _refused(
        await fed.post(f"/v1/federation/connections/{cid}/approve", headers=b["key"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    approved = await fed.post(
        f"/v1/federation/connections/{cid}/approve",
        headers=b["owner"],
        json={"inbound_scopes": ["chat"]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["connection"]["state"] == "ACTIVE"
    mine = (await fed.get("/v1/federation/connections", headers=a["owner"])).json()["items"][0]
    assert (mine["state"], mine["outbound_scopes"]) == ("ACTIVE", ["chat"])

    # Unknown scopes are refused by validation; narrowing works; widening does not.
    bad = await fed.post(
        "/v1/federation/connections",
        headers=a["owner"],
        json={"peer_workspace": b_fid, "outbound_scopes": ["admin"], "reason": "too much"},
    )
    assert bad.status_code == 422, bad.text
    widened = await fed.patch(
        f"/v1/federation/connections/{cid}/scopes",
        headers=b["reviewer"],
        json={"outbound_scopes": ["chat", "delegate"], "inbound_scopes": ["chat"]},
    )
    _refused(widened, 403, "federation_forbidden", "FED_E_SCOPE_DENIED")

    # Either side's person revokes; both sides see it; history is per side.
    revoked = await fed.post(f"/v1/federation/connections/{cid}/revoke", headers=a["reviewer"])
    assert revoked.json()["connection"]["state"] == "REVOKED"
    theirs = (await fed.get("/v1/federation/connections", headers=b["owner"])).json()["items"][0]
    assert theirs["state"] == "REVOKED"
    _refused(
        await fed.post(f"/v1/federation/connections/{cid}/revoke", headers=b["owner"]),
        409,
        "federation_conflict",
        "FED_E_GRANT_TRANSITION",
    )
    history = (
        await fed.get(f"/v1/federation/connections/{cid}/history", headers=b["owner"])
    ).json()["items"]
    assert [h["action"] for h in history] == ["request", "approve", "revoke"]
    assert history[1]["approved_by"].startswith("user:")

    # A connection id from another workspace is not visible here.
    _refused(
        await fed.get(
            f"/v1/federation/connections/{cid}/history",
            headers=(await _workspace(fed, "api-c"))["owner"],
        ),
        404,
        "federation_not_found",
        "FED_E_GRANT_UNKNOWN",
    )


async def test_delegation_over_http_creates_one_real_provider_task(fed):
    """The task bridge end to end: the provider's task comes from the task service."""
    from datetime import datetime, timedelta, timezone

    a, b = await _workspace(fed, "dl-a"), await _workspace(fed, "dl-b")
    (await fed.post("/v1/federation/enable", headers=a["owner"])).json()["identity"][
        "federation_id"
    ]
    b_fid = (await fed.post("/v1/federation/enable", headers=b["owner"])).json()["identity"][
        "federation_id"
    ]
    cid = (
        await fed.post(
            "/v1/federation/connections",
            headers=a["owner"],
            json={
                "peer_workspace": b_fid,
                "outbound_scopes": ["chat", "delegate"],
                "inbound_scopes": ["chat"],
                "reason": "outsourcing pilot",
            },
        )
    ).json()["connection"]["connection_id"]
    assert (
        await fed.post(f"/v1/federation/connections/{cid}/approve", headers=b["owner"])
    ).status_code == 200

    terms = {
        "objective": "Translate the onboarding guide into Spanish.",
        "acceptance_criteria": ["Every section translated", "Terminology list followed"],
        "input_summary": "A 12-page public onboarding guide.",
        "data_classification": "public",
        "allowed_use": "Only for this task.",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "budget_cap": None,
        "result_contract": "One Markdown file.",
        "cancellation_policy": "drain",
        "external_side_effects": False,
    }
    offered = await fed.post(
        "/v1/federation/delegations",
        headers=a["owner"],
        json={"peer_workspace": b_fid, "terms": terms},
    )
    assert offered.status_code == 201, offered.text
    job = offered.json()["delegation"]["federation_job_id"]
    _no_local_ids(offered, a["id"], b["id"])

    # Only a person accepts; the workspace key is an agent.
    _refused(
        await fed.post(f"/v1/federation/delegations/{job}/accept", headers=b["key"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    accepted = await fed.post(f"/v1/federation/delegations/{job}/accept", headers=b["reviewer"])
    assert accepted.status_code == 200, accepted.text
    task_key = accepted.json()["delegation"]["local_task_key"]
    task = await fed.get(f"/v1/tasks/{task_key}", headers=b["owner"])
    assert task.status_code == 200, task.text
    body = task.json()
    assert body["title"].startswith("[federation] Translate")
    assert [item["text"] for item in body["acceptance"]] == terms["acceptance_criteria"]
    assert body["input_context"]["federation"]["content_origin"] == "federation"
    # A cannot see B's task, and the key never reached A.
    assert (await fed.get(f"/v1/tasks/{task_key}", headers=a["owner"])).status_code == 404
    mine = (await fed.get(f"/v1/federation/delegations/{job}", headers=a["owner"])).json()
    assert (
        mine["delegation"]["state"] == "ACCEPTED" and mine["delegation"]["local_task_key"] is None
    )

    submitted = await fed.post(
        f"/v1/federation/delegations/{job}/submit",
        headers=b["key"],
        json={
            "result": {
                "summary": "Done.",
                "artifacts": [{"name": "guide.es.md", "content": "# Guía"}],
            }
        },
    )
    assert submitted.status_code == 200, submitted.text
    done = await fed.post(f"/v1/federation/delegations/{job}/accept_result", headers=a["owner"])
    assert done.json()["delegation"]["state"] == "ACCEPTED_RESULT"
    theirs = (await fed.get(f"/v1/federation/delegations/{job}", headers=b["owner"])).json()
    assert theirs["delegation"]["state"] == "ACCEPTED_RESULT"
    assert [h["action"] for h in theirs["history"]] == [
        "offer",
        "accept",
        "submit",
        "accept_result",
    ]


async def test_cancelling_a_delegation_cancels_the_provider_task(fed):
    from datetime import datetime, timedelta, timezone

    a, b = await _workspace(fed, "cx-a"), await _workspace(fed, "cx-b")
    await fed.post("/v1/federation/enable", headers=a["owner"])
    b_fid = (await fed.post("/v1/federation/enable", headers=b["owner"])).json()["identity"][
        "federation_id"
    ]
    cid = (
        await fed.post(
            "/v1/federation/connections",
            headers=a["owner"],
            json={
                "peer_workspace": b_fid,
                "outbound_scopes": ["delegate"],
                "inbound_scopes": [],
                "reason": "cancel drill",
            },
        )
    ).json()["connection"]["connection_id"]
    await fed.post(f"/v1/federation/connections/{cid}/approve", headers=b["owner"])
    terms = {
        "objective": "Draft release notes.",
        "acceptance_criteria": ["Covers every change"],
        "input_summary": "The public changelog.",
        "data_classification": "public",
        "allowed_use": "Only for this task.",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "budget_cap": None,
        "result_contract": "One Markdown file.",
        "cancellation_policy": "drain",
        "external_side_effects": False,
    }
    job = (
        await fed.post(
            "/v1/federation/delegations",
            headers=a["owner"],
            json={"peer_workspace": b_fid, "terms": terms},
        )
    ).json()["delegation"]["federation_job_id"]
    task_key = (
        await fed.post(f"/v1/federation/delegations/{job}/accept", headers=b["owner"])
    ).json()["delegation"]["local_task_key"]

    asked = await fed.post(
        f"/v1/federation/delegations/{job}/cancel",
        headers=a["reviewer"],
        json={"reason": "plans changed"},
    )
    assert asked.status_code == 200, asked.text
    assert asked.json()["delegation"]["cancel"]["work_may_continue"] is True
    _refused(
        await fed.post(f"/v1/federation/delegations/{job}/cancel_ack", headers=b["key"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    confirmed = await fed.post(f"/v1/federation/delegations/{job}/cancel_ack", headers=b["owner"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["delegation"]["state"] == "CANCELLED"
    task = (await fed.get(f"/v1/tasks/{task_key}", headers=b["owner"])).json()
    assert task["status"] == "CANCELLED"
    mine = (await fed.get(f"/v1/federation/delegations/{job}", headers=a["owner"])).json()
    assert mine["delegation"]["state"] == "CANCELLED"
    assert mine["delegation"]["cancel"]["work_may_continue"] is False


async def test_an_accept_that_failed_after_creating_the_task_can_be_retried(fed):
    """Accepting creates the provider's task (committed by the task service)
    before the delegation commits. If the delegation's commit then fails, the
    task is left behind; a retry used to hit 409 task_exists for good."""
    from datetime import datetime, timedelta, timezone

    import asyncpg

    a, b = await _workspace(fed, "retry-a"), await _workspace(fed, "retry-b")
    await fed.post("/v1/federation/enable", headers=a["owner"])
    b_fid = (await fed.post("/v1/federation/enable", headers=b["owner"])).json()["identity"][
        "federation_id"
    ]
    cid = (
        await fed.post(
            "/v1/federation/connections",
            headers=a["owner"],
            json={
                "peer_workspace": b_fid,
                "outbound_scopes": ["delegate"],
                "inbound_scopes": [],
                "reason": "retry drill",
            },
        )
    ).json()["connection"]["connection_id"]
    await fed.post(f"/v1/federation/connections/{cid}/approve", headers=b["owner"])
    terms = {
        "objective": "Check the release checklist.",
        "acceptance_criteria": ["Every item is ticked or explained"],
        "input_summary": "The public checklist.",
        "data_classification": "public",
        "allowed_use": "Only for this task.",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "budget_cap": None,
        "result_contract": "A short note.",
        "cancellation_policy": "drain",
        "external_side_effects": False,
    }
    job = (
        await fed.post(
            "/v1/federation/delegations",
            headers=a["owner"],
            json={"peer_workspace": b_fid, "terms": terms},
        )
    ).json()["delegation"]["federation_job_id"]
    assert re.fullmatch(r"fjob_[a-z2-7]{26}", job)

    db = await asyncpg.connect(
        os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    )
    try:
        # The delegation's own commit fails once, after the task was created.
        await db.execute(
            "CREATE FUNCTION zz_fail_accept() RETURNS trigger AS $$ BEGIN "
            "RAISE EXCEPTION 'injected failure'; END; $$ LANGUAGE plpgsql"
        )
        await db.execute(
            "CREATE TRIGGER zz_fail_accept BEFORE UPDATE ON federation_delegations "
            "FOR EACH ROW WHEN (NEW.state = 'ACCEPTED' AND NEW.role = 'provider' "
            f"AND NEW.federation_job_id = '{job}') EXECUTE FUNCTION zz_fail_accept()"
        )
        failed = await fed.post(f"/v1/federation/delegations/{job}/accept", headers=b["owner"])
        assert failed.status_code == 503, failed.text
    finally:
        await db.execute("DROP TRIGGER IF EXISTS zz_fail_accept ON federation_delegations")
        await db.execute("DROP FUNCTION IF EXISTS zz_fail_accept()")
        await db.close()
    task_key = f"fed-{job[5:]}"
    assert (await fed.get(f"/v1/tasks/{task_key}", headers=b["owner"])).status_code == 200

    retried = await fed.post(f"/v1/federation/delegations/{job}/accept", headers=b["owner"])
    assert retried.status_code == 200, retried.text
    assert retried.json()["delegation"]["state"] == "ACCEPTED"
    assert retried.json()["delegation"]["local_task_key"] == task_key
    mine = (await fed.get(f"/v1/federation/delegations/{job}", headers=a["owner"])).json()
    assert mine["delegation"]["state"] == "ACCEPTED"
