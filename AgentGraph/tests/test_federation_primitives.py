"""Federation primitives without a database: ids, canonical JSON, keys,
envelope verification and encryption policy (federation F-02a/b/c,
docs/federation/ICD_I_MSG.md, doc://federation-i-id-identity-icd)."""

from __future__ import annotations

import base64
import pickle
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import get_settings
from app.services.federation import keys, policy
from app.services.federation.canonical import CanonicalError, canonical_bytes
from app.services.federation.envelope import KeyView, build_envelope, verify_envelope
from app.services.federation.errors import FederationError
from app.services.federation.identity import LocalRef, normalize_handle
from app.services.federation.ids import (
    derive_federation_id,
    key_id_for,
    parse_federation_id,
    require_type,
    verify_derivation,
)

# Test vectors (I-ID §3.2): Ed25519 key from a 32-byte seed of one repeated byte.
VECTORS = [
    (
        1,
        "subject",
        "8a88e3dd7409f195fd52db2d3cba5d72ca6709bf1d94121bf3748801b40f6f5c",
        "fed:subject:4ju3htf3hopdcvpnjcsir4h7ni",
    ),
    (
        2,
        "workspace",
        "8139770ea87d175f56a35466c34c7ecccb8d8a91b4ee37a25df60f5b8fc9b394",
        "fed:workspace:52yh54vixddf3y6uvrybxsdmlu",
    ),
    (
        3,
        "agent",
        "ed4928c628d1c2c6eae90338905995612959273a5c63f93636c14614ac8737d1",
        "fed:agent:dns3fz7lxc247bqtrwgrycz454",
    ),
]

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
ALICE_WS = "fed:workspace:aaaaaaaaaaaaaaaaaaaaaaaaaa"
BOB_WS = "fed:workspace:bbbbbbbbbbbbbbbbbbbbbbbbbb"


def _raw(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


@pytest.mark.parametrize(("seed", "id_type", "public_hex", "expected"), VECTORS)
def test_id_derivation_vectors(seed, id_type, public_hex, expected):
    private = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    assert _raw(private).hex() == public_hex
    assert derive_federation_id(id_type, bytes.fromhex(public_hex)) == expected
    assert verify_derivation(expected, bytes.fromhex(public_hex))
    # ID-INV-7: a different key does not match the id
    assert not verify_derivation(expected, bytes.fromhex(VECTORS[seed % 3][2]))


def test_id_format_and_layers():
    assert parse_federation_id("fed:agent:dns3fz7lxc247bqtrwgrycz454") == (
        "agent",
        "dns3fz7lxc247bqtrwgrycz454",
    )
    for bad in (
        "fed:agent:DNS3FZ7LXC247BQTRWGRYCZ454",
        "fed:team:dns3fz7lxc247bqtrwgrycz454",
        "fed:agent:dns3",
        "prn_abc",
        None,
        7,
    ):
        with pytest.raises(FederationError) as err:
            parse_federation_id(bad)
        assert err.value.code == "FED_E_IDENTITY_INVALID"
    with pytest.raises(FederationError) as err:
        require_type("fed:agent:dns3fz7lxc247bqtrwgrycz454", "workspace")
    assert err.value.code == "FED_E_TYPE_MISMATCH"
    # the same key gives different ids in different layers
    raw = bytes.fromhex(VECTORS[0][2])
    assert (
        len({derive_federation_id(t, raw).split(":")[2] for t in ("subject", "workspace", "agent")})
        == 3
    )


def test_canonical_json():
    assert canonical_bytes({"b": 1, "a": [True, None, "é"], "A": {"y": 2, "x": 1}}) == (
        '{"A":{"x":1,"y":2},"a":[true,null,"é"],"b":1}'.encode()
    )
    for bad in ({"x": 1.5}, {"x": float("nan")}, {1: "x"}, {"x": 2**60}, {"x": b"bytes"}):
        with pytest.raises(CanonicalError):
            canonical_bytes(bad)
    deep: dict = {}
    cursor = deep
    for _ in range(40):
        cursor["n"] = {}
        cursor = cursor["n"]
    with pytest.raises(CanonicalError):
        canonical_bytes(deep)


@pytest.fixture
def kek(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "federation_kek", base64.b64encode(b"k" * 32).decode())
    monkeypatch.setattr(settings, "federation_kek_id", "kek-test")
    return settings


def test_key_sealing(kek, monkeypatch):
    pair = keys.generate()
    sealed, kek_id = keys.seal(pair)
    assert kek_id == "kek-test" and pair.public_b64 not in sealed
    assert _raw(keys.unseal(sealed, pair.key_id)) == pair.public_raw
    assert repr(pair) == f"KeyPair(key_id={pair.key_id!r})"
    # bound to its key id
    with pytest.raises(FederationError):
        keys.unseal(sealed, key_id_for(b"x" * 32))
    # no usable KEK, no hosted keys
    monkeypatch.setattr(kek, "federation_kek", "")
    with pytest.raises(FederationError) as err:
        keys.seal(pair)
    assert err.value.code == "FED_E_KEK_MISSING"


class _Directory:
    """In-memory KeyLookup and ReplayCheck for the verifier."""

    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], KeyView] = {}
        self.seen: set[str] = set()

    def add(
        self, issuer: str, pair: keys.KeyPair, *, state="active", not_after=None, status="active"
    ):
        self.keys[(issuer, pair.key_id)] = KeyView(pair.public_raw, state, not_after, status)

    async def lookup(self, issuer: str, key_id: str):
        return self.keys.get((issuer, key_id))

    async def replay(self, digest: str, _expires) -> bool:
        if digest in self.seen:
            return False
        self.seen.add(digest)
        return True


