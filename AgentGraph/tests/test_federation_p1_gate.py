"""Federation P1 gate (task F-06): security and fault injection.

Every test here runs between workspaces in different root tenants, the way two
customers would use Veynod. Each test names the vision §21 case or the attack
it reproduces. F-007, F-009, F-010 and F-011 are covered in depth in
tests/test_federation_messaging.py and tests/test_federation_api.py; the gate
report (docs/federation/P1_GATE_REPORT.md) maps every P1 case to its tests.
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory
from app.services.federation import delegations, grants, identity, messaging
from app.services.federation.canonical import sha256_hex
from app.services.federation.envelope import build_envelope
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import new_connection_id

pytestmark = pytest.mark.integration


def db_dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


_OUT_ENVELOPE = (
    "SELECT envelope FROM federation_messages WHERE message_id = :m AND direction = 'out'"
)


@pytest.fixture
def federation_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_kek", base64.b64encode(os.urandom(32)).decode())
    messaging.buckets.reset()
    yield settings
    messaging.buckets.reset()


class Transport(grants.LocalTransport):
    """The same-deployment transport with a switch for faults."""

    def __init__(self) -> None:
        self.fault: BaseException | None = None
        self.answer = None

    async def deliver(self, envelope):
        if self.fault is not None:
            raise self.fault
        if self.answer is not None:
            await super().deliver(envelope)
            return self.answer
        return await super().deliver(envelope)


@pytest.fixture
def transport():
    wire = Transport()
    grants.set_transport(wire)
    try:
        yield wire
    finally:
        grants.set_transport(grants.LocalTransport())


async def _ws(client, admin_headers, name: str) -> dict:
    response = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={"name": f"{name}-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    workspace_id = body["workspace"]["id"]
    view = await identity.enable_workspace(workspace_id, actor=f"user:owner-{name}")
    return {
        "id": workspace_id,
        "fid": view["federation_id"],
        "subject": view["parent_id"],
        "key": {"Authorization": f"Bearer {body['api_key']}"},
        "owner": Actor("user", f"owner-{name}", is_owner=True),
        "person": Actor("user", f"member-{name}"),
        "agent": Actor("agent", f"bot-{name}"),
    }


async def _connect(a, b, *, a_to_b=("chat",), b_to_a=("chat",)) -> str:
    asked = await grants.request(
        a["id"],
        peer_workspace=b["fid"],
        outbound_scopes=list(a_to_b),
        inbound_scopes=list(b_to_a),
        reason="pilot collaboration",
        actor=a["owner"],
    )
    await grants.approve(b["id"], asked["connection_id"], actor=b["owner"])
    return asked["connection_id"]


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


def _chat(thread: str | None = None, text_body: str = "hello", key: str | None = None) -> dict:
    return {
        "thread_id": thread or messaging.new_thread_id(),
        "idempotency_key": key or uuid.uuid4().hex,
        "text": text_body,
        "encryption_policy": "server_readable",
    }


async def _signed_at(fid: str, *, type: str, audience: str, payload: dict, now: datetime) -> dict:
    """An envelope signed with the identity's real key at a chosen time."""
    async with async_session_factory() as platform:
        key_id, private = await identity._current_key(platform, fid)
    return build_envelope(
        type=type,
        issuer=fid,
        key_id=key_id,
        private=private,
        audience=audience,
        payload=payload,
        now=now,
    )


async def _count(sql: str, **params) -> int:
    async with async_session_factory() as platform:
        return int(await platform.scalar(text(sql), params) or 0)


# ── §21 P1 cases ──────────────────────────────────────────────────────────


