"""Agent Poker Arena (P6 R2) and delivery obligations (R3). NO ECONOMIC VALUE.

Contract: docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md §5-§7.

* Two workspaces queue with the same simulated stake (DEMO_M from MOCK_DEMO
  capacity only) and are matched heads-up. Workspaces that count as one
  controller (integrity.related) are never matched.
* Match: MATCHED -> OWNER_AUTHORIZED -> DEMO_RESERVED -> COMMITTED -> PLAYING
  -> RESULT_VERIFIED -> DEMO_SETTLED -> CLOSED, with ABORTED, TIMED_OUT,
  DISPUTED and RECONCILING on the side. Each owner authorizes the same
  contract hash; that holds the stake. The host commits to its seed before
  the seats commit; the seats reveal; the decks come from all three seeds
  (arena_engine). The host's seed is revealed with the result, so anyone can
  replay the match from the action log.
* Settlement is decoupled and once only: the loser's stake becomes a DEMO_M
  voucher for the winner (a bounded right to delegate work to the loser
  through the existing bridge), the winner's stake goes back. A draw or an
  abort returns both. Nothing is created: the ledger balances.
* The voucher comes with a delivery obligation on the loser. Facts (a
  refused, expired or late delegation) put it in review; only an operator
  confirms a default, and only a confirmed default restricts the workspace -
  from starting new arena matches, nothing else. Delivery, equivalent
  compensation or a resolution lifts the restriction automatically. A report
  filed against a workspace never restricts it by itself.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.services.federation import arena_engine as engine
from app.services.federation import rights
from app.services.federation.canonical import sha256_hex
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import require_type

NOTICE = (
    "Simulated arena: no economic value. ARENA_CHIP exists only inside a match; the stake "
    "and any winnings are DEMO_M, which cannot be bought, withdrawn, exchanged or used to "
    "pay a real model"
)
MIN_STAKE = 100_000  # 0.1 DEMO_M
# sng: one result, the winner takes the loser's whole stake.
# cash: chips are tokens (stake / starting stack per chip); a seat may stand up
# between hands; each side settles by its final stack. No rebuys, so a seat
# never loses more than its buy-in.
MODES = ("sng", "cash")
MAX_STAKE = 10 * rights.TOKENS_PER_M
SETUP_STATES = ("MATCHED", "OWNER_AUTHORIZED", "DEMO_RESERVED", "COMMITTED")
FINAL_STATES = ("CLOSED", "ABORTED", "TIMED_OUT")
OBLIGATION_FINAL = ("FULFILLED", "REMEDIATED", "RESOLVED")
OBLIGATION_LIVE = ("PENDING", "DUE", "DELIVERING")
# Delegation outcomes that are the provider's (the obligor's) failure to serve.
FAULT_OUTCOMES = ("REJECTED", "EXPIRED", "FAILED", "STOP_NOT_CONFIRMED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return rights.enabled() and bool(get_settings().federation_arena_enabled)


def require_enabled() -> None:
    if not enabled():
        raise FederationError("FED_E_ARENA_DISABLED", "the arena is off on this deployment")


def _person(actor: Actor, what: str, *, owner: bool = False) -> None:
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", f"a person {what}")
    if owner and not actor.is_owner:
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", f"an owner {what}")


def _rules() -> dict[str, Any]:
    return dict(engine.RULES_V1)


def tokens_per_chip(row: dict[str, Any]) -> int | None:
    if row["mode"] != "cash":
        return None
    return int(row["stake"]) // int(row["rules"]["starting_stack"])


def outcome(row: dict[str, Any], game: dict[str, Any]) -> tuple[int | None, int]:
    """(winner seat, tokens the loser pays). sng: the whole stake; cash: what
    the loser's stack lost, in tokens. None/0: nothing changes hands."""
    if row["mode"] == "cash":
        start = int(row["rules"]["starting_stack"])
        stacks = game["stacks"]
        if stacks[0] == stacks[1]:
            return None, 0
        winner = 0 if stacks[0] > stacks[1] else 1
        return winner, (start - stacks[1 - winner]) * tokens_per_chip(row)
    if game["winner"] is None:
        return None, 0
    return game["winner"], int(row["stake"])


def _contract(row: dict[str, Any]) -> dict[str, Any]:
    """What both owners authorize, hashed: fixed for the whole match."""
    return {
        "v": 1,
        "match_id": row["match_id"],
        "seats": [row["seat0_fid"], row["seat1_fid"]],
        "specs": [row["seat0_spec"], row["seat1_spec"]],
        "model_class": row["model_class"],
        "mode": row["mode"],
        "tokens_per_chip": tokens_per_chip(row),
        "stake": int(row["stake"]),
        "unit": rights.UNIT,
        "rules_hash": row["rules_hash"],
        "server_commit": row["server_commit"],
        "turn_timeout_policy": {
            "turn_seconds": row["rules"]["turn_seconds"],
            "on_timeout": "check if possible, else fold",
            "forfeit_after": row["rules"]["max_timeouts"],
        },
        "max_hands": row["rules"]["max_hands"],
        "setup_deadline": row["setup_deadline"].isoformat(),
        "settlement": (
            "each side's final stack times tokens_per_chip; what one side lost becomes a "
            "DEMO_M voucher for the other; no rebuys"
            if row["mode"] == "cash"
            else "loser's stake becomes a DEMO_M voucher for the winner; draw returns both"
        ),
        "economic_value": "none",
    }


async def _match(session: AsyncSession, match_id: str, *, lock: bool = False) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM federation_arena_matches WHERE match_id = :m"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"m": match_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_MATCH_UNKNOWN", "no such match")
    out = dict(row)
    for key in ("rules", "seats", "game", "result"):
        if isinstance(out.get(key), str):
            out[key] = json.loads(out[key])
    return out


def _seat_of(row: dict[str, Any], fid: str) -> int:
    if fid == row["seat0_fid"]:
        return 0
    if fid == row["seat1_fid"]:
        return 1
    raise FederationError("FED_E_MATCH_UNKNOWN", "no such match")


async def _history(
    session: AsyncSession, match_id: str, frm: str | None, to: str, actor: str, note=None
) -> None:
    await session.execute(
        text(
            "INSERT INTO federation_arena_history (match_id, from_state, to_state, actor, note) "
            "VALUES (:m, :f, :t, :a, :n)"
        ),
        {"m": match_id, "f": frm, "t": to, "a": actor, "n": note},
    )


async def _save(session: AsyncSession, row: dict[str, Any], **changes: Any) -> None:
    row.update(changes)
    await session.execute(
        text(
            "UPDATE federation_arena_matches SET state = :state, seats = CAST(:seats AS jsonb), "
            "game = CAST(:game AS jsonb), action_seq = :seq, turn_deadline = :td, "
            "result = CAST(:result AS jsonb), result_proof_hash = :proof, version = version + 1, "
            "updated_at = NOW() WHERE match_id = :m"
        ),
        {
            "state": row["state"],
            "seats": json.dumps(row["seats"]),
            "game": json.dumps(row["game"]) if row["game"] is not None else None,
            "seq": row["action_seq"],
            "td": row["turn_deadline"],
            "result": json.dumps(row["result"]) if row["result"] is not None else None,
            "proof": row["result_proof_hash"],
            "m": row["match_id"],
        },
    )


async def _move(session: AsyncSession, row: dict[str, Any], to: str, actor: str, note=None):
    frm = row["state"]
    row["state"] = to
    await _history(session, row["match_id"], frm, to, actor, note)


# ── restriction ───────────────────────────────────────────────────────────


