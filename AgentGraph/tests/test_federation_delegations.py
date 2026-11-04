"""The task bridge between workspaces (federation F-07/F-08, docs/federation/ICD_I_BRIDGE.md).

Vision acceptance covered here: F-002 (no delegate scope: refused with no side
effect), F-012 (a repeated offer yields one provider task), F-013 (after an
outage the queued objects catch up without a second task), F-008 (an older
object never moves state back), F-014 (a rejected result goes to dispute, not
to done), F-015 (the accepted terms must be the offered terms), F-006/F-018
(local ids and credentials never cross).
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.services.federation import delegations, grants, identity
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor

pytestmark = pytest.mark.integration


@pytest.fixture
def federation_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_kek", base64.b64encode(os.urandom(32)).decode())
    return settings


class FlakyTransport(grants.LocalTransport):
    def __init__(self) -> None:
        self.up = True

    async def deliver(self, envelope):
        if not self.up:
            raise FederationError("FED_E_UNREACHABLE", "peer unreachable (test)")
        return await super().deliver(envelope)


@pytest.fixture
def transport():
    flaky = FlakyTransport()
    grants.set_transport(flaky)
    try:
        yield flaky
    finally:
        grants.set_transport(grants.LocalTransport())


async def _ws(client, admin_headers, name: str) -> dict:
    response = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={"name": f"{name}-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    workspace_id = response.json()["workspace"]["id"]
    view = await identity.enable_workspace(workspace_id, actor=f"user:owner-{name}")
    return {
        "id": workspace_id,
        "fid": view["federation_id"],
        "owner": Actor("user", f"owner-{name}", is_owner=True),
        "person": Actor("user", f"member-{name}"),
        "agent": Actor("agent", f"bot-{name}"),
    }


async def _connect(a, b, *, a_to_b=("chat", "delegate")) -> str:
    asked = await grants.request(
        a["id"],
        peer_workspace=b["fid"],
        outbound_scopes=list(a_to_b),
        inbound_scopes=["chat"],
        reason="pilot collaboration",
        actor=a["owner"],
    )
    await grants.approve(b["id"], asked["connection_id"], actor=b["owner"])
    return asked["connection_id"]


def _terms(**overrides) -> dict:
    terms = {
        "objective": "Summarise the three public reports on regional grid demand.",
        "acceptance_criteria": ["Cites each report", "Under 800 words"],
        "input_summary": "Three public PDF reports, titles listed in the brief.",
        "data_classification": "public",
        "allowed_use": "Only for this task; do not keep the inputs.",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=3))
        .replace(microsecond=0)
        .isoformat(),
        "budget_cap": None,
        "result_contract": "One Markdown report plus a list of sources.",
        "cancellation_policy": "drain",
        "external_side_effects": False,
    }
    terms.update(overrides)
    return terms


class Tasks:
    """Stands in for the provider's task service: records each task created."""

    def __init__(self) -> None:
        self.created: list[str] = []

    async def create(self, row) -> str:
        key = f"fed-{row['federation_job_id'][5:]}"
        self.created.append(key)
        return key


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _one(ws, job_id) -> dict:
    return (await delegations.get(ws["id"], job_id))["delegation"]


RESULT = {
    "summary": "Demand grows 4% a year in all three reports.",
    "artifacts": [{"name": "report.md", "content": "# Grid demand\n\n..."}],
    "cost_receipt": {
        "metering_source": "self_reported",
        "confidence": "low",
        "usage": {"model_tokens": 18000},
    },
}