async def test_f001_same_agent_name_in_two_root_tenants(client, admin_headers, federation_on):
    a, b = await _ws(client, admin_headers, "g1a"), await _ws(client, admin_headers, "g1b")
    conn = await asyncpg.connect(db_dsn())
    try:
        roots = await conn.fetch(
            "SELECT t.root_tenant_id FROM workspaces w JOIN tenants t ON t.id = w.tenant_id "
            "WHERE w.id = ANY($1::uuid[])",
            [uuid.UUID(a["id"]), uuid.UUID(b["id"])],
        )
    finally:
        await conn.close()
    assert len({row[0] for row in roots}) == 2, "the gate needs two different root tenants"
    assert a["subject"] != b["subject"]

    # The same local agent name in both: two different global identities,
    # each mapped back to its own workspace only.
    first = await identity.register_agent(a["id"], "prn_researcher", actor="user:owner-g1a")
    second = await identity.register_agent(b["id"], "prn_researcher", actor="user:owner-g1b")
    assert first["federation_id"] != second["federation_id"]
    assert first["parent_id"] == a["fid"] and second["parent_id"] == b["fid"]
    assert str((await identity.map_local(first["federation_id"])).workspace_id) == a["id"]
    assert str((await identity.map_local(second["federation_id"])).workspace_id) == b["id"]