async def active_restriction(session: AsyncSession, fid: str) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM federation_arena_restrictions WHERE fid = :f AND "
                    "state = 'ACTIVE' ORDER BY created_at LIMIT 1"
                ),
                {"f": fid},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def _require_unrestricted(session: AsyncSession, fid: str) -> None:
    found = await active_restriction(session, fid)
    if found is not None:
        raise FederationError(
            "FED_E_ARENA_RESTRICTED",
            "a confirmed undelivered arena obligation: deliver, compensate or resolve it to "
            "play again (other federation features are not affected)",
            obligation_id=found["obligation_id"],
        )


# ── queue and matching ────────────────────────────────────────────────────


async def join_queue(
    workspace_id: str,
    *,
    spec_id: str,
    stake: int,
    mode: str = "sng",
    unit: str | None = None,
    actor: Actor,
) -> dict[str, Any]:
    """A person puts this workspace in the queue; a compatible waiting
    workspace (same stake and rules, not the same controller) is matched."""
    require_enabled()
    _person(actor, "joins the arena queue")
    rights.check_unit(unit)
    if not isinstance(stake, int) or isinstance(stake, bool) or not MIN_STAKE <= stake <= MAX_STAKE:
        raise FederationError(
            "FED_E_PACKAGE_INVALID", f"stake is an integer of {MIN_STAKE} to {MAX_STAKE} tokens"
        )
    from app.services.federation import integrity

    rules = _rules()
    rules_hash = engine.rules_hash(rules)
    if mode not in MODES:
        raise FederationError("FED_E_PACKAGE_INVALID", "mode is sng or cash")
    if mode == "cash" and stake % rules["starting_stack"]:
        raise FederationError(
            "FED_E_PACKAGE_INVALID",
            f"a cash table needs a stake divisible by {rules['starting_stack']} chips",
        )
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        await _require_unrestricted(session, me)
        spec = await rights.spec_row(session, spec_id)
        if spec["provider_fid"] != me:
            raise FederationError("FED_E_RESOURCE_UNKNOWN", "stake from a spec of yours")
        rights.require_escrowable(spec)
        left = await rights.balance(session, rights.cap(spec_id))
        if left < stake:
            raise FederationError("FED_E_CAPACITY_EXCEEDED", "not enough capacity", left=left)
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('arena:queue'))"))
        if await session.scalar(
            text("SELECT 1 FROM federation_arena_queue WHERE fid = :f AND state = 'WAITING'"),
            {"f": me},
        ):
            raise FederationError("FED_E_MATCH_STATE", "this workspace is already waiting")
        candidates = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_arena_queue WHERE state = 'WAITING' AND "
                        "stake = :s AND rules_hash = :h AND model_class = :c AND mode = :md "
                        "AND fid <> :f ORDER BY created_at LIMIT 50"
                    ),
                    # Same model class only: like-for-like stakes, no conversion.
                    {
                        "s": stake,
                        "h": rules_hash,
                        "c": spec["model_family"],
                        "md": mode,
                        "f": me,
                    },
                )
            )
            .mappings()
            .all()
        )
        partner = None
        for c in candidates:
            if await integrity.related(me, c["fid"]):
                continue  # one controller never plays itself
            if await active_restriction(session, c["fid"]):
                continue
            partner = dict(c)
            break
        entry_id = rights.new_id("faq")
        await session.execute(
            text(
                "INSERT INTO federation_arena_queue (entry_id, fid, spec_id, model_class, mode, "
                "stake, rules_hash, state, created_by) VALUES (:e, :f, :s, :c, :md, :k, :h, :st, "
                ":by)"
            ),
            {
                "e": entry_id,
                "f": me,
                "s": spec_id,
                "c": spec["model_family"],
                "md": mode,
                "k": stake,
                "h": rules_hash,
                "st": "MATCHED" if partner else "WAITING",
                "by": actor.ref,
            },
        )
        match_id = None
        if partner is not None:
            match_id = await _create_match(
                session,
                seats=[(partner["fid"], partner["spec_id"]), (me, spec_id)],
                model_class=spec["model_family"],
                mode=mode,
                stake=stake,
                rules=rules,
                actor=actor.ref,
            )
            await session.execute(
                text(
                    "UPDATE federation_arena_queue SET state = 'MATCHED', match_id = :m "
                    "WHERE entry_id IN (:a, :b)"
                ),
                {"m": match_id, "a": partner["entry_id"], "b": entry_id},
            )
        await session.commit()
    return {
        "entry_id": entry_id,
        "state": "MATCHED" if match_id else "WAITING",
        "match": await get_match(workspace_id, match_id) if match_id else None,
        "notice": NOTICE,
    }


async def leave_queue(workspace_id: str, *, actor: Actor) -> dict[str, Any]:
    require_enabled()
    _person(actor, "leaves the arena queue")
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        left = await session.scalar(
            text(
                "UPDATE federation_arena_queue SET state = 'LEFT' WHERE fid = :f AND "
                "state = 'WAITING' RETURNING entry_id"
            ),
            {"f": me},
        )
        await session.commit()
    return {"left": left is not None}


async def _create_match(
    session: AsyncSession,
    *,
    seats: list[tuple[str, str]],
    model_class: str,
    mode: str,
    stake: int,
    rules: dict[str, Any],
    actor: str,
) -> str:
    match_id = rights.new_id("fam")
    server_seed = secrets.token_hex(32)
    setup_deadline = _now() + timedelta(minutes=get_settings().federation_arena_setup_minutes)
    row = {
        "match_id": match_id,
        "seat0_fid": seats[0][0],
        "seat1_fid": seats[1][0],
        "seat0_spec": seats[0][1],
        "seat1_spec": seats[1][1],
        "model_class": model_class,
        "mode": mode,
        "stake": stake,
        "rules": rules,
        "rules_hash": engine.rules_hash(rules),
        "server_commit": engine.seed_commitment(server_seed),
        "setup_deadline": setup_deadline,
    }
    await session.execute(
        text(
            "INSERT INTO federation_arena_matches (match_id, seat0_fid, seat1_fid, seat0_spec, "
            "seat1_spec, model_class, mode, stake, rules, rules_hash, contract_hash, state, "
            "seats, server_seed, server_commit, setup_deadline) VALUES (:m, :a, :b, :sa, :sb, "
            ":mc, :md, :k, "
            "CAST(:r AS jsonb), :rh, :ch, 'MATCHED', CAST(:seats AS jsonb), :ss, :sc, :sd)"
        ),
        {
            "m": match_id,
            "a": row["seat0_fid"],
            "b": row["seat1_fid"],
            "sa": row["seat0_spec"],
            "sb": row["seat1_spec"],
            "mc": model_class,
            "md": mode,
            "k": stake,
            "r": json.dumps(rules),
            "rh": row["rules_hash"],
            "ch": sha256_hex(_contract(row)),
            "seats": json.dumps([{}, {}]),
            "ss": server_seed,
            "sc": row["server_commit"],
            "sd": setup_deadline,
        },
    )
    await _history(session, match_id, None, "MATCHED", actor)
    return match_id


# ── views ─────────────────────────────────────────────────────────────────