def _envelope(pair, **overrides):
    args = {
        "type": "chat.message",
        "issuer": ALICE_WS,
        "key_id": pair.key_id,
        "private": pair.private,
        "audience": BOB_WS,
        "payload": {"text": "hello"},
        "now": NOW,
    }
    args.update(overrides)
    return build_envelope(**args)


async def _verify(directory, envelope, *, now=NOW, audience=BOB_WS):
    return await verify_envelope(
        envelope,
        local_workspace_fid=audience,
        key_lookup=directory.lookup,
        replay_check=directory.replay,
        now=now,
        skew_seconds=120,
    )


async def _code(directory, envelope, **kwargs) -> str:
    with pytest.raises(FederationError) as err:
        await _verify(directory, envelope, **kwargs)
    return err.value.code


async def test_envelope_round_trip_and_replay():
    directory, pair = _Directory(), keys.generate()
    directory.add(ALICE_WS, pair)
    envelope = _envelope(pair)
    verified = await _verify(directory, envelope)
    assert verified.issuer == ALICE_WS and verified.payload == {"text": "hello"}
    # the same signed object is accepted once (F-007 / ICD_I_MSG §3 step 7)
    assert await _code(directory, envelope) == "FED_E_REPLAY"


async def test_envelope_rejections():
    directory, pair = _Directory(), keys.generate()
    directory.add(ALICE_WS, pair)
    # ID-INV-12: addressed to someone else, or not addressed at all
    assert await _code(directory, _envelope(pair), audience=ALICE_WS) == "FED_E_AUDIENCE_MISMATCH"
    no_audience = _envelope(pair)
    no_audience["audience"] = None
    assert await _code(directory, no_audience) == "FED_E_AUDIENCE_MISMATCH"
    # time window with 120 s skew
    assert (
        await _code(directory, _envelope(pair), now=NOW + timedelta(hours=1, seconds=121))
        == "FED_E_EXPIRED"
    )
    assert (
        await _code(directory, _envelope(pair), now=NOW - timedelta(seconds=121))
        == "FED_E_NOT_YET_VALID"
    )
    await _verify(directory, _envelope(pair), now=NOW - timedelta(seconds=60))
    # payload and signature tampering
    tampered = _envelope(pair)
    tampered["payload"] = {"text": "HELLO"}
    assert await _code(directory, tampered) == "FED_E_BAD_DIGEST"
    resigned = _envelope(pair)
    resigned["object_id"] = "fmsg_forged"
    assert await _code(directory, resigned) == "FED_E_BAD_SIGNATURE"
    # unknown issuer or key
    other = keys.generate()
    assert await _code(directory, _envelope(other)) == "FED_E_IDENTITY_UNKNOWN"
    # structure
    extra = _envelope(pair)
    extra["admin"] = True
    assert await _code(directory, extra) == "FED_E_ENVELOPE_INVALID"
    floaty = _envelope(pair, payload={"amount": 1})
    floaty["payload"] = {"amount": 1.5}
    assert await _code(directory, floaty) == "FED_E_ENVELOPE_INVALID"
    big = _envelope(pair, payload={"text": "x" * 70_000})
    assert await _code(directory, big) == "FED_E_TOO_LARGE"
    old = _envelope(pair)
    old["protocol_version"] = "0.0"
    assert await _code(directory, old) == "FED_E_PROTOCOL_VERSION"