async def test_f002_connected_without_delegate_scope(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "g2a"), await _ws(client, admin_headers, "g2b")
    await _connect(a, b, a_to_b=("chat",))
    terms = {
        "objective": "Summarise three public reports.",
        "acceptance_criteria": ["Cites each report"],
        "input_summary": "Three public reports.",
        "data_classification": "public",
        "allowed_use": "Only for this task.",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=2))
        .replace(microsecond=0)
        .isoformat(),
        "budget_cap": None,
        "result_contract": "One Markdown report.",
        "cancellation_policy": "drain",
        "external_side_effects": False,
    }
    assert (
        await _code(
            delegations.offer(a["id"], peer_workspace=b["fid"], terms=terms, actor=a["owner"])
        )
        == "FED_E_SCOPE_DENIED"
    )
    # A forged offer straight to B is refused by B's own fence too.
    forged = await identity.sign_as(
        a["fid"],
        type="delegate.offer",
        audience=b["fid"],
        payload={
            "federation_job_id": delegations.new_job_id(),
            "version": 1,
            "terms": terms,
            "offer_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert await _code(transport.deliver(forged)) == "FED_E_SCOPE_DENIED"
    for ws in (a, b):
        assert (
            await _count(
                "SELECT count(*) FROM federation_delegations WHERE workspace_id = CAST(:w AS uuid)",
                w=ws["id"],
            )
            == 0
        )


async def test_f003_an_agent_cannot_forge_a_persons_approval(
    client, admin_headers, federation_on, transport
):
    a = await _ws(client, admin_headers, "g3a")
    b = await _ws(client, admin_headers, "g3b")
    c = await _ws(client, admin_headers, "g3c")
    asked = await grants.request(
        a["id"],
        peer_workspace=b["fid"],
        outbound_scopes=["chat"],
        inbound_scopes=["chat"],
        reason="pilot collaboration",
        actor=a["owner"],
    )
    cid = asked["connection_id"]
    # Neither an agent nor a person who is not an owner can approve.
    assert await _code(grants.approve(b["id"], cid, actor=b["agent"])) == (
        "FED_E_APPROVAL_NOT_HUMAN"
    )
    assert await _code(grants.approve(b["id"], cid, actor=b["person"])) == (
        "FED_E_APPROVAL_NOT_HUMAN"
    )
    # A third workspace signing "approved" for A's request changes nothing.
    fake = await identity.sign_as(
        c["fid"],
        type="grant.decision",
        audience=a["fid"],
        payload={
            "connection_id": cid,
            "decision": "approved",
            "requester_scopes": ["chat"],
            "recipient_scopes": ["chat"],
            "encryption_policy": "server_readable",
        },
    )
    assert await _code(transport.deliver(fake)) in ("FED_E_GRANT_UNKNOWN", "FED_E_GRANT_TRANSITION")
    states = {row["state"] for row in await grants.list_connections(a["id"])}
    assert states == {"REQUESTED"}
    assert (
        await _code(
            messaging.send(a["id"], peer_workspace=b["fid"], text_body="hi", actor=a["agent"])
        )
        == "FED_E_GRANT_INACTIVE"
    )


async def test_f004_trust_is_not_transitive(client, admin_headers, federation_on, transport):
    a = await _ws(client, admin_headers, "g4a")
    b = await _ws(client, admin_headers, "g4b")
    c = await _ws(client, admin_headers, "g4c")
    await _connect(a, b)
    await _connect(b, c)
    # A trusts B and B trusts C: A still cannot reach C.
    assert (
        await _code(
            messaging.send(a["id"], peer_workspace=c["fid"], text_body="hi C", actor=a["agent"])
        )
        == "FED_E_GRANT_UNKNOWN"
    )
    # B cannot pass A's signed message on to C either: it is addressed to B.
    sent = await messaging.send(
        a["id"], peer_workspace=b["fid"], text_body="for B only", actor=a["agent"]
    )
    async with async_session_factory() as platform:
        envelope = await platform.scalar(
            text(_OUT_ENVELOPE),
            {"m": sent["message_id"]},
        )
    assert await _code(messaging.receive(c["id"], envelope)) == "FED_E_AUDIENCE_MISMATCH"
    assert (
        await _code(messaging.receive(c["id"], {**envelope, "audience": c["fid"]}))
        == "FED_E_BAD_SIGNATURE"
    )
    assert (await messaging.list_messages(c["id"]))["items"] == []


async def test_f005_expired_revoked_frozen_blocked(client, admin_headers, federation_on, transport):
    cases = {}
    for state in ("expired", "revoked", "frozen", "blocked"):
        a = await _ws(client, admin_headers, f"g5a{state[:2]}")
        b = await _ws(client, admin_headers, f"g5b{state[:2]}")
        cid = await _connect(a, b)
        # Signed while the connection was still live: must not count afterwards.
        early = await identity.sign_as(
            a["fid"], type="chat.message", audience=b["fid"], payload=_chat()
        )
        if state == "expired":
            async with async_session_factory() as platform:
                await platform.execute(
                    text(
                        "UPDATE federation_grants SET expires_at = NOW() - INTERVAL '1 minute' "
                        "WHERE connection_id = :c"
                    ),
                    {"c": cid},
                )
                await platform.commit()
        elif state == "revoked":
            await grants.revoke(b["id"], cid, actor=b["owner"])
        elif state == "frozen":
            await grants.freeze(b["id"], cid, actor=b["owner"])
        else:
            await grants.block(b["id"], cid, actor=b["owner"])
        cases[state] = (
            await _code(transport.deliver(early)),
            await _code(
                messaging.send(b["id"], peer_workspace=a["fid"], text_body="x", actor=b["agent"])
            ),
        )
        assert (await messaging.list_messages(b["id"]))["items"] == []
    assert cases["expired"] == ("FED_E_GRANT_EXPIRED", "FED_E_GRANT_EXPIRED")
    assert cases["revoked"] == ("FED_E_GRANT_INACTIVE", "FED_E_GRANT_INACTIVE")
    assert cases["frozen"] == ("FED_E_GRANT_INACTIVE", "FED_E_GRANT_INACTIVE")
    assert cases["blocked"] == ("FED_E_GRANT_INACTIVE", "FED_E_GRANT_INACTIVE")

    # Unfreezing does not revive what was signed before the freeze.
    a = await _ws(client, admin_headers, "g5ua")
    b = await _ws(client, admin_headers, "g5ub")
    cid = await _connect(a, b)
    early = await identity.sign_as(
        a["fid"], type="chat.message", audience=b["fid"], payload=_chat()
    )
    await grants.freeze(b["id"], cid, actor=b["owner"])
    await grants.unfreeze(b["id"], cid, actor=b["owner"])
    assert await _code(transport.deliver(early)) == "FED_E_GRANT_STALE"


async def test_f006_credentials_never_cross(client, admin_headers, federation_on, transport):
    a, b = await _ws(client, admin_headers, "g6a"), await _ws(client, admin_headers, "g6b")
    await _connect(a, b)
    for leak in (
        "token: ghp_abcdefghijklmnopqrstuvwx",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "our key is agk_Q1w2E3r4T5y6",
    ):
        assert (
            await _code(
                messaging.send(a["id"], peer_workspace=b["fid"], text_body=leak, actor=a["agent"])
            )
            == "FED_E_CREDENTIAL_IN_PAYLOAD"
        )
        forged = await identity.sign_as(
            a["fid"], type="chat.message", audience=b["fid"], payload=_chat(text_body=leak)
        )
        assert await _code(transport.deliver(forged)) == "FED_E_CREDENTIAL_IN_PAYLOAD"
    assert (await messaging.list_messages(b["id"]))["items"] == []


async def test_f029_no_silent_downgrade(client, admin_headers, federation_on, transport):
    a, b = await _ws(client, admin_headers, "g9a"), await _ws(client, admin_headers, "g9b")
    await _connect(a, b)
    for declared, expected in (
        ("", "FED_E_POLICY_UNSUPPORTED"),
        ("plaintext", "FED_E_POLICY_UNSUPPORTED"),
        ("e2ee", "FED_E_POLICY_UNSUPPORTED"),
    ):
        payload = {**_chat(), "encryption_policy": declared}
        envelope = await identity.sign_as(
            a["fid"], type="chat.message", audience=b["fid"], payload=payload
        )
        assert await _code(transport.deliver(envelope)) in (expected, "FED_E_POLICY_DOWNGRADE")
    # A connection request that offers no policy this side supports is refused.
    c = await _ws(client, admin_headers, "g9c")
    weak = await identity.sign_as(
        c["fid"],
        type="grant.request",
        audience=a["fid"],
        payload={
            "connection_id": new_connection_id(),
            "requester_scopes": ["chat"],
            "recipient_scopes": ["chat"],
            "reason": "downgrade attempt",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "encryption_policies": ["plaintext"],
        },
    )
    assert await _code(transport.deliver(weak)) == "FED_E_POLICY_UNSUPPORTED"
    assert {row["peer_workspace"] for row in await grants.list_connections(a["id"])} == {b["fid"]}
    assert (await messaging.list_messages(b["id"]))["items"] == []


# ── attacks: forgery, replay, time, size ──────────────────────────────────


async def test_forged_envelopes_are_refused(client, admin_headers, federation_on, transport):
    a = await _ws(client, admin_headers, "fa")
    b = await _ws(client, admin_headers, "fb")
    c = await _ws(client, admin_headers, "fc")
    await _connect(a, b)
    good = await identity.sign_as(a["fid"], type="chat.message", audience=b["fid"], payload=_chat())

    tampered = {**good, "payload": {**good["payload"], "text": "changed in transit"}}
    assert await _code(messaging.receive(b["id"], tampered)) == "FED_E_BAD_DIGEST"
    redigested = {**tampered, "payload_sha256": sha256_hex(tampered["payload"])}
    assert await _code(messaging.receive(b["id"], redigested)) == "FED_E_BAD_SIGNATURE"
    # C signs with its own key but claims to be A.
    by_c = await identity.sign_as(c["fid"], type="chat.message", audience=b["fid"], payload=_chat())
    impostor = {**by_c, "issuer": a["fid"]}
    assert await _code(messaging.receive(b["id"], impostor)) == "FED_E_IDENTITY_UNKNOWN"
    assert await _code(messaging.receive(b["id"], {**good, "extra": 1})) == (
        "FED_E_ENVELOPE_INVALID"
    )
    assert await _code(messaging.receive(b["id"], {**good, "protocol_version": "9.9"})) in (
        "FED_E_PROTOCOL_VERSION",
        "FED_E_ENVELOPE_INVALID",
    )
    assert (await messaging.list_messages(b["id"]))["items"] == []
    # The genuine one still goes through afterwards.
    assert (await transport.deliver(good))["status"] == "received"


async def test_replays_are_refused_or_answered_once(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "pa"), await _ws(client, admin_headers, "pb")
    asked = await grants.request(
        a["id"],
        peer_workspace=b["fid"],
        outbound_scopes=["chat"],
        inbound_scopes=["chat"],
        reason="pilot collaboration",
        actor=a["owner"],
    )
    await grants.approve(b["id"], asked["connection_id"], actor=b["owner"])
    history_before = len(await grants.history(a["id"], asked["connection_id"]))
    async with async_session_factory() as platform:
        logged = await platform.scalar(
            text(
                "SELECT count(*) FROM federation_replay_guard WHERE workspace_id = CAST(:w AS uuid)"
            ),
            {"w": b["id"]},
        )
    assert logged >= 1
    # Replaying a chat envelope gives the first receipt again and no second message.
    sent = await messaging.send(
        a["id"], peer_workspace=b["fid"], text_body="once", actor=a["agent"]
    )
    async with async_session_factory() as platform:
        envelope = await platform.scalar(
            text(_OUT_ENVELOPE),
            {"m": sent["message_id"]},
        )
    for _ in range(5):
        assert (await transport.deliver(envelope))["status"] == "duplicate"
    assert len((await messaging.list_messages(b["id"]))["items"]) == 1
    assert len(await grants.history(a["id"], asked["connection_id"])) == history_before


async def test_envelopes_outside_their_time_window(client, admin_headers, federation_on, transport):
    a, b = await _ws(client, admin_headers, "ta"), await _ws(client, admin_headers, "tb")
    await _connect(a, b)
    now = datetime.now(timezone.utc)
    stale = await _signed_at(
        a["fid"],
        type="chat.message",
        audience=b["fid"],
        payload=_chat(),
        now=now - timedelta(hours=3),
    )
    future = await _signed_at(
        a["fid"],
        type="chat.message",
        audience=b["fid"],
        payload=_chat(),
        now=now + timedelta(hours=1),
    )
    assert await _code(messaging.receive(b["id"], stale)) == "FED_E_EXPIRED"
    assert await _code(messaging.receive(b["id"], future)) == "FED_E_NOT_YET_VALID"
    assert (await messaging.list_messages(b["id"]))["items"] == []


async def test_oversized_messages_are_refused(client, admin_headers, federation_on, transport):
    a, b = await _ws(client, admin_headers, "sa"), await _ws(client, admin_headers, "sb")
    await _connect(a, b)
    assert (
        await _code(
            messaging.send(
                a["id"],
                peer_workspace=b["fid"],
                text_body="x" * (messaging.MAX_TEXT_CHARS + 1),
                actor=a["agent"],
            )
        )
        == "FED_E_TOO_LARGE"
    )
    huge = await identity.sign_as(
        a["fid"], type="chat.message", audience=b["fid"], payload=_chat(text_body="y" * 70_000)
    )
    assert await _code(messaging.receive(b["id"], huge)) == "FED_E_TOO_LARGE"
    deep: dict = {}
    node = deep
    for _ in range(20):  # deeper than the 16 a payload may nest
        node["n"] = {}
        node = node["n"]
    nested = await identity.sign_as(
        a["fid"], type="chat.message", audience=b["fid"], payload={**_chat(), "deep": deep}
    )
    assert await _code(messaging.receive(b["id"], nested)) == "FED_E_ENVELOPE_INVALID"
    assert (await messaging.list_messages(b["id"]))["items"] == []


# ── isolation ─────────────────────────────────────────────────────────────


async def test_tenant_isolation_holds_for_federation_rows(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "ia"), await _ws(client, admin_headers, "ib")
    await _connect(a, b)
    sent = await messaging.send(
        a["id"], peer_workspace=b["fid"], text_body="private", actor=a["agent"]
    )
    # Over the API, B's own key cannot read A's copy, and the other way round.
    other = await client.get(f"/v1/federation/messages/{sent['message_id']}", headers=b["key"])
    assert other.status_code in (404, 403), other.text
    # In the database, the application role scoped to B sees B's rows only.
    conn = await asyncpg.connect(db_dsn())
    try:
        tenant_b = await conn.fetchval(
            "SELECT tenant_id FROM workspaces WHERE id = $1::uuid", b["id"]
        )
        seen = {}
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE agentgraph_app")
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true), "
                "set_config('app.tenant_scope', $2, true)",
                b["id"],
                tenant_b,
            )
            for table in (
                "federation_grants",
                "federation_messages",
                "federation_threads",
                "federation_delivery_attempts",
                "federation_replay_guard",
            ):
                seen[table] = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE workspace_id <> $1::uuid", b["id"]
                )
            own = await conn.fetchval("SELECT count(*) FROM federation_messages")
            identities = await conn.fetchval(
                "SELECT has_table_privilege('agentgraph_app', 'federation_identity_keys', 'SELECT')"
            )
    finally:
        await conn.close()
    assert seen == dict.fromkeys(seen, 0), seen
    assert own == 1
    assert identities is False