async def _view(session: AsyncSession, row: dict[str, Any], seat: int | None) -> dict[str, Any]:
    history = (
        (
            await session.execute(
                text(
                    "SELECT from_state, to_state, actor, note, created_at FROM "
                    "federation_arena_history WHERE match_id = :m ORDER BY id"
                ),
                {"m": row["match_id"]},
            )
        )
        .mappings()
        .all()
    )
    seats = []
    for i, fid in enumerate((row["seat0_fid"], row["seat1_fid"])):
        s = row["seats"][i]
        seats.append(
            {
                "fid": fid,
                "authorized": bool(s.get("authorized_by")),
                "agent_play": bool(s.get("agent_play")),
                "commitment": s.get("commit"),
                "revealed": bool(s.get("seed")),
            }
        )
    game = None
    if row["game"] is not None:
        game = engine.seat_view(row["game"], seat)
    revealed = row["state"] in ("RESULT_VERIFIED", "DISPUTED", "DEMO_SETTLED", "CLOSED") or (
        row["state"] == "RECONCILING" and row["result"] is not None
    )
    return {
        "match_id": row["match_id"],
        "state": row["state"],
        "version": row["version"],
        "my_seat": seat,
        "seats": seats,
        "model_class": row["model_class"],
        "mode": row["mode"],
        "tokens_per_chip": tokens_per_chip(row),
        "stake": int(row["stake"]),
        "stake_m": rights.as_m(row["stake"]),
        "unit": row["unit"],
        "rules": row["rules"],
        "rules_hash": row["rules_hash"],
        "contract_hash": row["contract_hash"],
        "server_commit": row["server_commit"],
        "server_seed": row["server_seed"] if revealed else None,
        "action_seq": row["action_seq"],
        "turn_deadline": row["turn_deadline"].isoformat() if row["turn_deadline"] else None,
        "setup_deadline": row["setup_deadline"].isoformat(),
        "game": game,
        "result": row["result"],
        "result_proof_hash": row["result_proof_hash"],
        "history": [
            {
                "from": h["from_state"],
                "to": h["to_state"],
                "actor": h["actor"],
                "note": h["note"],
                "at": h["created_at"].isoformat(),
            }
            for h in history
        ],
        "simulated": True,
        "economic_value": "none",
        "notice": NOTICE,
    }


async def get_match(workspace_id: str, match_id: str) -> dict[str, Any]:
    """A seat's view: its own cards only. Others get 404 (RR-05)."""
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id)
        seat = _seat_of(row, me)
        return await _view(session, row, seat)


async def list_matches(workspace_id: str) -> list[dict[str, Any]]:
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        ids = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT match_id FROM federation_arena_matches WHERE seat0_fid = :m OR "
                        "seat1_fid = :m ORDER BY created_at DESC LIMIT 50"
                    ),
                    {"m": me},
                )
            ).all()
        ]
    return [await get_match(workspace_id, m) for m in ids]


# ── setup: authorize, commit, reveal ──────────────────────────────────────


def _may_play(row: dict[str, Any], seat: int, actor: Actor) -> None:
    if actor.kind == "user":
        return
    if not row["seats"][seat].get("agent_play"):
        raise FederationError(
            "FED_E_APPROVAL_NOT_HUMAN", "the owner has not allowed agents to play this match"
        )


async def authorize(
    workspace_id: str,
    match_id: str,
    *,
    contract_hash: str,
    agent_play: bool = False,
    actor: Actor,
) -> dict[str, Any]:
    """The owner authorizes the match contract (by its hash) and the stake is
    held from its simulated capacity in the same transaction."""
    require_enabled()
    _person(actor, "authorizes a match", owner=True)
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        seat = _seat_of(row, me)
        if row["state"] != "MATCHED":
            raise FederationError(
                "FED_E_MATCH_STATE", "the match is past authorization", state=row["state"]
            )
        if contract_hash != row["contract_hash"]:
            raise FederationError(
                "FED_E_MATCH_STATE", "authorize the contract as shown: its hash differs"
            )
        if row["seats"][seat].get("authorized_by"):
            raise FederationError("FED_E_MATCH_STATE", "already authorized")
        await _require_unrestricted(session, me)
        spec_id = row[f"seat{seat}_spec"]
        spec = await rights.spec_row(session, spec_id)
        rights.require_escrowable(spec)
        await rights.lock(session, rights.cap(spec_id))
        left = await rights.balance(session, rights.cap(spec_id))
        stake = int(row["stake"])
        if left < stake:
            raise FederationError("FED_E_CAPACITY_EXCEEDED", "not enough capacity", left=left)
        await rights.post(
            session,
            kind="stake_hold",
            entries=[(rights.cap(spec_id), -stake), (rights.stake(match_id, seat), stake)],
            reference=match_id,
            idempotency_key=f"hold:{match_id}:{seat}",
        )
        row["seats"][seat].update(authorized_by=actor.ref, agent_play=bool(agent_play))
        if all(s.get("authorized_by") for s in row["seats"]):
            await _move(session, row, "OWNER_AUTHORIZED", actor.ref, "both owners authorized")
            await _move(session, row, "DEMO_RESERVED", actor.ref, "both stakes held")
        await _save(session, row)
        await session.commit()
    return await get_match(workspace_id, match_id)


async def commit_seed(
    workspace_id: str, match_id: str, *, commitment: str, actor: Actor
) -> dict[str, Any]:
    """A seat publishes sha256(seed) before anyone reveals."""
    require_enabled()
    if not isinstance(commitment, str) or len(commitment) != 64:
        raise FederationError("FED_E_PACKAGE_INVALID", "a commitment is a sha256 hex digest")
    try:
        bytes.fromhex(commitment)
    except ValueError as exc:
        raise FederationError("FED_E_PACKAGE_INVALID", "a commitment is hex") from exc
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        seat = _seat_of(row, me)
        _may_play(row, seat, actor)
        if row["state"] != "DEMO_RESERVED":
            raise FederationError(
                "FED_E_MATCH_STATE", "commit after both stakes are held", state=row["state"]
            )
        if row["seats"][seat].get("commit"):
            if row["seats"][seat]["commit"] == commitment.lower():
                return await _view(session, row, seat)
            raise FederationError("FED_E_MATCH_STATE", "a commitment cannot be changed")
        row["seats"][seat]["commit"] = commitment.lower()
        if all(s.get("commit") for s in row["seats"]):
            await _move(session, row, "COMMITTED", actor.ref, "both seats committed")
        await _save(session, row)
        await session.commit()
    return await get_match(workspace_id, match_id)


async def reveal_seed(workspace_id: str, match_id: str, *, seed: str, actor: Actor):
    """A seat reveals its seed; it must match its commitment. When both have
    revealed, the decks are fixed and the first hand is dealt."""
    require_enabled()
    if not isinstance(seed, str) or len(seed) != 64:
        raise FederationError("FED_E_PACKAGE_INVALID", "a seed is 32 bytes of hex")
    try:
        bytes.fromhex(seed)
    except ValueError as exc:
        raise FederationError("FED_E_PACKAGE_INVALID", "a seed is hex") from exc
    seed = seed.lower()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        seat = _seat_of(row, me)
        _may_play(row, seat, actor)
        if row["state"] != "COMMITTED":
            raise FederationError(
                "FED_E_MATCH_STATE", "reveal after both seats committed", state=row["state"]
            )
        if engine.seed_commitment(seed) != row["seats"][seat]["commit"]:
            raise FederationError(
                "FED_E_ARENA_ILLEGAL", "the seed does not match this seat's commitment"
            )
        row["seats"][seat]["seed"] = seed
        if all(s.get("seed") for s in row["seats"]):
            combined = engine.combined_seed(row["server_seed"], [s["seed"] for s in row["seats"]])
            row["game"] = engine.new_match(row["rules"], combined)
            await _move(session, row, "PLAYING", actor.ref, "both seeds revealed; hand 1 dealt")
            row["turn_deadline"] = _deadline(row)
            if row["game"]["finished"]:
                await _finish(session, row, actor.ref)
        await _save(session, row)
        await session.commit()
    return await get_match(workspace_id, match_id)