async def test_key_and_identity_states():
    directory = _Directory()
    retiring, revoked, frozen, gone = (keys.generate() for _ in range(4))
    directory.add(ALICE_WS, retiring, state="retiring", not_after=NOW + timedelta(days=7))
    directory.add(ALICE_WS, revoked, state="revoked")
    directory.add("fed:workspace:cccccccccccccccccccccccccc", frozen, status="frozen")
    directory.add("fed:workspace:dddddddddddddddddddddddddd", gone, status="tombstoned")
    # a retiring key verifies inside its overlap window only (I-ID §5.2)
    await _verify(directory, _envelope(retiring))
    late = _envelope(retiring, now=NOW + timedelta(days=8))
    assert await _code(directory, late, now=NOW + timedelta(days=8)) == "FED_E_KEY_NOT_CURRENT"
    assert await _code(directory, _envelope(revoked)) == "FED_E_KEY_REVOKED"
    assert await _code(
        directory, _envelope(frozen, issuer="fed:workspace:cccccccccccccccccccccccccc")
    ) == ("FED_E_IDENTITY_FROZEN")
    assert await _code(
        directory, _envelope(gone, issuer="fed:workspace:dddddddddddddddddddddddddd")
    ) == ("FED_E_IDENTITY_TOMBSTONED")


async def test_public_identity_objects_need_no_audience():
    directory, pair = _Directory(), keys.generate()
    directory.add(ALICE_WS, pair)
    record = _envelope(
        pair, type="identity.record", audience=None, payload={"federation_id": ALICE_WS}
    )
    assert (await _verify(directory, record)).type == "identity.record"
    with pytest.raises(FederationError):
        _envelope(pair, type="grant.request", audience=None)


def test_encryption_policy_never_downgrades():
    assert policy.negotiate(["server_readable"]) == "server_readable"
    assert policy.negotiate(["server_readable", "e2ee"]) == "server_readable"
    with pytest.raises(FederationError) as err:
        policy.negotiate(["e2ee"])
    assert err.value.code == "FED_E_POLICY_UNSUPPORTED"
    # a connection recorded at a stronger policy refuses a weaker one (F-029)
    with pytest.raises(FederationError) as err:
        policy.negotiate(["server_readable"], supported=("server_readable", "e2ee"), current="e2ee")
    assert err.value.code == "FED_E_POLICY_DOWNGRADE"
    assert (
        policy.negotiate(
            ["server_readable", "e2ee"],
            supported=("server_readable", "e2ee"),
            current="server_readable",
        )
        == "e2ee"
    )
    with pytest.raises(FederationError) as err:
        policy.check_message("server_readable", "e2ee")
    assert err.value.code == "FED_E_POLICY_DOWNGRADE"
    policy.check_message("server_readable", "server_readable")


def test_local_ref_cannot_be_exported():
    ref = LocalRef("ten_x", "00000000-0000-0000-0000-000000000000", "prn_x")
    assert "ten_x" not in repr(ref) and "prn_x" not in repr(ref)
    with pytest.raises(TypeError):
        pickle.dumps(ref)


def test_handles_are_normalised():
    assert normalize_handle("Ｒesearcher") == "researcher"
    for bad in ("", "-x", "a b", "a/b", "x" * 65, "研究员"):
        with pytest.raises(FederationError):
            normalize_handle(bad)
