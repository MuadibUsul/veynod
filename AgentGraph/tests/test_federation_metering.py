"""Usage metering for delegated work (federation F-09, ICD_I_BRIDGE §8).

A-02 usage summary and cost evidence can be looked up; A-03 an enforced cap
is hard (no new run past it); A-04 shadow, enforce and self-reported are kept
apart. Everything here is a usage record, never money.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.services.federation import delegations, metering
from tests import test_federation_delegations as _base
from tests.test_federation_delegations import RESULT, Tasks, _code, _connect, _terms, _ws
from tests.test_federation_p1_gate import db_dsn

pytestmark = pytest.mark.integration

# The delegation tests' fixtures, shared.
federation_on = _base.federation_on
transport = _base.transport
# A delivery with no receipt typed in: usage comes from the metered events.
PLAIN = {k: v for k, v in RESULT.items() if k != "cost_receipt"}


async def _running(client, admin_headers, cap: dict | None) -> tuple[dict, dict, str]:
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    await _connect(a, b)
    job = (
        await delegations.offer(
            a["id"], peer_workspace=b["fid"], actor=a["owner"], terms=_terms(budget_cap=cap)
        )
    )["delegation"]["federation_job_id"]
    await delegations.act(
        b["id"], job, "accept", actor=b["owner"], create_local_task=Tasks().create
    )
    return a, b, job


def _use(ws, job, kind, unit, amount, **extra):
    return metering.use(
        ws["id"], job, kind=kind, unit=unit, amount=amount, actor_ref=ws["agent"].ref, **extra
    )


async def test_usage_summary_and_evidence_can_be_looked_up(
    client, admin_headers, federation_on, transport
):
    """A-02: per unit and per source, with the evidence behind each number."""
    a, b, job = await _running(client, admin_headers, {"amount": 10_000, "unit": "model_tokens"})
    await _use(b, job, "reserve", "model_tokens", 6000)
    await _use(b, job, "record", "model_tokens", 3000)
    await _use(
        b,
        job,
        "record",
        "model_tokens",
        2000,
        source="adapter",
        proof_ref="billing-export:2026-11-05#line-42",
        price_snapshot_id="price-2026-11",
    )
    await _use(b, job, "record", "tool_calls", 12)
    # Only this server measures as the platform; an adapter needs a proof.
    assert await _code(_use(b, job, "record", "model_tokens", 1, source="platform")) == (
        "FED_E_TERMS_INVALID"
    )
    assert await _code(_use(b, job, "record", "model_tokens", 1, source="adapter")) == (
        "FED_E_TERMS_INVALID"
    )
    provider = (await delegations.get(b["id"], job))["metering"]
    assert provider["usage"]["model_tokens"] == {
        "total": 5000,
        "measured": 2000,
        "self_reported": 3000,
        "by_source": {"self_reported": 3000, "adapter": 2000},
    }
    assert provider["reservations"] == 1
    assert {e["kind"] for e in provider["evidence"]} == {"reserve", "record"}
    assert provider["not_money"] is True

    await delegations.act(b["id"], job, "submit", actor=b["agent"], result=PLAIN)
    requester = await delegations.get(a["id"], job)
    usage = requester["metering"]["usage"]
    assert usage["model_tokens"]["measured"] == 2000
    assert usage["model_tokens"]["self_reported"] == 3000
    assert usage["tool_calls"]["self_reported"] == 12
    # The platform measured how long the run took, on its own.
    assert usage["minutes"]["by_source"] == {"platform": usage["minutes"]["total"]}
    assert usage["minutes"]["total"] >= 1
    [receipt] = requester["metering"]["receipts"]
    assert receipt["metering_source"] == "self_reported"  # as sure as its weakest line
    assert receipt["proof_ref"] == "billing-export:2026-11-05#line-42"
    assert receipt["price_snapshot_id"] == "price-2026-11"
    assert {
        "unit": "model_tokens",
        "amount": 2000,
        "source": "adapter",
        "proof_ref": "billing-export:2026-11-05#line-42",
        "price_snapshot_id": "price-2026-11",
    } in receipt["usage"]["evidence"]
    assert requester["delegation"]["budget"]["spent"] == 5000


async def test_an_enforced_cap_stops_new_runs(client, admin_headers, federation_on, transport):
    """A-03: past an enforced cap no run starts and no revision is asked for;
    what was already used is still recorded."""
    a, b, job = await _running(client, admin_headers, {"amount": 5000, "unit": "model_tokens"})
    await _use(b, job, "reserve", "model_tokens", 3000)
    await _use(b, job, "record", "model_tokens", 4500)
    assert await _code(_use(b, job, "reserve", "model_tokens", 1000)) == "FED_E_BUDGET_EXCEEDED"
    # Usage that already happened is never refused.
    after = await _use(b, job, "record", "model_tokens", 1000)
    assert after["budget"]["spent"] == 5500 and after["budget"]["blocking"] is True
    # Another unit is not capped.
    await _use(b, job, "reserve", "tool_calls", 50)
    await delegations.act(b["id"], job, "submit", actor=b["agent"], result=PLAIN)
    assert (
        await _code(delegations.act(a["id"], job, "revise", actor=a["owner"], note="again"))
        == "FED_E_BUDGET_EXCEEDED"
    )
    budget = (await delegations.get(a["id"], job))["delegation"]["budget"]
    assert budget["mode"] == "enforce" and budget["over"] and budget["blocking"]


async def test_shadow_records_and_warns_but_never_blocks(
    client, admin_headers, federation_on, transport
):
    """A-04: shadow, enforce and self-reported stay apart."""
    a, b, job = await _running(
        client, admin_headers, {"amount": 1000, "unit": "model_tokens", "mode": "shadow"}
    )
    await _use(b, job, "reserve", "model_tokens", 5000)  # allowed: shadow
    await _use(b, job, "record", "model_tokens", 6000)
    await delegations.act(b["id"], job, "submit", actor=b["agent"], result=PLAIN)
    budget = (await delegations.get(a["id"], job))["delegation"]["budget"]
    assert budget["mode"] == "shadow"
    assert budget["over"] is True and budget["blocking"] is False
    assert "shadow" in budget["warning"]
    revised = await delegations.act(a["id"], job, "revise", actor=a["owner"], note="go on")
    assert revised["delegation"]["state"] == "RUNNING"
    assert (
        await _code(
            delegations.offer(
                a["id"],
                peer_workspace=b["fid"],
                actor=a["owner"],
                terms=_terms(budget_cap={"amount": 1000, "unit": "model_tokens", "mode": "soft"}),
            )
        )
        == "FED_E_SETTLEMENT_DISABLED"
    )


async def test_a_typed_receipt_is_never_taken_as_measured(
    client, admin_headers, federation_on, transport
):
    a, b, job = await _running(client, admin_headers, None)
    await delegations.act(
        b["id"],
        job,
        "submit",
        actor=b["agent"],
        result={
            **RESULT,
            "cost_receipt": {
                "metering_source": "platform",
                "confidence": "high",
                "usage": {"model_tokens": 999},
            },
        },
    )
    [receipt] = (await delegations.get(a["id"], job))["metering"]["receipts"]
    assert receipt["metering_source"] == "self_reported" and receipt["confidence"] == "low"
    assert receipt["usage"]["by_source"]["model_tokens"] == {"self_reported": 999}


async def test_only_the_provider_meters_and_only_while_it_runs(
    client, admin_headers, federation_on, transport
):
    a, b, job = await _running(client, admin_headers, {"amount": 100, "unit": "tool_calls"})
    assert await _code(_use(a, job, "record", "tool_calls", 1)) == "FED_E_JOB_TRANSITION"
    await delegations.request_cancel(a["id"], job, actor=a["owner"], reason="no longer needed")
    assert await _code(_use(b, job, "reserve", "tool_calls", 1)) == "FED_E_JOB_TRANSITION"
    await _use(b, job, "record", "tool_calls", 3)  # what already ran is still recorded
    await delegations.act(b["id"], job, "cancel_ack", actor=b["owner"])
    assert await _code(_use(b, job, "record", "tool_calls", 1)) == "FED_E_JOB_TRANSITION"

    conn = await asyncpg.connect(db_dsn())
    try:
        can_update = await conn.fetchval(
            "SELECT has_table_privilege('agentgraph_app', 'federation_usage_events', 'UPDATE')"
        )
        can_delete = await conn.fetchval(
            "SELECT has_table_privilege('agentgraph_app', 'federation_usage_events', 'DELETE')"
        )
    finally:
        await conn.close()
    assert can_update is False and can_delete is False