def _deadline(row: dict[str, Any]) -> datetime | None:
    if row["game"] is None or row["game"]["finished"]:
        return None
    return _now() + timedelta(seconds=int(row["rules"]["turn_seconds"]))


def _seed(row: dict[str, Any]) -> str:
    return engine.combined_seed(row["server_seed"], [s["seed"] for s in row["seats"]])


# ── play ──────────────────────────────────────────────────────────────────


async def act(
    workspace_id: str, match_id: str, *, seq: int, action: dict[str, Any], actor: Actor
) -> dict[str, Any]:
    """One action. ``seq`` is the action number the client saw: a resend of
    the same action is answered again (replayed) and never applied twice; a
    different action for an old number is stale."""
    require_enabled()
    if not isinstance(action, dict) or set(action) - {"type", "amount"}:
        raise FederationError("FED_E_ARENA_ILLEGAL", "an action is {type, amount?}")
    if action.get("type") in ("timeout", "concede", "stand_up"):
        raise FederationError("FED_E_ARENA_ILLEGAL", "that action is not played by hand")
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        seat = _seat_of(row, me)
        _may_play(row, seat, actor)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise FederationError("FED_E_ARENA_ILLEGAL", "seq is the action number")
        if seq < row["action_seq"]:
            prior = (
                (
                    await session.execute(
                        text(
                            "SELECT seat, action FROM federation_arena_actions WHERE "
                            "match_id = :m AND seq = :s"
                        ),
                        {"m": match_id, "s": seq},
                    )
                )
                .mappings()
                .first()
            )
            prior_action = prior and (
                json.loads(prior["action"]) if isinstance(prior["action"], str) else prior["action"]
            )
            if prior and prior["seat"] == seat and prior_action == action:
                view = await _view(session, row, seat)
                return {**view, "replayed": True}
            raise FederationError(
                "FED_E_ARENA_STALE", "that action number is taken", next_seq=row["action_seq"]
            )
        if seq != row["action_seq"]:
            raise FederationError(
                "FED_E_ARENA_STALE",
                "act on the current action number",
                next_seq=row["action_seq"],
            )
        if row["state"] != "PLAYING":
            raise FederationError(
                "FED_E_MATCH_STATE", "the match is not in play", state=row["state"]
            )
        try:
            row["game"] = engine.apply(row["rules"], _seed(row), row["game"], seat, action)
        except engine.IllegalAction as exc:
            raise FederationError("FED_E_ARENA_ILLEGAL", str(exc)) from exc
        await _log_action(session, row, seat, action, actor.ref)
        if row["game"]["finished"]:
            await _finish(session, row, actor.ref)
        else:
            row["turn_deadline"] = _deadline(row)
        await _save(session, row)
        await session.commit()
    return {**(await get_match(workspace_id, match_id)), "replayed": False}


async def _log_action(
    session: AsyncSession, row: dict[str, Any], seat: int, action: dict[str, Any], actor: str
) -> None:
    await session.execute(
        text(
            "INSERT INTO federation_arena_actions (match_id, seq, seat, action, actor) VALUES "
            "(:m, :s, :seat, CAST(:a AS jsonb), :by)"
        ),
        {
            "m": row["match_id"],
            "s": row["action_seq"],
            "seat": seat,
            "a": json.dumps(action),
            "by": actor,
        },
    )
    row["action_seq"] += 1


async def _actions(session: AsyncSession, match_id: str) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT seq, seat, action FROM federation_arena_actions WHERE match_id = :m "
                    "ORDER BY seq"
                ),
                {"m": match_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "seq": r["seq"],
            "seat": r["seat"],
            "action": json.loads(r["action"]) if isinstance(r["action"], str) else r["action"],
        }
        for r in rows
    ]


def _proof_payload(row: dict[str, Any], log: list[dict[str, Any]]) -> dict[str, Any]:
    game = row["game"]
    winner, paid = outcome(row, game)
    fids = [row["seat0_fid"], row["seat1_fid"]]
    return {
        "v": 1,
        "match_id": row["match_id"],
        "contract_hash": row["contract_hash"],
        "rules_hash": row["rules_hash"],
        "seats": fids,
        "model_class": row["model_class"],
        "mode": row["mode"],
        "tokens_per_chip": tokens_per_chip(row),
        "stake": int(row["stake"]),
        "unit": rights.UNIT,
        "server_commit": row["server_commit"],
        "server_seed": row["server_seed"],
        "seat_commits": [s["commit"] for s in row["seats"]],
        "seat_seeds": [s["seed"] for s in row["seats"]],
        "action_count": len(log),
        "actions_sha256": sha256_hex([{"seat": e["seat"], "action": e["action"]} for e in log]),
        "final_state_sha256": sha256_hex(game),
        "winner_seat": winner,
        "winner": fids[winner] if winner is not None else None,
        "settles_tokens": paid,
        "reason": game["reason"],
        "final_stacks": game["stacks"],
        "hands_played": len(game["hands"]),
        "economic_value": "none",
    }


async def _finish(session: AsyncSession, row: dict[str, Any], actor: str) -> None:
    """The game is over: build the proof, reveal the host seed, sign."""
    from app.services.federation import identity

    log = await _actions(session, row["match_id"])
    payload = _proof_payload(row, log)
    signatures = []
    for fid in (row["seat0_fid"], row["seat1_fid"]):
        try:
            signatures.append(
                await identity.sign_as(
                    fid,
                    type="arena.result",
                    audience=None,
                    payload=payload,
                    ttl=timedelta(hours=24),
                )
            )
        except FederationError as exc:  # a frozen identity cannot sign; say so
            signatures.append({"issuer": fid, "unsigned": exc.code})
    row["result"] = {"proof": payload, "signatures": signatures}
    row["result_proof_hash"] = sha256_hex(payload)
    row["turn_deadline"] = None
    await _move(
        session,
        row,
        "RESULT_VERIFIED",
        actor,
        f"{payload['reason']}: winner {payload['winner'] or 'none (draw)'}",
    )


async def leave(workspace_id: str, match_id: str, *, actor: Actor) -> dict[str, Any]:
    """Before play: abort and return both stakes (a person). In play: an sng
    seat concedes; a cash seat stands up and the table ends after the hand in
    progress, each side keeping its chips (a person, or an allowed agent)."""
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        seat = _seat_of(row, me)
        if row["state"] in SETUP_STATES:
            _person(actor, "leaves a match")
            await _return_stakes(session, row, "ABORTED", actor.ref, f"seat {seat} left")
        elif row["state"] == "PLAYING" and row["mode"] == "cash":
            _may_play(row, seat, actor)
            if row["game"].get("standing") is None:
                row["game"] = engine.stand_up(row["game"], seat)
                await _log_action(session, row, seat, {"type": "stand_up"}, actor.ref)
        elif row["state"] == "PLAYING":
            _may_play(row, seat, actor)
            row["game"] = engine.concede(row["game"], seat)
            await _log_action(session, row, seat, {"type": "concede"}, actor.ref)
            await _finish(session, row, actor.ref)
        else:
            raise FederationError("FED_E_MATCH_STATE", "nothing to leave", state=row["state"])
        await _save(session, row)
        await session.commit()
    return await get_match(workspace_id, match_id)


async def _return_stakes(
    session: AsyncSession, row: dict[str, Any], to: str, actor: str, note: str
) -> str | None:
    """Both held stakes go back to their capacity (abort, timeout, draw, void)."""
    txn = None
    for seat in (0, 1):
        account = rights.stake(row["match_id"], seat)
        await rights.lock(session, account)
        held = await rights.balance(session, account)
        if held:
            txn, _ = await rights.post(
                session,
                kind="stake_return",
                entries=[(account, -held), (rights.cap(row[f"seat{seat}_spec"]), held)],
                reference=row["match_id"],
                idempotency_key=f"return:{row['match_id']}:{seat}",
            )
    row["turn_deadline"] = None
    await _move(session, row, to, actor, note)
    return txn


