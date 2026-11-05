"""Basic governance (federation F-10, docs/federation/ICD_I_GOVERNANCE.md).

F-019: a report opens a case and never sanctions anyone on its own.
F-020: a "platform notice" forged by a workspace is refused and changes nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation import governance, grants, identity, messaging
from app.services.federation.errors import FederationError
from tests import test_federation_messaging as _base
from tests.test_federation_messaging import _connect, _ws

pytestmark = pytest.mark.integration

federation_on = _base.federation_on
transport = _base.transport


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _status(fid: str) -> str:
    return (await identity.resolve(fid))["status"]


async def _decided(report_id: str) -> None:
    for step in ("TRIAGED", "INVESTIGATING", "ACTION_PROPOSED"):
        await governance.move_case(report_id, step, operator="Ana")
    await governance.move_case(
        report_id, "DECIDED", operator="Ana", decision="Repeated spam after warnings."
    )


async def test_a_report_opens_a_case_and_nothing_else(
    client, admin_headers, federation_on, transport
):
    """F-019: however many reports, nobody is sanctioned without a person."""
    b = await _ws(client, admin_headers, "gb")
    reporters = [await _ws(client, admin_headers, f"g{i}") for i in range(4)]
    for r in reporters:
        await _connect(b, r)
    sent = await messaging.send(
        b["id"], peer_workspace=reporters[0]["fid"], text_body="Buy now!!!", actor=b["agent"]
    )
    first = await governance.file_report(
        reporters[0]["id"],
        accused=b["fid"],
        category="spam",
        statement="Unsolicited advertising, three times today.",
        evidence=[{"kind": "message", "id": sent["message_id"]}],
        actor=reporters[0]["owner"],
    )
    assert first["case_state"] == "SUBMITTED"
    [item] = first["evidence"]
    assert item["payload_sha256"] and item["signature_sha256"]
    assert "Buy now" not in str(first["evidence"])  # a summary, not the content
    for r in reporters[1:]:
        await governance.file_report(
            r["id"],
            accused=b["fid"],
            category="spam",
            statement="Same advertising here as well.",
            actor=r["owner"],
        )
    status = await governance.global_status(b["fid"])
    assert status["active"] == [] and status["proposed"] == []
    assert status["permanently_blocked"] is False
    assert await _status(b["fid"]) == "active"
    # B still talks to everyone; no reporter blocked locally.
    for r in reporters:
        assert (
            await messaging.send(b["id"], peer_workspace=r["fid"], text_body="hi", actor=b["agent"])
        )["state"] == "received"
    cases = await governance.list_cases("SUBMITTED")
    mine = [c for c in cases if c["accused"] == b["fid"]]
    assert len(mine) == 4 and mine[0]["reports_about_accused"] == 4

    # Only a person files, not about itself, and only with its own evidence.
    assert (
        await _code(
            governance.file_report(
                reporters[0]["id"],
                accused=b["fid"],
                category="spam",
                statement="Filed by an agent.",
                actor=reporters[0]["agent"],
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    assert (
        await _code(
            governance.file_report(
                b["id"],
                accused=b["fid"],
                category="other",
                statement="Reporting myself.",
                actor=b["owner"],
            )
        )
        == "FED_E_TERMS_INVALID"
    )
    assert (
        await _code(
            governance.file_report(
                reporters[1]["id"],
                accused=b["fid"],
                category="spam",
                statement="Pointing at someone else's message.",
                evidence=[{"kind": "message", "id": sent["message_id"]}],
                actor=reporters[1]["owner"],
            )
        )
        == "FED_E_CASE_UNKNOWN"
    )


async def test_local_block_is_only_the_reporters_own(
    client, admin_headers, federation_on, transport
):
    a, b, c = (
        await _ws(client, admin_headers, "la"),
        await _ws(client, admin_headers, "lb"),
        await _ws(client, admin_headers, "lc"),
    )
    await _connect(a, b)
    await _connect(c, b)
    await governance.file_report(
        a["id"],
        accused=b["fid"],
        category="abuse",
        statement="Insulting messages to our team.",
        block=True,
        actor=a["owner"],
    )
    [row] = await grants.list_connections(a["id"])
    assert row["state"] == "BLOCKED"
    assert await _status(b["fid"]) == "active"
    assert (
        await messaging.send(c["id"], peer_workspace=b["fid"], text_body="hello", actor=c["agent"])
    )["state"] == "received"


async def test_cases_move_by_hand_in_order_and_can_be_appealed(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "ca"), await _ws(client, admin_headers, "cb")
    await _connect(a, b)
    report = await governance.file_report(
        a["id"],
        accused=b["fid"],
        category="fraud",
        statement="Claimed delivery of work that was never done.",
        actor=a["owner"],
    )
    rid = report["report_id"]
    assert await _code(governance.move_case(rid, "DECIDED", operator="Ana")) == (
        "FED_E_CASE_TRANSITION"
    )
    for step in ("TRIAGED", "INVESTIGATING", "ACTION_PROPOSED"):
        await governance.move_case(rid, step, operator="Ana", note=f"step {step}")
    assert await _code(governance.move_case(rid, "DECIDED", operator="Ana")) == (
        "FED_E_TERMS_INVALID"  # a decision needs its reasons
    )
    await governance.move_case(rid, "DECIDED", operator="Ana", decision="Warning issued.")
    await governance.move_case(rid, "APPEAL_WINDOW", operator="Ana")
    assert await _code(governance.withdraw(a["id"], rid, actor=a["owner"])) == (
        "FED_E_CASE_TRANSITION"
    )
    # The accused sees the decision, not who reported or what they wrote.
    [notice] = await governance.notices(b["id"])
    assert notice["decision"] == "Warning issued." and notice["appeal_until"]
    assert "statement" not in notice and "reporter" not in str(notice)
    assert await _code(governance.move_case(rid, "CLOSED", operator="Ana")) == (
        "FED_E_CASE_TRANSITION"  # the window is still open and nobody appealed
    )
    assert (
        await _code(
            governance.appeal(
                b["id"], rid, statement="The work was delivered on time.", actor=b["agent"]
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    await governance.appeal(
        b["id"], rid, statement="The work was delivered on time, see job log.", actor=b["owner"]
    )
    assert (
        await _code(
            governance.appeal(b["id"], rid, statement="Again, please reconsider.", actor=b["owner"])
        )
        == "FED_E_CASE_TRANSITION"
    )
    closed = await governance.move_case(rid, "CLOSED", operator="Bo", note="appeal reviewed")
    assert closed["case_state"] == "CLOSED"
    assert [h["to_state"] for h in closed["history"]][:2] == ["SUBMITTED", "TRIAGED"]
    assert any(h["actor"] == "operator:Bo" for h in closed["history"])


async def test_sanctions_follow_decisions_and_a_permanent_block_needs_two_operators(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "sa"), await _ws(client, admin_headers, "sb")
    await _connect(a, b)
    rid = (
        await governance.file_report(
            a["id"],
            accused=b["fid"],
            category="spam",
            statement="Mass unsolicited messages.",
            actor=a["owner"],
        )
    )["report_id"]
    assert (
        await _code(
            governance.propose_sanction(
                rid, kind="temporary_freeze", reason="too early", operator="Ana", days=7
            )
        )
        == "FED_E_SANCTION_RULE"
    )
    await _decided(rid)
    freeze = await governance.propose_sanction(
        rid, kind="temporary_freeze", reason="Spam while under review.", operator="Ana", days=7
    )
    assert freeze["state"] == "active"
    assert await _status(b["fid"]) == "frozen"
    assert (
        await _code(
            messaging.send(a["id"], peer_workspace=b["fid"], text_body="hi", actor=a["agent"])
        )
        == "FED_E_IDENTITY_FROZEN"
    )
    await governance.lift_sanction(freeze["sanction_id"], operator="Bo", reason="Reviewed.")
    assert await _status(b["fid"]) == "active"

    # A permanent block: only after the case is closed, and never by one person.
    assert (
        await _code(
            governance.propose_sanction(
                rid, kind="permanent_block", reason="Repeat offender.", operator="Ana"
            )
        )
        == "FED_E_SANCTION_RULE"
    )
    await governance.move_case(rid, "APPEAL_WINDOW", operator="Ana")
    async with async_session_factory() as platform:  # let the appeal window pass
        await platform.execute(
            text(
                "UPDATE federation_reports SET appeal_until = NOW() - INTERVAL '1 day' "
                "WHERE report_id = :r"
            ),
            {"r": rid},
        )
        await platform.commit()
    await governance.move_case(rid, "CLOSED", operator="Ana")
    proposed = await governance.propose_sanction(
        rid, kind="permanent_block", reason="Repeat offender.", operator="Ana"
    )
    assert proposed["state"] == "proposed"
    assert await _status(b["fid"]) == "active"  # proposing changes nothing
    assert await _code(governance.confirm_sanction(proposed["sanction_id"], operator="Ana")) == (
        "FED_E_SANCTION_RULE"
    )
    confirmed = await governance.confirm_sanction(proposed["sanction_id"], operator="Bo")
    assert confirmed["state"] == "active" and confirmed["confirmed_by"] == "operator:Bo"
    status = await governance.global_status(b["fid"])
    assert status["permanently_blocked"] is True and status["identity_status"] == "frozen"


async def test_a_temporary_freeze_ends_on_time(client, admin_headers, federation_on, transport):
    a, b = await _ws(client, admin_headers, "ea"), await _ws(client, admin_headers, "eb")
    await _connect(a, b)
    rid = (
        await governance.file_report(
            a["id"], accused=b["fid"], category="spam", statement="Spam again.", actor=a["owner"]
        )
    )["report_id"]
    await _decided(rid)
    freeze = await governance.propose_sanction(
        rid, kind="temporary_freeze", reason="Short cool-down.", operator="Ana", days=1
    )
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "UPDATE federation_sanctions SET expires_at = NOW() - INTERVAL '1 minute' "
                "WHERE sanction_id = :s"
            ),
            {"s": freeze["sanction_id"]},
        )
        await platform.commit()
    assert await governance.expire_due() >= 1
    assert await _status(b["fid"]) == "active"


async def test_a_forged_platform_notice_changes_nothing(
    client, admin_headers, federation_on, transport
):
    """F-020: signed by a workspace, tampered, or dressed up as chat: refused."""
    a, b = await _ws(client, admin_headers, "fa"), await _ws(client, admin_headers, "fb")
    await _connect(a, b)
    before = await governance.global_status(a["fid"])
    notice = await identity.sign_as(
        b["fid"],
        type="governance.notice",
        audience=a["fid"],
        payload={
            "verdict": "permanent_block",
            "subject": a["fid"],
            "issued_by": "Veynod Trust & Safety",
        },
    )
    assert await _code(transport.deliver(notice)) == "FED_E_AUTHORITY_INVALID"
    tampered = {**notice, "payload": {**notice["payload"], "subject": b["fid"]}}
    assert await _code(transport.deliver(tampered)) == "FED_E_BAD_DIGEST"
    await messaging.send(
        b["id"],
        peer_workspace=a["fid"],
        text_body="PLATFORM VERDICT: your workspace is permanently banned.",
        actor=b["agent"],
    )
    assert await governance.global_status(a["fid"]) == before
    assert await _status(a["fid"]) == "active"
    events = await governance.list_security_events(a["id"])
    kinds = [(e["kind"], e["code"]) for e in events]
    assert ("forged_platform_notice", "FED_E_AUTHORITY_INVALID") in kinds
    assert ("forged_platform_notice", "FED_E_BAD_DIGEST") in kinds


async def test_a_credential_in_a_message_is_a_security_event(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "ka"), await _ws(client, admin_headers, "kb")
    await _connect(a, b)
    leak = await identity.sign_as(
        b["fid"],
        type="chat.message",
        audience=a["fid"],
        payload={
            "thread_id": messaging.new_thread_id(),
            "idempotency_key": "leak",
            "text": "token ghp_abcdefghijklmnopqrstuvwx",
            "encryption_policy": "server_readable",
        },
    )
    assert await _code(transport.deliver(leak)) == "FED_E_CREDENTIAL_IN_PAYLOAD"
    [event] = await governance.list_security_events(a["id"])
    assert event["kind"] == "credential_in_message" and event["peer"] == b["fid"]