async def test_disabled_federation_answers_nothing(client, admin_headers):
    response = await client.post(
        "/v1/admin/workspaces", headers=admin_headers, json={"name": f"off-{uuid.uuid4().hex[:6]}"}
    )
    key = {"Authorization": f"Bearer {response.json()['api_key']}"}
    fake = f"fed:workspace:{'a' * 26}"
    calls = [
        ("GET", "/v1/federation/me", None),
        ("GET", "/v1/federation/connections", None),
        ("GET", "/v1/federation/threads", None),
        ("GET", "/v1/federation/messages", None),
        ("GET", "/v1/federation/delegations", None),
        ("POST", "/v1/federation/messages", {"peer_workspace": fake, "text": "hi"}),
    ]
    for method, path, body in calls:
        answer = await client.request(method, path, headers=key, json=body)
        assert answer.status_code == 404, (path, answer.text)
        assert answer.json()["error"]["code"] == "federation_disabled", path
    assert (
        await _count(
            "SELECT count(*) FROM federation_messages WHERE peer_workspace_fid = :f", f=fake
        )
        == 0
    )


# ── faults ────────────────────────────────────────────────────────────────


async def test_faults_on_the_wire_never_fake_a_delivery(
    client, admin_headers, federation_on, transport
):
    a = await _ws(client, admin_headers, "wa")
    b = await _ws(client, admin_headers, "wb")
    c = await _ws(client, admin_headers, "wc")
    await _connect(a, b)

    # A connection reset is a failed attempt, retried later, never a crash.
    transport.fault = ConnectionResetError("reset by peer")
    queued = await messaging.send(
        a["id"], peer_workspace=b["fid"], text_body="one", actor=a["agent"]
    )
    assert queued["state"] == "accepted_locally"
    assert queued["delivery"]["last_result"] == "retry"
    transport.fault = None

    # An answer that is not a valid receipt from the peer proves nothing.
    transport.answer = {"status": "received"}
    unsigned = await messaging.send(
        a["id"], peer_workspace=b["fid"], text_body="two", actor=a["agent"]
    )
    assert unsigned["state"] == "sent"
    forged_receipt = await identity.sign_as(
        c["fid"],
        type="delivery.receipt",
        audience=a["fid"],
        payload={"message_id": "fmsg_" + "a" * 26, "status": "processed"},
    )
    transport.answer = {"status": "received", "receipt": forged_receipt}
    third = await messaging.send(
        a["id"], peer_workspace=b["fid"], text_body="three", actor=a["agent"]
    )
    assert third["state"] == "sent"
    transport.answer = None

    # A receipt for A's message signed by someone other than B is refused at A.
    by_c = await identity.sign_as(
        c["fid"],
        type="delivery.receipt",
        audience=a["fid"],
        payload={"message_id": third["message_id"], "status": "processed"},
    )
    assert await _code(messaging.receive(a["id"], by_c)) in (
        "FED_E_GRANT_UNKNOWN",
        "FED_E_MESSAGE_UNKNOWN",
    )
    assert (await messaging.get(a["id"], third["message_id"]))["state"] == "sent"


async def test_a_crash_after_queueing_is_recovered(
    client, admin_headers, federation_on, transport, monkeypatch
):
    a, b = await _ws(client, admin_headers, "ka2"), await _ws(client, admin_headers, "kb2")
    await _connect(a, b)
    real = messaging.attempt

    async def crashed(workspace_id, row_id):  # the process dies before the first attempt
        return "accepted_locally"

    monkeypatch.setattr(messaging, "attempt", crashed)
    queued = await messaging.send(
        a["id"], peer_workspace=b["fid"], text_body="survive", actor=a["agent"]
    )
    assert queued["delivery"]["attempts"] == 0
    monkeypatch.setattr(messaging, "attempt", real)
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "UPDATE federation_messages SET created_at = NOW() - INTERVAL '2 minutes' "
                "WHERE message_id = :m AND direction = 'out'"
            ),
            {"m": queued["message_id"]},
        )
        await platform.commit()
    await messaging.deliver_due()
    assert (await messaging.get(a["id"], queued["message_id"]))["state"] == "received"
    assert len((await messaging.list_messages(b["id"]))["items"]) == 1