# ── disputes ──────────────────────────────────────────────────────────────


async def dispute(workspace_id: str, match_id: str, *, reason: str, actor: Actor):
    """A seat's person holds the settlement for an operator to look at."""
    require_enabled()
    _person(actor, "disputes a result")
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
        raise FederationError("FED_E_PACKAGE_INVALID", "a reason is 5 to 2000 characters")
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        _seat_of(row, me)
        if row["state"] != "RESULT_VERIFIED":
            raise FederationError(
                "FED_E_MATCH_STATE", "only an unsettled result is disputed", state=row["state"]
            )
        await _move(session, row, "DISPUTED", actor.ref, reason.strip())
        await _save(session, row)
        await session.commit()
    return await get_match(workspace_id, match_id)


async def resolve(match_id: str, *, outcome: str, note: str, operator: str) -> dict[str, Any]:
    """Operator: uphold the verified result (it settles) or void the match
    (both stakes return). Also clears RECONCILING."""
    require_enabled()
    if outcome not in ("uphold", "void"):
        raise FederationError("FED_E_PACKAGE_INVALID", "outcome is uphold or void")
    async with async_session_factory() as session:
        row = await _match(session, match_id, lock=True)
        if row["state"] not in ("DISPUTED", "RECONCILING"):
            raise FederationError("FED_E_MATCH_STATE", "nothing to resolve", state=row["state"])
        actor = f"operator:{operator}"
        if outcome == "void" or row["result"] is None:
            await _return_stakes(session, row, "ABORTED", actor, f"voided: {note}")
        else:
            await _move(session, row, "RESULT_VERIFIED", actor, f"upheld: {note}")
            await _save(session, row)
            await _settle(session, row, actor)
        await _save(session, row)
        await session.commit()
        return await _view(session, row, None)


# ── verification ──────────────────────────────────────────────────────────


async def verify(match_id: str) -> dict[str, Any]:
    """Replay the match from the revealed seeds and the action log; check the
    commitments, the proof hash and both signatures."""
    require_enabled()
    from app.services.federation import identity, keys
    from app.services.federation.envelope import signing_bytes
    from app.services.federation.ids import b64url_decode

    async with async_session_factory() as session:
        row = await _match(session, match_id)
        log = await _actions(session, match_id)
    if row["result"] is None:
        raise FederationError("FED_E_MATCH_STATE", "no result yet", state=row["state"])
    proof = row["result"]["proof"]
    checks: dict[str, bool] = {}
    checks["server_commit"] = engine.seed_commitment(proof["server_seed"]) == proof["server_commit"]
    checks["seat_commits"] = all(
        engine.seed_commitment(s) == c
        for s, c in zip(proof["seat_seeds"], proof["seat_commits"], strict=True)
    )
    seed = engine.combined_seed(proof["server_seed"], proof["seat_seeds"])
    replayed = engine.replay(
        row["rules"], seed, [{"seat": e["seat"], "action": e["action"]} for e in log]
    )
    checks["replay_matches"] = sha256_hex(replayed) == proof["final_state_sha256"]
    checks["rules_hash"] = engine.rules_hash(row["rules"]) == proof["rules_hash"]
    checks["actions"] = (
        sha256_hex([{"seat": e["seat"], "action": e["action"]} for e in log])
        == proof["actions_sha256"]
    )
    checks["proof_hash"] = sha256_hex(proof) == row["result_proof_hash"]
    signed = []
    for envelope in row["result"]["signatures"]:
        good = False
        if "signature" in envelope and envelope.get("payload") == proof:
            key = await identity.key_lookup(envelope["issuer"], envelope.get("key_id", ""))
            if key is not None and key.state != "revoked":
                try:
                    good = keys.verify(
                        key.public_raw,
                        b64url_decode(envelope["signature"]),
                        signing_bytes(envelope),
                    )
                except (KeyError, ValueError):
                    good = False
        signed.append({"issuer": envelope.get("issuer"), "valid": good})
    checks["signatures"] = all(s["valid"] for s in signed)
    winner = replayed["winner"]
    return {
        "match_id": match_id,
        "valid": all(checks.values()),
        "checks": checks,
        "signatures": signed,
        "winner_seat": winner,
        "reason": replayed["reason"],
        "hands": len(replayed["hands"]),
        "hand_history": replayed["hands"],
    }


# ── settlement ────────────────────────────────────────────────────────────


async def _settle(session: AsyncSession, row: dict[str, Any], actor: str) -> None:
    """Once per match: award or return the stakes, open the obligation."""
    if await session.scalar(
        text("SELECT 1 FROM federation_arena_settlements WHERE match_id = :m"),
        {"m": row["match_id"]},
    ):
        return
    stake = int(row["stake"])
    held = []
    for seat in (0, 1):
        await rights.lock(session, rights.stake(row["match_id"], seat))
        held.append(await rights.balance(session, rights.stake(row["match_id"], seat)))
    if held != [stake, stake]:
        await _move(
            session,
            row,
            "RECONCILING",
            actor,
            f"stakes held {held}, expected {stake} each: nothing moved",
        )
        return
    winner = row["result"]["proof"]["winner_seat"]
    paid = int(row["result"]["proof"].get("settles_tokens", stake))
    if winner is None or paid == 0:
        txn = await _return_stakes(
            session, row, "DEMO_SETTLED", actor, "nothing changes hands: both stakes back"
        )
        await session.execute(
            text(
                "INSERT INTO federation_arena_settlements (match_id, kind, txn_id) VALUES "
                "(:m, 'return', :t)"
            ),
            {"m": row["match_id"], "t": txn},
        )
        await _move(session, row, "CLOSED", actor)
        return
    loser = 1 - winner
    fids = [row["seat0_fid"], row["seat1_fid"]]
    loser_spec = await rights.spec_row(session, row[f"seat{loser}_spec"])
    voucher_id = await rights.issue(
        session,
        spec=loser_spec,
        beneficiary=fids[winner],
        amount=paid,
        origin="arena",
        origin_ref=row["match_id"],
        usage_scope=(
            f"Arena match {row['match_id']}: delegate work to {fids[loser]} through the "
            f"federation bridge on model class {row['model_class']} only, up to this amount "
            "of simulated capacity"
        ),
        expires_at=_now() + timedelta(days=get_settings().federation_arena_voucher_days),
        source_account=rights.stake(row["match_id"], loser),
        idempotency_key=f"award:{row['match_id']}",
    )
    txn, _ = await rights.post(
        session,
        kind="stake_return",
        entries=[
            (rights.stake(row["match_id"], winner), -stake),
            (rights.cap(row[f"seat{winner}_spec"]), stake),
        ],
        reference=row["match_id"],
        idempotency_key=f"return:{row['match_id']}:{winner}",
    )
    if stake - paid:  # cash tables: the loser keeps what its stack did not lose
        await rights.post(
            session,
            kind="stake_return",
            entries=[
                (rights.stake(row["match_id"], loser), -(stake - paid)),
                (rights.cap(row[f"seat{loser}_spec"]), stake - paid),
            ],
            reference=row["match_id"],
            idempotency_key=f"return:{row['match_id']}:{loser}",
        )
    await session.execute(
        text(
            "INSERT INTO federation_arena_settlements (match_id, kind, txn_id, voucher_id) "
            "VALUES (:m, 'award', :t, :v)"
        ),
        {"m": row["match_id"], "t": txn, "v": voucher_id},
    )
    await _move(session, row, "DEMO_SETTLED", actor, f"voucher {voucher_id} for {fids[winner]}")
    obligation_id = rights.new_id("fob")
    expires = await session.scalar(
        text("SELECT expires_at FROM federation_rr_vouchers WHERE voucher_id = :v"),
        {"v": voucher_id},
    )
    await session.execute(
        text(
            "INSERT INTO federation_arena_obligations (obligation_id, match_id, obligor_fid, "
            "obligee_fid, voucher_id, amount, state, due_at) VALUES (:o, :m, :a, :b, :v, :k, "
            "'PENDING', :d)"
        ),
        {
            "o": obligation_id,
            "m": row["match_id"],
            "a": fids[loser],
            "b": fids[winner],
            "v": voucher_id,
            "k": paid,
            "d": expires,
        },
    )
    await _ob_history(session, obligation_id, None, "PENDING", actor, f"match {row['match_id']}")
    await _move(session, row, "CLOSED", actor)