async def test_without_delegate_nothing_is_written(client, admin_headers, federation_on):
    """F-002."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b, a_to_b=("chat",))
    assert (
        await _code(
            delegations.offer(a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"])
        )
        == "FED_E_SCOPE_DENIED"
    )
    assert await delegations.list_delegations(a["id"]) == []
    assert await delegations.list_delegations(b["id"]) == []
    # A peer that skips its own check is still refused on arrival.
    forged = await identity.sign_as(
        a["fid"],
        type="delegate.offer",
        audience=b["fid"],
        payload={
            "federation_job_id": delegations.new_job_id(),
            "version": 1,
            "terms": _terms(),
            "offer_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert await _code(delegations.receive(b["id"], forged)) == "FED_E_SCOPE_DENIED"
    assert await delegations.list_delegations(b["id"]) == []


async def test_offer_accept_result_revise_and_accept(client, admin_headers, federation_on):
    """The whole path, F-012 (one task per offer) and a revision round."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    tasks = Tasks()
    sent = await delegations.offer(
        a["id"],
        peer_workspace=b["fid"],
        terms=_terms(),
        actor=a["owner"],
        local_task_key="research-outsourced",
    )
    job = sent["delegation"]["federation_job_id"]
    assert sent["delivered"] == 1 and sent["delegation"]["pending_deliveries"] == 0
    theirs = await _one(b, job)
    assert (theirs["role"], theirs["state"], theirs["version"]) == ("provider", "OFFERED", 1)
    assert theirs["terms"] == sent["delegation"]["terms"]
    assert theirs["acceptance_hash"] == sent["delegation"]["acceptance_hash"]
    assert theirs["untrusted_fields"] == ["terms"]
    assert theirs["local_task_key"] is None  # A's local key never travelled

    # F-012: the same delegation re-sent is acknowledged, not duplicated.
    again = await identity.sign_as(
        a["fid"],
        type="delegate.offer",
        audience=b["fid"],
        payload={
            "federation_job_id": job,
            "version": 1,
            "terms": theirs["terms"],
            "offer_expires_at": theirs["offer_expires_at"],
        },
    )
    assert (await delegations.receive(b["id"], again))["status"] == "duplicate"
    assert len(await delegations.list_delegations(b["id"])) == 1

    # Only a person takes on work; accepting creates exactly one provider task.
    assert (
        await _code(
            delegations.act(
                b["id"], job, "accept", actor=b["agent"], create_local_task=tasks.create
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    accepted = await delegations.act(
        b["id"], job, "accept", actor=b["person"], create_local_task=tasks.create
    )
    assert accepted["delegation"]["state"] == "ACCEPTED"
    assert (
        await _code(
            delegations.act(
                b["id"], job, "accept", actor=b["person"], create_local_task=tasks.create
            )
        )
        == "FED_E_JOB_TRANSITION"
    )
    assert len(tasks.created) == 1
    assert (await _one(a, job))["state"] == "ACCEPTED"

    # The provider's agent delivers; the requester sees the result as untrusted data.
    await delegations.act(b["id"], job, "submit", actor=b["agent"], result=RESULT)
    mine = await delegations.get(a["id"], job)
    assert mine["delegation"]["state"] == "SUBMITTED"
    assert mine["delegation"]["untrusted_fields"] == ["result"]
    assert mine["delegation"]["result"]["artifacts"][0]["sha256"] == (
        __import__("hashlib").sha256(b"# Grid demand\n\n...").hexdigest()
    )
    assert mine["cost_receipts"][0]["metering_source"] == "self_reported"

    # A person asks for a revision, the provider resubmits, a person accepts.
    assert await _code(delegations.act(a["id"], job, "revise", actor=a["agent"])) == (
        "FED_E_APPROVAL_NOT_HUMAN"
    )
    await delegations.act(a["id"], job, "revise", actor=a["person"], note="Add the 2025 figures.")
    assert (await _one(b, job))["state"] == "RUNNING"
    await delegations.act(
        b["id"],
        job,
        "submit",
        actor=b["agent"],
        result={**RESULT, "summary": "Now with 2025 figures."},
    )
    done = await delegations.act(a["id"], job, "accept_result", actor=a["owner"])
    assert done["delegation"]["state"] == "ACCEPTED_RESULT"
    final_b = await delegations.get(b["id"], job)
    assert (
        final_b["delegation"]["state"] == "ACCEPTED_RESULT"
        and final_b["delegation"]["revisions"] == 1
    )
    assert [h["version"] for h in final_b["history"]] == [1, 2, 3, 4, 5, 6]
    assert [h["action"] for h in (await delegations.get(a["id"], job))["history"]] == [
        "offer",
        "accept",
        "submit",
        "revise",
        "submit",
        "accept_result",
    ]


async def test_rejected_result_goes_to_dispute(client, admin_headers, federation_on):
    """F-014: refusing the result does not complete the work or release anything."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    job = (
        await delegations.offer(a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"])
    )["delegation"]["federation_job_id"]
    await delegations.act(
        b["id"], job, "accept", actor=b["owner"], create_local_task=Tasks().create
    )
    await delegations.act(b["id"], job, "submit", actor=b["agent"], result=RESULT)
    await delegations.act(a["id"], job, "dispute", actor=a["person"], note="Sources missing.")
    assert (await _one(a, job))["state"] == "DISPUTED" and (await _one(b, job))[
        "state"
    ] == "DISPUTED"
    assert await _code(delegations.act(a["id"], job, "accept_result", actor=a["owner"])) == (
        "FED_E_JOB_TRANSITION"
    )


async def test_accepted_terms_must_be_the_offered_terms(client, admin_headers, federation_on):
    """F-015: an acceptance of different terms is refused."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    job = (
        await delegations.offer(a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"])
    )["delegation"]["federation_job_id"]
    altered = delegations.acceptance_hash(
        delegations.validate_terms(_terms(acceptance_criteria=["Anything goes"]))
    )
    forged = await identity.sign_as(
        b["fid"],
        type="delegate.accept",
        audience=a["fid"],
        payload={"federation_job_id": job, "version": 2, "acceptance_hash": altered},
    )
    assert await _code(delegations.receive(a["id"], forged)) == "FED_E_ACCEPTANCE_MISMATCH"
    assert (await _one(a, job))["state"] == "OFFERED"


async def test_outage_catches_up_in_order_without_a_second_task(
    client, admin_headers, federation_on, transport
):
    """F-013 and F-008."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    tasks = Tasks()
    transport.up = False
    sent = await delegations.offer(
        a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"]
    )
    job = sent["delegation"]["federation_job_id"]
    assert (
        sent["last_error"] == "FED_E_UNREACHABLE" and sent["delegation"]["pending_deliveries"] == 1
    )
    assert await delegations.list_delegations(b["id"]) == []
    transport.up = True
    assert (await delegations.sync(a["id"], job))["delivered"] == 1

    # B accepts and submits while A is unreachable: both stay queued, in order.
    transport.up = False
    await delegations.act(b["id"], job, "accept", actor=b["owner"], create_local_task=tasks.create)
    queued = await delegations.act(b["id"], job, "submit", actor=b["agent"], result=RESULT)
    assert queued["delegation"]["pending_deliveries"] == 2
    assert (await _one(a, job))["state"] == "OFFERED"
    transport.up = True
    caught_up = await delegations.sync(b["id"], job)
    assert caught_up["delivered"] == 2 and caught_up["delegation"]["pending_deliveries"] == 0
    assert (await _one(a, job))["state"] == "SUBMITTED"
    assert len(tasks.created) == 1

    # F-008: an older object arriving late changes nothing; a gap is refused.
    late = await identity.sign_as(
        b["fid"],
        type="delegate.accept",
        audience=a["fid"],
        payload={
            "federation_job_id": job,
            "version": 2,
            "acceptance_hash": sent["delegation"]["acceptance_hash"],
        },
    )
    assert (await delegations.receive(a["id"], late))["status"] == "duplicate"
    assert (await _one(a, job))["state"] == "SUBMITTED"
    gap = await identity.sign_as(
        a["fid"],
        type="delegate.accept_result",
        audience=b["fid"],
        payload={"federation_job_id": job, "version": 9},
    )
    assert await _code(delegations.receive(b["id"], gap)) == "FED_E_JOB_STALE"


async def test_what_may_not_cross(client, admin_headers, federation_on):
    """F-006 / F-018 / ID-INV-8, settlement off, and the revocation fence."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    cid = await _connect(a, b)

    def offer(**kw):
        return delegations.offer(
            a["id"],
            peer_workspace=b["fid"],
            terms=_terms(**kw),
            actor=a["owner"],
        )

    assert await _code(offer(input_summary="See prn_01ABCDEF in our directory")) == (
        "FED_E_TERMS_INVALID"
    )
    assert await _code(offer(input_summary="Use Bearer abcdefghijklmnopqrstuvwxyz0123")) == (
        "FED_E_CREDENTIAL_IN_PAYLOAD"
    )
    assert await _code(offer(budget_cap={"amount": 50, "unit": "USD"})) == (
        "FED_E_SETTLEMENT_DISABLED"
    )
    assert await _code(offer(cancellation_policy="hard_cancel")) == "FED_E_TERMS_INVALID"
    assert (
        await _code(
            delegations.offer(a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["agent"])
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )

    job = (await offer())["delegation"]["federation_job_id"]
    await delegations.act(
        b["id"], job, "accept", actor=b["owner"], create_local_task=Tasks().create
    )
    leaking = {**RESULT, "summary": "Our key is sk-live-abcdefghijklmnop"}
    assert await _code(
        delegations.act(b["id"], job, "submit", actor=b["agent"], result=leaking)
    ) == ("FED_E_CREDENTIAL_IN_PAYLOAD")
    # After A revokes the connection, B can no longer deliver into it.
    await grants.revoke(a["id"], cid, actor=a["owner"])
    assert await _code(
        delegations.act(b["id"], job, "submit", actor=b["agent"], result=RESULT)
    ) == ("FED_E_GRANT_INACTIVE")


async def _sql(query: str, *args):
    import asyncpg

    conn = await asyncpg.connect(
        os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    )
    try:
        return await conn.execute(query, *args)
    finally:
        await conn.close()


async def test_undelivered_objects_retry_then_dead_letter(
    client, admin_headers, federation_on, transport
):
    """Automatic retries with backoff; a dead letter waits for a person (ICD_I_MSG §4.1)."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    transport.up = False
    sent = await delegations.offer(
        a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"]
    )
    job = sent["delegation"]["federation_job_id"]
    delivery = sent["delegation"]["delivery"]
    assert delivery["attempts"] == 1 and delivery["last_error"] == "FED_E_UNREACHABLE"
    assert delivery["next_retry_at"] is not None and not delivery["dead_letter"]
    # Not due yet: the pass leaves it alone.
    await delegations.deliver_due()
    assert (await _one(a, job))["delivery"]["attempts"] == 1
    # Due and reachable again: the pass delivers it.
    await _sql(
        "UPDATE federation_delegations SET next_retry_at = NOW() - interval '1 second' "
        "WHERE federation_job_id = $1",
        job,
    )
    transport.up = True
    await delegations.deliver_due()
    mine = await _one(a, job)
    assert mine["pending_deliveries"] == 0 and mine["delivery"]["attempts"] == 0
    assert (await _one(b, job))["state"] == "OFFERED"

    # Out of attempts: dead letter, no more automatic retries.
    await delegations.act(
        b["id"], job, "accept", actor=b["owner"], create_local_task=Tasks().create
    )
    transport.up = False
    await delegations.act(b["id"], job, "submit", actor=b["agent"], result=RESULT)
    await _sql(
        "UPDATE federation_delegations SET delivery_attempts = $2, "
        "next_retry_at = NOW() - interval '1 second' WHERE federation_job_id = $1 "
        "AND role = 'provider'",
        job,
        delegations.MAX_DELIVERY_ATTEMPTS - 1,
    )
    await delegations.deliver_due()
    theirs = await _one(b, job)
    assert theirs["delivery"]["dead_letter"] and theirs["delivery"]["next_retry_at"] is None
    transport.up = True
    await delegations.deliver_due()
    assert (await _one(b, job))["pending_deliveries"] == 1  # still waiting for a person
    revived = await delegations.sync(b["id"], job, resign=True)
    assert revived["delivered"] == 1 and not revived["delegation"]["delivery"]["dead_letter"]
    assert (await _one(a, job))["state"] == "SUBMITTED"


async def test_an_expired_queued_object_is_dead_lettered(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    transport.up = False
    job = (
        await delegations.offer(a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"])
    )["delegation"]["federation_job_id"]
    await _sql(
        "UPDATE federation_delegations SET outbox = jsonb_set(outbox, '{0,expires_at}', "
        "'\"2020-01-01T00:00:00Z\"'), next_retry_at = NOW() - interval '1 second' "
        "WHERE federation_job_id = $1",
        job,
    )
    transport.up = True
    await delegations.deliver_due()
    view = await _one(a, job)
    assert view["delivery"]["dead_letter"] and view["delivery"]["last_error"] == "FED_E_EXPIRED"
    assert await delegations.list_delegations(b["id"]) == []
    await delegations.sync(a["id"], job, resign=True)
    assert (await _one(b, job))["state"] == "OFFERED"


async def test_cancellation_is_asked_then_confirmed(client, admin_headers, federation_on):
    """F-016: stopping is requested, never assumed; confirmed by the provider; audited."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    job = (
        await delegations.offer(a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"])
    )["delegation"]["federation_job_id"]
    await delegations.act(
        b["id"], job, "accept", actor=b["owner"], create_local_task=Tasks().create
    )

    assert (
        await _code(
            delegations.request_cancel(a["id"], job, actor=a["agent"], reason="changed plans")
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    assert (
        await _code(delegations.request_cancel(b["id"], job, actor=b["owner"], reason="too busy"))
        == "FED_E_JOB_TRANSITION"
    )
    asked = await delegations.request_cancel(
        a["id"], job, actor=a["person"], reason="changed plans"
    )
    cancel = asked["delegation"]["cancel"]
    assert cancel["work_may_continue"] and not cancel["stop_not_confirmed"]
    assert asked["delegation"]["state"] == "ACCEPTED"  # asking is not stopping
    theirs = await _one(b, job)
    assert theirs["cancel"]["reason"] == "changed plans"
    assert theirs["cancel"]["requested_by"] == f"peer:{a['fid']}"
    assert (
        await _code(delegations.request_cancel(a["id"], job, actor=a["owner"], reason="again"))
        == "FED_E_JOB_TRANSITION"
    )

    # Unconfirmed for longer than the timeout: say so, do not pretend.
    await _sql(
        "UPDATE federation_delegations SET cancel_requested_at = NOW() - interval '2 hours' "
        "WHERE federation_job_id = $1 AND role = 'requester'",
        job,
    )
    late = (await _one(a, job))["cancel"]
    assert late["stop_not_confirmed"] and late["work_may_continue"]

    # The provider's person confirms; its own task is cancelled with it.
    cancelled: list[str] = []

    async def cancel_task(row):
        cancelled.append(row["local_task_key"])

    assert (
        await _code(
            delegations.act(
                b["id"], job, "cancel_ack", actor=b["agent"], cancel_local_task=cancel_task
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    await delegations.act(
        b["id"], job, "cancel_ack", actor=b["person"], cancel_local_task=cancel_task
    )
    assert cancelled == [f"fed-{job[5:]}"]
    for ws in (a, b):
        view = await _one(ws, job)
        assert view["state"] == "CANCELLED"
        assert not view["cancel"]["work_may_continue"] and not view["cancel"]["stop_not_confirmed"]
    assert (
        await _code(delegations.act(b["id"], job, "submit", actor=b["agent"], result=RESULT))
        == "FED_E_JOB_TRANSITION"
    )
    history = (await delegations.get(a["id"], job))["history"]
    assert [h["action"] for h in history] == ["offer", "accept", "cancel_ack"]


async def test_a_withdrawn_offer_cannot_be_taken_on(client, admin_headers, federation_on):
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    job = (
        await delegations.offer(a["id"], peer_workspace=b["fid"], terms=_terms(), actor=a["owner"])
    )["delegation"]["federation_job_id"]
    await delegations.request_cancel(a["id"], job, actor=a["owner"], reason="no longer needed")
    tasks = Tasks()
    assert (
        await _code(
            delegations.act(
                b["id"], job, "accept", actor=b["owner"], create_local_task=tasks.create
            )
        )
        == "FED_E_JOB_TRANSITION"
    )
    assert tasks.created == []
    await delegations.act(b["id"], job, "cancel_ack", actor=b["owner"])
    assert (await _one(a, job))["state"] == "CANCELLED"