async def settle_due(limit: int = 100) -> int:
    window = timedelta(minutes=get_settings().federation_arena_dispute_minutes)
    async with async_session_factory() as session:
        ids = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT match_id FROM federation_arena_matches WHERE state = "
                        "'RESULT_VERIFIED' AND updated_at <= :t ORDER BY updated_at LIMIT :l"
                    ),
                    {"t": _now() - window, "l": limit},
                )
            ).all()
        ]
    done = 0
    for match_id in ids:
        async with async_session_factory() as session:
            row = await _match(session, match_id, lock=True)
            if row["state"] != "RESULT_VERIFIED":
                continue
            await _settle(session, row, "system:settlement")
            await _save(session, row)
            await session.commit()
            done += 1
    return done


async def tick(limit: int = 100) -> int:
    """Turn timeouts (check or fold, then forfeit) and setup deadlines."""
    now = _now()
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT match_id FROM federation_arena_matches WHERE (state = 'PLAYING' "
                        "AND turn_deadline <= :n) OR (state IN ('MATCHED', 'OWNER_AUTHORIZED', "
                        "'DEMO_RESERVED', 'COMMITTED') AND setup_deadline <= :n) LIMIT :l"
                    ),
                    {"n": now, "l": limit},
                )
            )
            .scalars()
            .all()
        )
    done = 0
    for match_id in rows:
        async with async_session_factory() as session:
            row = await _match(session, match_id, lock=True)
            if row["state"] == "PLAYING" and row["turn_deadline"] and row["turn_deadline"] <= now:
                seat = row["game"]["hand"]["to_act"]
                row["game"] = engine.apply(
                    row["rules"], _seed(row), row["game"], seat, {"type": "timeout"}
                )
                await _log_action(session, row, seat, {"type": "timeout"}, "system:timeout")
                if row["game"]["finished"]:
                    await _finish(session, row, "system:timeout")
                else:
                    row["turn_deadline"] = _deadline(row)
            elif row["state"] in SETUP_STATES and row["setup_deadline"] <= now:
                missing = [
                    i
                    for i, s in enumerate(row["seats"])
                    if not (
                        s.get("seed")
                        if row["state"] == "COMMITTED"
                        else s.get("commit")
                        if row["state"] == "DEMO_RESERVED"
                        else s.get("authorized_by")
                    )
                ]
                await _return_stakes(
                    session,
                    row,
                    "TIMED_OUT",
                    "system:timeout",
                    f"setup not finished in time ({row['state']}); waiting on seats {missing}",
                )
            else:
                continue
            await _save(session, row)
            await session.commit()
            done += 1
    return done


# ── obligations (R3) ──────────────────────────────────────────────────────


async def _ob_history(session, obligation_id, frm, to, actor, note=None) -> None:
    await session.execute(
        text(
            "INSERT INTO federation_arena_obligation_history (obligation_id, from_state, "
            "to_state, actor, note) VALUES (:o, :f, :t, :a, :n)"
        ),
        {"o": obligation_id, "f": frm, "t": to, "a": actor, "n": note},
    )


async def _ob(session: AsyncSession, obligation_id: str, *, lock: bool = False) -> dict:
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM federation_arena_obligations WHERE obligation_id = :o"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"o": obligation_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_OBLIGATION_UNKNOWN", "no such obligation")
    out = dict(row)
    if isinstance(out["facts"], str):
        out["facts"] = json.loads(out["facts"])
    return out


async def _ob_move(session, ob: dict[str, Any], to: str, actor: str, note=None) -> None:
    await _ob_history(session, ob["obligation_id"], ob["state"], to, actor, note)
    ob["state"] = to
    await session.execute(
        text(
            "UPDATE federation_arena_obligations SET state = :s, facts = CAST(:f AS jsonb), "
            "due_at = :d, version = version + 1, updated_at = NOW() WHERE obligation_id = :o"
        ),
        {"s": to, "f": json.dumps(ob["facts"]), "d": ob["due_at"], "o": ob["obligation_id"]},
    )


async def _lift(session: AsyncSession, ob: dict[str, Any], reason: str) -> None:
    await session.execute(
        text(
            "UPDATE federation_arena_restrictions SET state = 'LIFTED', lifted_at = NOW(), "
            "lifted_reason = :r WHERE obligation_id = :o AND state = 'ACTIVE'"
        ),
        {"r": reason, "o": ob["obligation_id"]},
    )


async def _reservations(session: AsyncSession, voucher_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in (
            await session.execute(
                text(
                    "SELECT * FROM federation_rr_reservations WHERE voucher_id = :v "
                    "ORDER BY created_at"
                ),
                {"v": voucher_id},
            )
        )
        .mappings()
        .all()
    ]


async def _job_state(session: AsyncSession, obligee_fid: str, job_id: str) -> str | None:
    from app.services.federation import delegations

    ws = await rights._beneficiary_workspace(session, obligee_fid)
    if ws is None:
        return None
    try:
        return delegations._state(await delegations._row(session, ws, job_id))
    except FederationError:
        return None


async def obligations_advance(limit: int = 200) -> int:
    """Move obligations along from facts only: what the voucher's delegations
    did. A fault opens a review; nothing here restricts anyone."""
    days = timedelta(days=get_settings().federation_arena_obligation_days)
    async with async_session_factory() as session:
        ids = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT obligation_id FROM federation_arena_obligations WHERE state IN "
                        "('PENDING', 'DUE', 'DELIVERING', 'RESTRICTED') ORDER BY updated_at "
                        "LIMIT :l"
                    ),
                    {"l": limit},
                )
            ).all()
        ]
    moved = 0
    for obligation_id in ids:
        async with async_session_factory() as session:
            ob = await _ob(session, obligation_id, lock=True)
            voucher = await session.scalar(
                text("SELECT status FROM federation_rr_vouchers WHERE voucher_id = :v"),
                {"v": ob["voucher_id"]},
            )
            res = await _reservations(session, ob["voucher_id"])
            known = {f["reservation_id"] for f in ob["facts"]}
            before = ob["state"]
            now = _now()
            if ob["state"] == "RESTRICTED":
                restricted_at = await session.scalar(
                    text(
                        "SELECT created_at FROM federation_arena_restrictions WHERE "
                        "obligation_id = :o"
                    ),
                    {"o": obligation_id},
                )
                delivered = [
                    r
                    for r in res
                    if r["delegation_outcome"] == "ACCEPTED_RESULT"
                    and r["settled_at"]
                    and restricted_at
                    and r["settled_at"] > restricted_at
                ]
                if delivered or voucher == "EXHAUSTED":
                    await _lift(session, ob, "delivered after the restriction")
                    await _ob_move(
                        session,
                        ob,
                        "REMEDIATED",
                        "system:obligations",
                        "delivered: restriction lifted",
                    )
                elif voucher in ("WAIVED", "EXPIRED"):
                    await _lift(session, ob, f"voucher {voucher.lower()}")
                    await _ob_move(
                        session,
                        ob,
                        "RESOLVED",
                        "system:obligations",
                        f"voucher {voucher.lower()}: restriction lifted",
                    )
            else:
                faults = []
                for r in res:
                    if r["reservation_id"] in known:
                        continue
                    if r["state"] != "RESERVED" and r["delegation_outcome"] in FAULT_OUTCOMES:
                        faults.append(
                            {
                                "reservation_id": r["reservation_id"],
                                "federation_job_id": r["federation_job_id"],
                                "fact": f"delegation {r['delegation_outcome']}",
                                "amount": int(r["amount"]),
                                "excused": False,
                            }
                        )
                    elif r["state"] == "RESERVED" and r["created_at"] + days <= now:
                        faults.append(
                            {
                                "reservation_id": r["reservation_id"],
                                "federation_job_id": r["federation_job_id"],
                                "fact": f"not delivered within {days.days} days",
                                "amount": int(r["amount"]),
                                "excused": False,
                            }
                        )
                open_res = [r for r in res if r["state"] == "RESERVED"]
                if open_res:
                    ob["due_at"] = min(r["created_at"] + days for r in open_res)
                if faults:
                    ob["facts"] = ob["facts"] + faults
                    await _ob_move(
                        session,
                        ob,
                        "OVERDUE_REVIEW",
                        "system:obligations",
                        "; ".join(f["fact"] for f in faults) + ": waiting for review, "
                        "no restriction yet",
                    )
                elif voucher == "EXHAUSTED":
                    await _ob_move(session, ob, "FULFILLED", "system:obligations")
                elif voucher in ("EXPIRED", "WAIVED"):
                    await _ob_move(
                        session,
                        ob,
                        "RESOLVED",
                        "system:obligations",
                        f"voucher {voucher.lower()} unused: not the obligor's fault",
                    )
                elif open_res:
                    states = [
                        await _job_state(session, ob["obligee_fid"], r["federation_job_id"])
                        for r in open_res
                    ]
                    target = "DUE" if all(s == "OFFERED" for s in states) else "DELIVERING"
                    if target != ob["state"]:
                        await _ob_move(session, ob, target, "system:obligations")
                elif ob["state"] != "PENDING":
                    await _ob_move(session, ob, "PENDING", "system:obligations")
            if ob["state"] != before:
                moved += 1
            await session.commit()
    return moved


def _ob_out(row: dict[str, Any], history: list, restriction: dict | None) -> dict[str, Any]:
    return {
        "obligation_id": row["obligation_id"],
        "match_id": row["match_id"],
        "obligor": row["obligor_fid"],
        "obligee": row["obligee_fid"],
        "voucher_id": row["voucher_id"],
        "amount": int(row["amount"]),
        "unit": rights.UNIT,
        "state": row["state"],
        "due_at": row["due_at"].isoformat(),
        "facts": row["facts"],
        "obligor_statement": row["obligor_statement"],
        "restriction": {
            "restriction_id": restriction["restriction_id"],
            "state": restriction["state"],
            "scope": "new arena matches only",
            "reason": restriction["reason"],
            "decided_by": restriction["decided_by"],
            "lifted_reason": restriction["lifted_reason"],
        }
        if restriction
        else None,
        "history": [
            {
                "from": h["from_state"],
                "to": h["to_state"],
                "actor": h["actor"],
                "note": h["note"],
                "at": h["created_at"].isoformat(),
            }
            for h in history
        ],
        "legal_debt": False,
        "notice": "simulated obligation: not a legal debt, no money involved",
    }


async def get_obligation(obligation_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await _ob(session, obligation_id)
        history = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_arena_obligation_history WHERE "
                        "obligation_id = :o ORDER BY id"
                    ),
                    {"o": obligation_id},
                )
            )
            .mappings()
            .all()
        )
        restriction = (
            (
                await session.execute(
                    text("SELECT * FROM federation_arena_restrictions WHERE obligation_id = :o"),
                    {"o": obligation_id},
                )
            )
            .mappings()
            .first()
        )
    return _ob_out(row, list(history), dict(restriction) if restriction else None)


async def get_obligation_for(workspace_id: str, obligation_id: str) -> dict[str, Any]:
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _ob(session, obligation_id)
    if me not in (row["obligor_fid"], row["obligee_fid"]):
        raise FederationError("FED_E_OBLIGATION_UNKNOWN", "no such obligation")
    return await get_obligation(obligation_id)


async def list_obligations(workspace_id: str) -> dict[str, Any]:
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        ids = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT obligation_id FROM federation_arena_obligations WHERE "
                        "obligor_fid = :m OR obligee_fid = :m ORDER BY created_at DESC LIMIT 100"
                    ),
                    {"m": me},
                )
            ).all()
        ]
        restriction = await active_restriction(session, me)
    views = [await get_obligation(i) for i in ids]
    return {
        "owed_by_me": [v for v in views if v["obligor"] == me],
        "owed_to_me": [v for v in views if v["obligee"] == me],
        "restricted": restriction is not None,
        "restriction_scope": "new arena matches only" if restriction else None,
    }


async def state_case(
    workspace_id: str, obligation_id: str, *, statement: str, actor: Actor
) -> dict[str, Any]:
    """The obligor answers the facts (an appeal) before or after a decision."""
    require_enabled()
    _person(actor, "answers for an obligation")
    if not isinstance(statement, str) or not 5 <= len(statement.strip()) <= 2000:
        raise FederationError("FED_E_PACKAGE_INVALID", "a statement is 5 to 2000 characters")
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        ob = await _ob(session, obligation_id, lock=True)
        if ob["obligor_fid"] != me:
            raise FederationError("FED_E_OBLIGATION_UNKNOWN", "no such obligation of yours")
        if ob["state"] not in ("OVERDUE_REVIEW", "RESTRICTED"):
            raise FederationError("FED_E_OBLIGATION_STATE", "nothing to answer", state=ob["state"])
        await session.execute(
            text(
                "UPDATE federation_arena_obligations SET obligor_statement = :s, updated_at = "
                "NOW() WHERE obligation_id = :o"
            ),
            {"s": statement.strip(), "o": obligation_id},
        )
        await _ob_history(
            session, obligation_id, ob["state"], ob["state"], actor.ref, "obligor statement filed"
        )
        await session.commit()
    return await get_obligation(obligation_id)


async def decide(obligation_id: str, *, decision: str, note: str, operator: str) -> dict[str, Any]:
    """Operator review. confirm: a default, the arena restriction starts.
    excuse: the facts are not the obligor's fault (an outage), back to
    PENDING. resolve: the case is closed for the obligor; any restriction
    is lifted."""
    require_enabled()
    if decision not in ("confirm", "excuse", "resolve"):
        raise FederationError("FED_E_PACKAGE_INVALID", "decision is confirm, excuse or resolve")
    if not isinstance(note, str) or len(note.strip()) < 5:
        raise FederationError("FED_E_PACKAGE_INVALID", "a decision needs a note")
    actor = f"operator:{operator}"
    async with async_session_factory() as session:
        ob = await _ob(session, obligation_id, lock=True)
        if decision == "confirm":
            if ob["state"] != "OVERDUE_REVIEW":
                raise FederationError(
                    "FED_E_OBLIGATION_STATE", "confirm a reviewed fault", state=ob["state"]
                )
            await _ob_move(session, ob, "CONFIRMED_UNFULFILLED", actor, note.strip())
            await session.execute(
                text(
                    "INSERT INTO federation_arena_restrictions (restriction_id, fid, "
                    "obligation_id, reason, decided_by) VALUES (:r, :f, :o, :why, :by)"
                ),
                {
                    "r": rights.new_id("far"),
                    "f": ob["obligor_fid"],
                    "o": obligation_id,
                    "why": note.strip()[:2000],
                    "by": actor,
                },
            )
            await _ob_move(session, ob, "RESTRICTED", actor, "new arena matches blocked")
        elif decision == "excuse":
            if ob["state"] != "OVERDUE_REVIEW":
                raise FederationError(
                    "FED_E_OBLIGATION_STATE", "excuse a reviewed fault", state=ob["state"]
                )
            for f in ob["facts"]:
                f["excused"] = True
            await _ob_move(session, ob, "PENDING", actor, f"excused: {note.strip()}")
        else:
            if ob["state"] not in ("OVERDUE_REVIEW", "RESTRICTED", "CONFIRMED_UNFULFILLED"):
                raise FederationError(
                    "FED_E_OBLIGATION_STATE", "nothing to resolve", state=ob["state"]
                )
            await _lift(session, ob, f"resolved: {note.strip()}")
            await _ob_move(session, ob, "RESOLVED", actor, note.strip())
        await session.commit()
    return await get_obligation(obligation_id)


async def compensate(
    workspace_id: str, obligation_id: str, *, spec_id: str, actor: Actor
) -> dict[str, Any]:
    """The obligor's owner makes good the faulted amount with a fresh DEMO_M
    voucher from its own simulated capacity: REMEDIATED, restriction lifted."""
    require_enabled()
    _person(actor, "compensates", owner=True)
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        ob = await _ob(session, obligation_id, lock=True)
        if ob["obligor_fid"] != me:
            raise FederationError("FED_E_OBLIGATION_UNKNOWN", "no such obligation of yours")
        if ob["state"] not in ("OVERDUE_REVIEW", "RESTRICTED"):
            raise FederationError(
                "FED_E_OBLIGATION_STATE", "nothing to compensate", state=ob["state"]
            )
        amount = min(
            sum(f["amount"] for f in ob["facts"] if not f.get("excused")), int(ob["amount"])
        ) or int(ob["amount"])
        spec = await rights.spec_row(session, spec_id)
        if spec["provider_fid"] != me:
            raise FederationError("FED_E_RESOURCE_UNKNOWN", "compensate from a spec of yours")
        rights.require_escrowable(spec)
        owed_class = await session.scalar(
            text(
                "SELECT s.model_family FROM federation_rr_vouchers v JOIN federation_rr_specs s "
                "ON s.spec_id = v.spec_id WHERE v.voucher_id = :v"
            ),
            {"v": ob["voucher_id"]},
        )
        if spec["model_family"] != owed_class:
            raise FederationError(
                "FED_E_MODEL_CLASS_MISMATCH",
                "compensate in the model class that is owed",
                needed=owed_class,
                got=spec["model_family"],
            )
        await rights.lock(session, rights.cap(spec_id))
        left = await rights.balance(session, rights.cap(spec_id))
        if left < amount:
            raise FederationError("FED_E_CAPACITY_EXCEEDED", "not enough capacity", left=left)
        voucher_id = await rights.issue(
            session,
            spec=spec,
            beneficiary=ob["obligee_fid"],
            amount=amount,
            origin="compensate",
            origin_ref=obligation_id,
            usage_scope=f"Compensation for arena obligation {obligation_id}",
            expires_at=_now() + timedelta(days=get_settings().federation_arena_voucher_days),
            source_account=rights.cap(spec_id),
            idempotency_key=f"compensate:{obligation_id}",
        )
        await _lift(session, ob, f"compensated with voucher {voucher_id}")
        await _ob_move(
            session,
            ob,
            "REMEDIATED",
            actor.ref,
            f"compensated {amount} tokens with voucher {voucher_id}",
        )
        await session.commit()
    return await get_obligation(obligation_id)


async def credit(fid: str) -> dict[str, Any]:
    """Several separate counts, never one score (vision §12.1)."""
    require_enabled()
    require_type(fid, "workspace")
    async with async_session_factory() as session:
        counts = {
            r["state"]: int(r["n"])
            for r in (
                await session.execute(
                    text(
                        "SELECT state, count(*) AS n FROM federation_arena_obligations WHERE "
                        "obligor_fid = :f GROUP BY state"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .all()
        }
        confirmed = int(
            await session.scalar(
                text("SELECT count(*) FROM federation_arena_restrictions WHERE fid = :f"),
                {"f": fid},
            )
            or 0
        )
        matches = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM federation_arena_matches WHERE (seat0_fid = :f OR "
                    "seat1_fid = :f) AND state = 'CLOSED'"
                ),
                {"f": fid},
            )
            or 0
        )
        disputes = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM federation_arena_history h JOIN "
                    "federation_arena_matches m ON m.match_id = h.match_id WHERE "
                    "h.to_state = 'DISPUTED' AND (m.seat0_fid = :f OR m.seat1_fid = :f)"
                ),
                {"f": fid},
            )
            or 0
        )
        restriction = await active_restriction(session, fid)
    return {
        "fid": fid,
        "matches_closed": matches,
        "obligations": {
            "fulfilled": counts.get("FULFILLED", 0),
            "open": sum(counts.get(s, 0) for s in OBLIGATION_LIVE),
            "in_review": counts.get("OVERDUE_REVIEW", 0),
            "restricted_now": counts.get("RESTRICTED", 0),
            "remediated": counts.get("REMEDIATED", 0),
            "resolved": counts.get("RESOLVED", 0),
        },
        "confirmed_defaults_ever": confirmed,
        "disputes": disputes,
        "restricted": restriction is not None,
        "score": None,  # deliberately no single number
    }


async def integrity_flags(window_days: int = 7, threshold: int = 3) -> list[dict[str, Any]]:
    """Operator view: pairs that keep playing each other, or one-sided runs.
    Flags are for review only; nothing is restricted automatically (RR-11)."""
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT LEAST(seat0_fid, seat1_fid) AS a, GREATEST(seat0_fid, seat1_fid) "
                        "AS b, count(*) AS n, array_agg(result->'proof'->>'winner') AS winners "
                        "FROM federation_arena_matches WHERE state = 'CLOSED' AND updated_at > "
                        ":t GROUP BY 1, 2 HAVING count(*) >= :k"
                    ),
                    {"t": _now() - timedelta(days=window_days), "k": threshold},
                )
            )
            .mappings()
            .all()
        )
    from app.services.federation import integrity

    out = []
    for r in rows:
        winners = [w for w in r["winners"] if w]
        one_sided = len(set(winners)) == 1 and len(winners) >= threshold
        out.append(
            {
                "pair": [r["a"], r["b"]],
                "matches": int(r["n"]),
                "one_sided": one_sided,
                "same_controller": await integrity.related(r["a"], r["b"]),
                "action": "review",
                "automatic_sanction": None,
            }
        )
    return out


async def admin_matches(state: str | None = None) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        q = "SELECT match_id FROM federation_arena_matches"
        params: dict[str, Any] = {}
        if state:
            q += " WHERE state = :s"
            params["s"] = state
        ids = [
            r[0]
            for r in (
                await session.execute(text(q + " ORDER BY updated_at DESC LIMIT 200"), params)
            ).all()
        ]
        out = []
        for m in ids:
            out.append(await _view(session, await _match(session, m), None))
    return out


async def advance() -> int:
    if not enabled():
        return 0
    return await tick() + await settle_due() + await obligations_advance()
