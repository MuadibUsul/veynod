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


TABLE_SIZES = (2, 6)
# Tournament prize shares by number of players (percent, by place).
PAYOUTS: dict[int, tuple[int, ...]] = {
    2: (100,),
    3: (70, 30),
    4: (70, 30),
    5: (65, 35),
    6: (65, 35),
}


def _fids(row: dict[str, Any]) -> list[str]:
    """The workspace of every sitting, in order (a workspace may sit twice)."""
    return [s["fid"] for s in row["seats"]]


def _seated(row: dict[str, Any]) -> list[int]:
    """Sittings that hold (or will hold) a stake: not given up before sitting."""
    return [i for i, s in enumerate(row["seats"]) if not s.get("abandoned")]


def _chair(row: dict[str, Any], idx: int) -> int:
    """The table chair (engine seat) a sitting uses."""
    return int(row["seats"][idx].get("chair", idx))


def _here(entry: dict[str, Any]) -> bool:
    """Still at the table: not given up and not departed."""
    return not entry.get("abandoned") and not entry.get("departed")


def _free_chair(row: dict[str, Any]) -> int | None:
    """A chair nobody sits on (cash tables reuse chairs)."""
    used = {int(e.get("chair", i)) for i, e in enumerate(row["seats"]) if _here(e)}
    for chair in range(int(row["table_size"])):
        if chair in used:
            continue
        game = row["game"]
        if game is not None and (
            game["status"][chair] not in ("empty", "left", "out")
            or game["pending"][chair] is not None
        ):
            continue
        return chair
    return None


def tokens_per_chip(row: dict[str, Any]) -> int | None:
    if row["mode"] != "cash":
        return None
    return int(row["stake"]) // int(row["rules"]["starting_stack"])


def _final_stack(row: dict[str, Any], game: dict[str, Any] | None, idx: int) -> int:
    entry = row["seats"][idx]
    start = int(row["rules"]["starting_stack"])
    if not entry.get("authorized_by") or entry.get("abandoned") or entry.get("joining"):
        return start  # never played: keeps the buy-in
    if entry.get("departed"):
        return int(entry["departed_stack"])
    if game is None:
        return start
    chair = _chair(row, idx)
    return int(game["stacks"][chair]) + int(game["pending"][chair] or 0)


def nets(row: dict[str, Any], game: dict[str, Any]) -> list[int]:
    """Tokens each sitting wins (+) or loses (-); they add up to zero.

    cash: (final chips - chips bought) x tokens per chip, per sitting.
    sng: prize shares of everyone's buy-ins by place, minus the buy-in.
    """
    seats = row["seats"]
    stake = int(row["stake"])
    if row["mode"] == "cash":
        tpc = tokens_per_chip(row) or 0
        start = int(row["rules"]["starting_stack"])
        return [(_final_stack(row, game, i) - start) * tpc for i in range(len(seats))]
    players = [i for i in range(len(seats)) if seats[i].get("seed")]
    if game["winner"] is None:  # a drawn heads-up: nothing changes hands
        return [0] * len(seats)
    pool = stake * len(players)
    pay = [0] * len(seats)
    ranking = [s for s in game["ranking"] if s in players]
    paid = 0
    for place, pct in enumerate(PAYOUTS[len(players)]):
        amount = pool * pct // 100
        pay[ranking[place]] += amount
        paid += amount
    pay[ranking[0]] += pool - paid
    return [pay[i] - (stake if i in players else 0) for i in range(len(seats))]


def transfers(net: list[int]) -> list[tuple[int, int, int]]:
    """(loser seat, winner seat, tokens): biggest loser pays biggest winner first."""
    owe = sorted(((-n, i) for i, n in enumerate(net) if n < 0), reverse=True)
    gain = sorted(((n, i) for i, n in enumerate(net) if n > 0), reverse=True)
    owe_l = [[a, i] for a, i in owe]
    gain_l = [[a, i] for a, i in gain]
    out = []
    li = wi = 0
    while li < len(owe_l) and wi < len(gain_l):
        amount = min(owe_l[li][0], gain_l[wi][0])
        out.append((owe_l[li][1], gain_l[wi][1], amount))
        owe_l[li][0] -= amount
        gain_l[wi][0] -= amount
        if owe_l[li][0] == 0:
            li += 1
        if gain_l[wi][0] == 0:
            wi += 1
    return out


def _contract(row: dict[str, Any]) -> dict[str, Any]:
    """The table's terms, hashed; every player authorizes the same hash."""
    return {
        "v": 2,
        "match_id": row["match_id"],
        "table_size": int(row["table_size"]),
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
            "after_timeouts": row["rules"]["max_timeouts"],
        },
        "max_hands": row["rules"]["max_hands"],
        "setup_deadline": row["setup_deadline"].isoformat(),
        "payouts_percent": None
        if row["mode"] == "cash"
        else {str(n): list(p) for n, p in PAYOUTS.items()},
        "settlement": (
            "each seat's final chips minus chips bought, times tokens_per_chip; losers' "
            "tokens become DEMO_M vouchers for winners; no rebuys"
            if row["mode"] == "cash"
            else "prize shares of all buy-ins by place; losers' tokens become DEMO_M "
            "vouchers for winners"
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
    """The workspace's sitting: the one still at the table, else its last."""
    mine = [i for i, s in enumerate(row["seats"]) if s["fid"] == fid]
    if not mine:
        raise FederationError("FED_E_MATCH_UNKNOWN", "no such match")
    here = [i for i in mine if _here(row["seats"][i])]
    return here[-1] if here else mine[-1]


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


# ── the owner's standing policy for agents ────────────────────────────────

MAX_MATCHES_PER_DAY = 200


async def _policy(session: AsyncSession, fid: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text("SELECT * FROM federation_arena_policies WHERE fid = :f"), {"f": fid}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return {
            "agents_may_play": False,
            "max_stake": 0,
            "matches_per_day": 0,
            "updated_by": None,
            "updated_at": None,
        }
    out = dict(row)
    out["updated_at"] = out["updated_at"].isoformat()
    out.pop("fid", None)
    return out


async def get_policy(workspace_id: str) -> dict[str, Any]:
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        return {"fid": me, **(await _policy(session, me))}


async def set_policy(
    workspace_id: str,
    *,
    agents_may_play: bool,
    max_stake: int = 0,
    matches_per_day: int = 0,
    actor: Actor,
) -> dict[str, Any]:
    """The owner sets how far its agents may go on their own: queue, authorize
    (stake up to max_stake, at most matches_per_day a day) and play."""
    require_enabled()
    _person(actor, "sets the arena policy", owner=True)
    if not isinstance(agents_may_play, bool):
        raise FederationError("FED_E_PACKAGE_INVALID", "agents_may_play is true or false")
    for name, value, most in (
        ("max_stake", max_stake, MAX_STAKE),
        ("matches_per_day", matches_per_day, MAX_MATCHES_PER_DAY),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= most:
            raise FederationError("FED_E_PACKAGE_INVALID", f"{name} is 0 to {most}")
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        await session.execute(
            text(
                "INSERT INTO federation_arena_policies (fid, agents_may_play, max_stake, "
                "matches_per_day, updated_by) VALUES (:f, :p, :s, :d, :by) ON CONFLICT (fid) "
                "DO UPDATE SET agents_may_play = :p, max_stake = :s, matches_per_day = :d, "
                "updated_by = :by, updated_at = NOW()"
            ),
            {"f": me, "p": agents_may_play, "s": max_stake, "d": matches_per_day, "by": actor.ref},
        )
        await session.commit()
    return await get_policy(workspace_id)


async def _require_agent_allowed(
    session: AsyncSession, fid: str, actor: Actor, *, stake: int, what: str
) -> None:
    """A person always may; an agent only inside its owner's standing policy."""
    if actor.kind == "user":
        return
    policy = await _policy(session, fid)
    if not policy["agents_may_play"]:
        raise FederationError(
            "FED_E_APPROVAL_NOT_HUMAN",
            f"an agent {what} only if its owner allows agents to play (arena policy)",
        )
    if stake > policy["max_stake"]:
        raise FederationError(
            "FED_E_APPROVAL_NOT_HUMAN",
            "above the stake the owner allows agents",
            max_stake=policy["max_stake"],
        )
    today = int(
        await session.scalar(
            text(
                "SELECT count(*) FROM federation_arena_players WHERE fid = :f AND "
                "authorized_at > NOW() - interval '1 day'"
            ),
            {"f": fid},
        )
        or 0
    )
    if today >= policy["matches_per_day"]:
        raise FederationError(
            "FED_E_APPROVAL_NOT_HUMAN",
            "the owner's daily match limit for agents is reached",
            matches_per_day=policy["matches_per_day"],
        )


# ── queue and matching ────────────────────────────────────────────────────


async def _insert_players(
    session: AsyncSession,
    match_id: str,
    players: list[tuple[str, str]],
    first_seat: int = 0,
    chairs: list[int] | None = None,
) -> None:
    for offset, (fid, spec) in enumerate(players):
        await session.execute(
            text(
                "INSERT INTO federation_arena_players (match_id, seat, chair, fid, spec_id) "
                "VALUES (:m, :s, :c, :f, :p)"
            ),
            {
                "m": match_id,
                "s": first_seat + offset,
                "c": chairs[offset] if chairs else first_seat + offset,
                "f": fid,
                "p": spec,
            },
        )


async def _open_cash_table(
    session: AsyncSession, me: str, *, stake: int, model_class: str, rules_hash: str
) -> dict[str, Any] | None:
    """A 6-seat cash table of this level with a free chair, forming or in play."""
    from app.services.federation import integrity

    ids = (
        (
            await session.execute(
                text(
                    "SELECT match_id FROM federation_arena_matches WHERE mode = 'cash' AND "
                    "table_size = 6 AND stake = :s AND model_class = :c AND rules_hash = :h AND "
                    "state IN ('MATCHED', 'PLAYING') ORDER BY created_at LIMIT 20"
                ),
                {"s": stake, "c": model_class, "h": rules_hash},
            )
        )
        .scalars()
        .all()
    )
    for match_id in ids:
        row = await _match(session, match_id, lock=True)
        if row["state"] not in ("MATCHED", "PLAYING"):
            continue
        if row["game"] is not None and row["game"]["finished"]:
            continue
        await _cash_departures(session, row, "system:seating")
        if _free_chair(row) is None:
            continue
        here = [s["fid"] for s in row["seats"] if _here(s)]
        if me in here:
            continue
        if any([await integrity.related(me, other) for other in here if other != me]):
            continue
        return row
    return None


async def join_queue(
    workspace_id: str,
    *,
    spec_id: str,
    stake: int,
    mode: str = "sng",
    seats: int = 2,
    unit: str | None = None,
    actor: Actor,
) -> dict[str, Any]:
    """A person (or an agent inside its owner's policy) asks for a seat.

    Tournaments (sng) start when the table is full (2 or 6). Cash tables
    start with two; a 6-seat cash table then takes more players at any
    time: a player joining one in play is dealt in from the next hand.
    Workspaces that count as one controller never share a table."""
    require_enabled()
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
    if seats not in TABLE_SIZES:
        raise FederationError("FED_E_PACKAGE_INVALID", "a table has 2 or 6 seats")
    if mode == "cash" and stake % rules["starting_stack"]:
        raise FederationError(
            "FED_E_PACKAGE_INVALID",
            f"a cash table needs a stake divisible by {rules['starting_stack']} chips",
        )
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        await _require_agent_allowed(session, me, actor, stake=stake, what="joins the queue")
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
        if await session.scalar(
            text(
                "SELECT 1 FROM federation_arena_players p JOIN federation_arena_matches m "
                "USING (match_id) WHERE p.fid = :f AND m.state IN ('MATCHED', "
                "'OWNER_AUTHORIZED', 'DEMO_RESERVED', 'COMMITTED', 'PLAYING') AND "
                "COALESCE((m.seats->p.seat->>'departed')::boolean, false) = false AND "
                "COALESCE((m.seats->p.seat->>'abandoned')::boolean, false) = false AND "
                "(m.game IS NULL OR NOT (m.game->'status'->>p.chair::int) = ANY "
                "(ARRAY['left', 'out']))"
            ),
            {"f": me},
        ):
            raise FederationError("FED_E_MATCH_STATE", "this workspace is already at a table")
        model_class = spec["model_family"]
        match_id = None
        entry_id = rights.new_id("faq")
        state = "WAITING"
        if mode == "cash" and seats == 6:
            table = await _open_cash_table(
                session, me, stake=stake, model_class=model_class, rules_hash=rules_hash
            )
            if table is not None:
                seat = len(table["seats"])
                chair = _free_chair(table)
                table["seats"].append(
                    {
                        "fid": me,
                        "spec": spec_id,
                        "chair": chair,
                        "joining": table["state"] == "PLAYING",
                        "joined_at": _now().isoformat(),
                    }
                )
                await _insert_players(
                    session, table["match_id"], [(me, spec_id)], seat, chairs=[chair]
                )
                await _history(
                    session,
                    table["match_id"],
                    table["state"],
                    table["state"],
                    actor.ref,
                    f"chair {chair} taken",
                )
                await _save(session, table)
                match_id = table["match_id"]
                state = "MATCHED"
        if match_id is None:
            need = 1 if mode == "cash" else seats - 1
            candidates = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM federation_arena_queue WHERE state = 'WAITING' AND "
                            "stake = :s AND rules_hash = :h AND model_class = :c AND mode = :md "
                            "AND table_size = :n AND fid <> :f ORDER BY created_at LIMIT 100"
                        ),
                        # Same model class only: like-for-like stakes, no conversion.
                        {
                            "s": stake,
                            "h": rules_hash,
                            "c": model_class,
                            "md": mode,
                            "n": seats,
                            "f": me,
                        },
                    )
                )
                .mappings()
                .all()
            )
            group: list[dict[str, Any]] = []
            for c in candidates:
                if len(group) == need:
                    break
                members = [me] + [g["fid"] for g in group]
                if any([await integrity.related(c["fid"], m) for m in members]):
                    continue  # one controller never plays itself
                if await active_restriction(session, c["fid"]):
                    continue
                group.append(dict(c))
            if len(group) == need:
                players = [(g["fid"], g["spec_id"]) for g in group] + [(me, spec_id)]
                match_id = await _create_match(
                    session,
                    players=players,
                    table_size=seats,
                    model_class=model_class,
                    mode=mode,
                    stake=stake,
                    rules=rules,
                    actor=actor.ref,
                )
                state = "MATCHED"
                await session.execute(
                    text(
                        "UPDATE federation_arena_queue SET state = 'MATCHED', match_id = :m "
                        "WHERE entry_id = ANY(:ids)"
                    ),
                    {"m": match_id, "ids": [g["entry_id"] for g in group]},
                )
            await session.execute(
                text(
                    "INSERT INTO federation_arena_queue (entry_id, fid, spec_id, model_class, "
                    "mode, table_size, stake, rules_hash, state, match_id, created_by) VALUES "
                    "(:e, :f, :s, :c, :md, :n, :k, :h, :st, :m, :by)"
                ),
                {
                    "e": entry_id,
                    "f": me,
                    "s": spec_id,
                    "c": model_class,
                    "md": mode,
                    "n": seats,
                    "k": stake,
                    "h": rules_hash,
                    "st": state,
                    "m": match_id,
                    "by": actor.ref,
                },
            )
        await session.commit()
    return {
        "entry_id": entry_id,
        "state": state,
        "match": await get_match(workspace_id, match_id) if match_id else None,
        "notice": NOTICE,
    }


async def leave_queue(workspace_id: str, *, actor: Actor) -> dict[str, Any]:
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        if actor.kind != "user" and not (await _policy(session, me))["agents_may_play"]:
            raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person leaves the queue")
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
    players: list[tuple[str, str]],
    table_size: int,
    model_class: str,
    mode: str,
    stake: int,
    rules: dict[str, Any],
    actor: str,
) -> str:
    match_id = rights.new_id("fam")
    server_seed = secrets.token_hex(32)
    setup_deadline = _now() + timedelta(minutes=get_settings().federation_arena_setup_minutes)
    now = _now().isoformat()
    row = {
        "match_id": match_id,
        "table_size": table_size,
        "model_class": model_class,
        "mode": mode,
        "stake": stake,
        "rules": rules,
        "rules_hash": engine.rules_hash(rules),
        "server_commit": engine.seed_commitment(server_seed),
        "setup_deadline": setup_deadline,
    }
    seats = [
        {"fid": fid, "spec": spec, "chair": i, "joined_at": now}
        for i, (fid, spec) in enumerate(players)
    ]
    await session.execute(
        text(
            "INSERT INTO federation_arena_matches (match_id, table_size, model_class, mode, "
            "stake, rules, rules_hash, contract_hash, state, seats, server_seed, server_commit, "
            "setup_deadline) VALUES (:m, :n, :mc, :md, :k, CAST(:r AS jsonb), :rh, :ch, "
            "'MATCHED', CAST(:seats AS jsonb), :ss, :sc, :sd)"
        ),
        {
            "m": match_id,
            "n": table_size,
            "mc": model_class,
            "md": mode,
            "k": stake,
            "r": json.dumps(rules),
            "rh": row["rules_hash"],
            "ch": sha256_hex(_contract(row)),
            "seats": json.dumps(seats),
            "ss": server_seed,
            "sc": row["server_commit"],
            "sd": setup_deadline,
        },
    )
    await _insert_players(session, match_id, players)
    await _history(
        session, match_id, None, "MATCHED", actor, f"{len(players)} of {table_size} seats"
    )
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
    status = row["game"]["status"] if row["game"] is not None else None
    # One entry per chair: who sits there now (or last sat there).
    by_chair: dict[int, int] = {}
    for i, e in enumerate(row["seats"]):
        chair = _chair(row, i)
        if chair not in by_chair or _here(e) or not _here(row["seats"][by_chair[chair]]):
            by_chair[chair] = i
    seats = []
    for chair in range(int(row["table_size"])):
        i = by_chair.get(chair)
        e = row["seats"][i] if i is not None else {}
        seats.append(
            {
                "seat": chair,
                "occupant": i,
                "fid": e.get("fid"),
                "authorized": bool(e.get("authorized_by")),
                "agent_play": bool(e.get("agent_play")),
                "commitment": e.get("commit"),
                "revealed": bool(e.get("seed")),
                "joining": bool(e.get("joining")),
                "abandoned": bool(e.get("abandoned")) or i is None,
                "departed": bool(e.get("departed")),
                "status": status[chair] if status is not None else None,
            }
        )
    occupants = [
        {
            "index": i,
            "fid": e["fid"],
            "chair": _chair(row, i),
            "joined_at": e.get("joined_at"),
            "here": _here(e),
            "departed": bool(e.get("departed")),
            "departed_stack": e.get("departed_stack"),
            "returned": int(e.get("returned", 0)),
            "paid": int(e.get("paid", 0)),
            "received": int(e.get("received", 0)),
        }
        for i, e in enumerate(row["seats"])
    ]
    chair = _chair(row, seat) if seat is not None else None
    game = None
    if row["game"] is not None:
        game = engine.seat_view(row["game"], chair)
    revealed = row["state"] in ("RESULT_VERIFIED", "DISPUTED", "DEMO_SETTLED", "CLOSED") or (
        row["state"] == "RECONCILING" and row["result"] is not None
    )
    return {
        "match_id": row["match_id"],
        "state": row["state"],
        "version": row["version"],
        "my_seat": chair,
        "my_occupant": seat,
        "my_fid": row["seats"][seat]["fid"] if seat is not None else None,
        "table_size": int(row["table_size"]),
        "seats": seats,
        "occupants": occupants,
        "model_class": row["model_class"],
        "mode": row["mode"],
        "tokens_per_chip": tokens_per_chip(row),
        "payouts_percent": None
        if row["mode"] == "cash"
        else list(PAYOUTS.get(len([s for s in row["seats"] if not s.get("joining")]), ())),
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
        ids = (
            (
                await session.execute(
                    text(
                        "SELECT p.match_id FROM federation_arena_players p JOIN "
                        "federation_arena_matches m USING (match_id) WHERE p.fid = :m "
                        "ORDER BY m.created_at DESC LIMIT 50"
                    ),
                    {"m": me},
                )
            )
            .scalars()
            .all()
        )
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
    """The owner (or an agent inside the owner's standing policy) authorizes
    the table's contract by its hash; the stake is held from simulated
    capacity in the same transaction. A seat taken at a cash table in play is
    dealt in from the next hand. An agent that authorizes also plays."""
    require_enabled()
    if actor.kind == "user":
        _person(actor, "authorizes a match", owner=True)
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        seat = _seat_of(row, me)
        if actor.kind != "user":
            await _require_agent_allowed(
                session, me, actor, stake=int(row["stake"]), what="authorizes a match"
            )
            agent_play = True
        mine = row["seats"][seat]
        joining = row["state"] == "PLAYING" and mine.get("joining")
        if row["state"] != "MATCHED" and not joining:
            raise FederationError(
                "FED_E_MATCH_STATE", "the match is past authorization", state=row["state"]
            )
        if mine.get("abandoned"):
            raise FederationError("FED_E_MATCH_STATE", "this seat was given up")
        if contract_hash != row["contract_hash"]:
            raise FederationError(
                "FED_E_MATCH_STATE", "authorize the contract as shown: its hash differs"
            )
        if mine.get("authorized_by"):
            raise FederationError("FED_E_MATCH_STATE", "already authorized")
        await _require_unrestricted(session, me)
        spec_id = mine["spec"]
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
        mine.update(authorized_by=actor.ref, agent_play=bool(agent_play))
        await session.execute(
            text(
                "UPDATE federation_arena_players SET authorized_at = NOW() WHERE match_id = :m "
                "AND seat = :s"
            ),
            {"m": match_id, "s": seat},
        )
        if joining:
            # Whoever left this chair is settled first; then the chair is reused.
            await _cash_departures(session, row, actor.ref)
            chair = _chair(row, seat)
            try:
                row["game"] = engine.sit(row["game"], chair, int(row["rules"]["starting_stack"]))
            except engine.IllegalAction as exc:
                raise FederationError("FED_E_MATCH_STATE", str(exc)) from exc
            await _log_action(
                session,
                row,
                chair,
                {"type": "sit", "chips": row["rules"]["starting_stack"]},
                actor.ref,
            )
            mine["joining"] = False
            await _history(
                session,
                match_id,
                row["state"],
                row["state"],
                actor.ref,
                f"chair {chair} sits in from the next hand",
            )
        elif all(row["seats"][i].get("authorized_by") for i in _seated(row)):
            await _move(session, row, "OWNER_AUTHORIZED", actor.ref, "every player authorized")
            await _move(session, row, "DEMO_RESERVED", actor.ref, "every stake held")
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
                "FED_E_MATCH_STATE", "commit after every stake is held", state=row["state"]
            )
        if row["seats"][seat].get("commit"):
            if row["seats"][seat]["commit"] == commitment.lower():
                return await _view(session, row, seat)
            raise FederationError("FED_E_MATCH_STATE", "a commitment cannot be changed")
        row["seats"][seat]["commit"] = commitment.lower()
        if all(row["seats"][i].get("commit") for i in _seated(row)):
            await _move(session, row, "COMMITTED", actor.ref, "every player committed")
        await _save(session, row)
        await session.commit()
    return await get_match(workspace_id, match_id)


async def reveal_seed(workspace_id: str, match_id: str, *, seed: str, actor: Actor):
    """A seat reveals its seed; it must match its commitment. When every
    player has revealed, the decks are fixed and the first hand is dealt."""
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
                "FED_E_MATCH_STATE", "reveal after every player committed", state=row["state"]
            )
        if engine.seed_commitment(seed) != row["seats"][seat]["commit"]:
            raise FederationError(
                "FED_E_ARENA_ILLEGAL", "the seed does not match this seat's commitment"
            )
        row["seats"][seat]["seed"] = seed
        if all(row["seats"][i].get("seed") for i in _seated(row)):
            row["game"] = engine.new_match(
                row["rules"],
                _seed(row),
                players=len(row["seats"]),
                seats=int(row["table_size"]),
                mode=row["mode"],
            )
            await _move(session, row, "PLAYING", actor.ref, "every seed revealed; hand 1 dealt")
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


def _starters(row: dict[str, Any]) -> list[int]:
    """The seats dealt in at the start: those that revealed a seed."""
    return [i for i, s in enumerate(row["seats"]) if s.get("seed")]


def _seed(row: dict[str, Any]) -> str:
    return engine.combined_seed(
        row["server_seed"], [row["seats"][i]["seed"] for i in _starters(row)]
    )


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
    if action.get("type") in ("timeout", "concede", "stand_up", "sit"):
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
            if prior and prior["seat"] == _chair(row, seat) and prior_action == action:
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
            row["game"] = engine.apply(
                row["rules"], _seed(row), row["game"], _chair(row, seat), action
            )
        except engine.IllegalAction as exc:
            raise FederationError("FED_E_ARENA_ILLEGAL", str(exc)) from exc
        await _log_action(session, row, _chair(row, seat), action, actor.ref)
        if row["game"]["finished"]:
            await _finish(session, row, actor.ref)
        else:
            row["turn_deadline"] = _deadline(row)
            await _cash_departures(session, row, actor.ref)
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
    """Everything per sitting (index into ``seats``); chairs say where each sat."""
    game = row["game"]
    net = nets(row, game)
    fids = _fids(row)
    starters = _starters(row)
    played = [i for i in _seated(row) if row["seats"][i].get("authorized_by")]
    if row["mode"] == "sng":
        ranking = [s for s in (game["ranking"] or []) if s < len(fids)]
        winner = game["winner"]
    else:
        ranking = sorted(played, key=lambda i: (-net[i], i))
        tops = [i for i in played if net[i] == max((net[j] for j in played), default=0)]
        winner = tops[0] if len(tops) == 1 and net[tops[0]] > 0 else None
    return {
        "v": 3,
        "match_id": row["match_id"],
        "contract_hash": row["contract_hash"],
        "rules_hash": row["rules_hash"],
        "table_size": int(row["table_size"]),
        "seats": fids,
        "chairs": [_chair(row, i) for i in range(len(fids))],
        "starters": starters,
        "model_class": row["model_class"],
        "mode": row["mode"],
        "tokens_per_chip": tokens_per_chip(row),
        "stake": int(row["stake"]),
        "unit": rights.UNIT,
        "server_commit": row["server_commit"],
        "server_seed": row["server_seed"],
        "seat_commits": [row["seats"][i]["commit"] for i in starters],
        "seat_seeds": [row["seats"][i]["seed"] for i in starters],
        "action_count": len(log),
        "actions_sha256": sha256_hex([{"seat": e["seat"], "action": e["action"]} for e in log]),
        "final_state_sha256": sha256_hex(game),
        "winner_seat": winner,
        "winner": fids[winner] if winner is not None else None,
        "ranking": ranking,
        "nets": net,
        "settles_tokens": sum(n for n in net if n > 0),
        "reason": game["reason"],
        "final_stacks": [_final_stack(row, game, i) for i in range(len(fids))],
        "hands_played": len(game["hands"]),
        "economic_value": "none",
    }


async def _finish(session: AsyncSession, row: dict[str, Any], actor: str) -> None:
    """The game is over: build the proof, reveal the host seed, sign."""
    from app.services.federation import identity

    log = await _actions(session, row["match_id"])
    payload = _proof_payload(row, log)
    signatures = []
    for i in _seated(row):
        fid = row["seats"][i]["fid"]
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
        f"{payload['reason']}: winner {payload['winner'] or 'none (even)'}",
    )


async def leave(workspace_id: str, match_id: str, *, actor: Actor) -> dict[str, Any]:
    """Before play: the table is called off and every stake goes back (a
    person). In play: a tournament seat concedes (out, its chips go to the
    others); a cash seat stands up after the hand in progress, and is settled
    then: what it did not lose comes back at once. A seat taken but not yet
    dealt in is given up."""
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        row = await _match(session, match_id, lock=True)
        seat = _seat_of(row, me)
        mine = row["seats"][seat]
        chair = _chair(row, seat)
        if not _here(mine) and row["state"] == "PLAYING":
            raise FederationError("FED_E_MATCH_STATE", "you have already left this table")
        if row["state"] in SETUP_STATES:
            _person(actor, "leaves a match")
            await _return_stakes(session, row, "ABORTED", actor.ref, f"chair {chair} left")
        elif row["state"] == "PLAYING" and mine.get("joining"):
            mine["abandoned"] = True
            mine["joining"] = False
            await _history(
                session,
                match_id,
                "PLAYING",
                "PLAYING",
                actor.ref,
                f"chair {chair} given up before sitting in",
            )
        elif row["state"] == "PLAYING" and row["mode"] == "cash":
            _may_play(row, seat, actor)
            game = row["game"]
            if (game["status"][chair] == "active" and chair not in game.get("standing", [])) or (
                game["pending"][chair] is not None
            ):
                row["game"] = engine.stand_up(game, chair)
                await _log_action(session, row, chair, {"type": "stand_up"}, actor.ref)
            await _cash_departures(session, row, actor.ref)
        elif row["state"] == "PLAYING":
            _may_play(row, seat, actor)
            if row["game"]["status"][chair] != "active":
                raise FederationError("FED_E_MATCH_STATE", "this seat is already out")
            row["game"] = engine.concede(row["game"], chair, row["rules"], _seed(row))
            await _log_action(session, row, chair, {"type": "concede"}, actor.ref)
            if row["game"]["finished"]:
                await _finish(session, row, actor.ref)
            else:
                row["turn_deadline"] = _deadline(row)
        else:
            raise FederationError("FED_E_MATCH_STATE", "nothing to leave", state=row["state"])
        await _save(session, row)
        await session.commit()
    return await get_match(workspace_id, match_id)


async def _pay(
    session: AsyncSession, row: dict[str, Any], loser: int, winner: int, amount: int, actor: str
) -> dict[str, Any]:
    """One loser pays one winner from its held stake: a DEMO_M voucher and a
    delivery obligation."""
    match_id = row["match_id"]
    fids = _fids(row)
    payer = row["seats"][loser]
    n = len(payer.setdefault("paid_to", []))
    loser_spec = await rights.spec_row(session, payer["spec"])
    expires = _now() + timedelta(days=get_settings().federation_arena_voucher_days)
    voucher_id = await rights.issue(
        session,
        spec=loser_spec,
        beneficiary=fids[winner],
        amount=amount,
        origin="arena",
        origin_ref=f"{match_id}:{loser}:{winner}:{n}",
        usage_scope=(
            f"Arena match {match_id}: delegate work to {fids[loser]} through the federation "
            f"bridge on model class {row['model_class']} only, up to this amount of simulated "
            "capacity"
        ),
        expires_at=expires,
        source_account=rights.stake(match_id, loser),
        idempotency_key=f"award:{match_id}:{loser}:{winner}:{n}",
    )
    payer["paid"] = int(payer.get("paid", 0)) + amount
    row["seats"][winner]["received"] = int(row["seats"][winner].get("received", 0)) + amount
    record = {
        "voucher_id": voucher_id,
        "from": fids[loser],
        "to": fids[winner],
        "amount": amount,
        "at": _now().isoformat(),
    }
    payer["paid_to"].append(record)
    obligation_id = rights.new_id("fob")
    await session.execute(
        text(
            "INSERT INTO federation_arena_obligations (obligation_id, match_id, obligor_fid, "
            "obligee_fid, voucher_id, amount, state, due_at) VALUES (:o, :m, :a, :b, :v, :k, "
            "'PENDING', :d)"
        ),
        {
            "o": obligation_id,
            "m": match_id,
            "a": fids[loser],
            "b": fids[winner],
            "v": voucher_id,
            "k": amount,
            "d": expires,
        },
    )
    await _ob_history(session, obligation_id, None, "PENDING", actor, f"match {match_id}")
    return record


async def _cash_departures(session: AsyncSession, row: dict[str, Any], actor: str) -> None:
    """Cash tables settle a player as it leaves (stood up or busted): what its
    stack did not lose comes back at once; losses and gains between players
    who have both left are paid now; the rest when the other side leaves or
    the table ends."""
    if row["mode"] != "cash" or row["game"] is None or row["game"]["finished"]:
        return
    game = row["game"]
    match_id = row["match_id"]
    start = int(row["rules"]["starting_stack"])
    tpc = tokens_per_chip(row) or 0
    changed = False
    for idx, e in enumerate(row["seats"]):
        if not _here(e) or not e.get("authorized_by") or e.get("joining"):
            continue
        chair = _chair(row, idx)
        status = game["status"][chair]
        if status == "active" or game["pending"][chair] is not None:
            continue
        # left, out, or stood up before being dealt in (empty again)
        stack = start if status == "empty" else int(game["stacks"][chair])
        e["departed"] = True
        e["departed_stack"] = stack
        e["departed_at"] = _now().isoformat()
        back = min(stack, start) * tpc
        if back:
            await rights.post(
                session,
                kind="stake_return",
                entries=[(rights.stake(match_id, idx), -back), (rights.cap(e["spec"]), back)],
                reference=match_id,
                idempotency_key=f"leave:{match_id}:{idx}",
            )
        e["returned"] = back
        changed = True
        await _history(
            session,
            match_id,
            row["state"],
            row["state"],
            actor,
            f"chair {chair} left with {stack} chips: {back} tokens back now",
        )
    if not changed:
        return
    # Pay what is final between players who have both left.
    left = [i for i, e in enumerate(row["seats"]) if e.get("departed")]
    remaining = [0] * len(row["seats"])
    for i in left:
        e = row["seats"][i]
        net = (int(e["departed_stack"]) - start) * tpc
        remaining[i] = net - int(e.get("received", 0)) + int(e.get("paid", 0))
    for loser, winner, amount in transfers(remaining):
        await _pay(session, row, loser, winner, amount, actor)


async def _return_stakes(
    session: AsyncSession, row: dict[str, Any], to: str | None, actor: str, note: str
) -> str | None:
    """Every stake still held goes back to its capacity."""
    txn = None
    for seat, s in enumerate(row["seats"]):
        account = rights.stake(row["match_id"], seat)
        await rights.lock(session, account)
        held = await rights.balance(session, account)
        if held:
            txn, _ = await rights.post(
                session,
                kind="stake_return",
                entries=[(account, -held), (rights.cap(s["spec"]), held)],
                reference=row["match_id"],
                idempotency_key=f"return:{row['match_id']}:{seat}",
            )
    row["turn_deadline"] = None
    if to is not None:
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
    (every stake returns). Also clears RECONCILING."""
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
    commitments, the proof hash and every signature."""
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
        row["rules"],
        seed,
        [{"seat": e["seat"], "action": e["action"]} for e in log],
        players=len(proof["starters"]),
        seats=int(row["table_size"]),
        mode=row["mode"],
    )
    checks["replay_matches"] = sha256_hex(replayed) == proof["final_state_sha256"]
    checks["rules_hash"] = engine.rules_hash(row["rules"]) == proof["rules_hash"]
    checks["actions"] = (
        sha256_hex([{"seat": e["seat"], "action": e["action"]} for e in log])
        == proof["actions_sha256"]
    )
    checks["proof_hash"] = sha256_hex(proof) == row["result_proof_hash"]
    checks["nets_balance"] = sum(proof["nets"]) == 0
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
    checks["signatures"] = bool(signed) and all(s["valid"] for s in signed)
    return {
        "match_id": match_id,
        "valid": all(checks.values()),
        "checks": checks,
        "signatures": signed,
        "winner_seat": replayed["winner"],
        "ranking": replayed["ranking"],
        "reason": replayed["reason"],
        "hands": len(replayed["hands"]),
        "hand_history": replayed["hands"],
    }


# ── settlement ────────────────────────────────────────────────────────────


async def _settle(session: AsyncSession, row: dict[str, Any], actor: str) -> None:
    """Once per match: what is still owed between players (after anything
    paid when cash players left) is paid as DEMO_M vouchers with a delivery
    obligation each; every stake's remainder goes back. Nothing is created:
    the nets add up to zero."""
    if await session.scalar(
        text("SELECT 1 FROM federation_arena_settlements WHERE match_id = :m"),
        {"m": row["match_id"]},
    ):
        return
    stake = int(row["stake"])
    match_id = row["match_id"]
    held, expected = [], []
    for seat, s in enumerate(row["seats"]):
        await rights.lock(session, rights.stake(match_id, seat))
        held.append(await rights.balance(session, rights.stake(match_id, seat)))
        expected.append(
            stake - int(s.get("returned", 0)) - int(s.get("paid", 0))
            if s.get("authorized_by")
            else 0
        )
    if held != expected:
        await _move(
            session,
            row,
            "RECONCILING",
            actor,
            f"stakes held {held}, expected {expected}: nothing moved",
        )
        return
    net = row["result"]["proof"]["nets"]
    remaining = [
        net[i] - int(s.get("received", 0)) + int(s.get("paid", 0))
        for i, s in enumerate(row["seats"])
    ]
    for loser, winner, amount in transfers(remaining):
        await _pay(session, row, loser, winner, amount, actor)
    vouchers = [v for s in row["seats"] for v in s.get("paid_to", [])]
    txn = await _return_stakes(session, row, None, actor, "remainders back")
    await session.execute(
        text(
            "INSERT INTO federation_arena_settlements (match_id, kind, txn_id, vouchers) "
            "VALUES (:m, :k, :t, CAST(:v AS jsonb))"
        ),
        {
            "m": match_id,
            "k": "award" if vouchers else "return",
            "t": txn,
            "v": json.dumps(vouchers),
        },
    )
    await _move(
        session,
        row,
        "DEMO_SETTLED",
        actor,
        "; ".join(f"voucher {v['voucher_id']} for {v['to']}" for v in vouchers)[:1900]
        or "nothing changes hands: every stake back",
    )
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
    """Turn timeouts, setup deadlines, and seats taken at a cash table in play
    but never authorized."""
    now = _now()
    setup = timedelta(minutes=get_settings().federation_arena_setup_minutes)
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT match_id FROM federation_arena_matches WHERE (state = 'PLAYING' "
                        "AND (turn_deadline <= :n OR seats::text LIKE '%\"joining\": true%')) "
                        "OR (state IN ('MATCHED', 'OWNER_AUTHORIZED', 'DEMO_RESERVED', "
                        "'COMMITTED') AND setup_deadline <= :n) LIMIT :l"
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
            changed = False
            if row["state"] == "PLAYING":
                for i, s in enumerate(row["seats"]):
                    if s.get("joining") and datetime.fromisoformat(s["joined_at"]) + setup <= now:
                        s["joining"] = False
                        s["abandoned"] = True
                        await _history(
                            session,
                            match_id,
                            "PLAYING",
                            "PLAYING",
                            "system:timeout",
                            f"seat {i} not authorized in time: given up",
                        )
                        changed = True
                if row["turn_deadline"] and row["turn_deadline"] <= now:
                    seat = row["game"]["hand"]["to_act"]
                    row["game"] = engine.apply(
                        row["rules"], _seed(row), row["game"], seat, {"type": "timeout"}
                    )
                    await _log_action(session, row, seat, {"type": "timeout"}, "system:timeout")
                    if row["game"]["finished"]:
                        await _finish(session, row, "system:timeout")
                    else:
                        row["turn_deadline"] = _deadline(row)
                        await _cash_departures(session, row, "system:timeout")
                    changed = True
            elif row["state"] in SETUP_STATES and row["setup_deadline"] <= now:
                key = {"COMMITTED": "seed", "DEMO_RESERVED": "commit"}.get(
                    row["state"], "authorized_by"
                )
                missing = [i for i in _seated(row) if not row["seats"][i].get(key)]
                await _return_stakes(
                    session,
                    row,
                    "TIMED_OUT",
                    "system:timeout",
                    f"setup not finished in time ({row['state']}); waiting on seats {missing}",
                )
                changed = True
            if not changed:
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
                    "SELECT count(*) FROM federation_arena_players p JOIN "
                    "federation_arena_matches m USING (match_id) WHERE p.fid = :f AND "
                    "m.state = 'CLOSED'"
                ),
                {"f": fid},
            )
            or 0
        )
        disputes = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM federation_arena_history h JOIN "
                    "federation_arena_players p ON p.match_id = h.match_id WHERE "
                    "h.to_state = 'DISPUTED' AND p.fid = :f"
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
    """Operator view: pairs that keep sharing tables, or one-sided runs.
    Flags are for review only; nothing is restricted automatically (RR-11)."""
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT a.fid AS a, b.fid AS b, count(*) AS n, "
                        "array_agg(m.result->'proof'->>'winner') AS winners FROM "
                        "federation_arena_players a JOIN federation_arena_players b ON "
                        "a.match_id = b.match_id AND a.fid < b.fid JOIN "
                        "federation_arena_matches m ON m.match_id = a.match_id WHERE "
                        "m.state = 'CLOSED' AND m.updated_at > :t GROUP BY 1, 2 HAVING "
                        "count(*) >= :k"
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


# ── lobby, standings, my record ──────────────────────────────────────────

# The tables the lobby offers (tokens of simulated capacity), heads-up and
# 6-max. Cash: the buy-in (1000 chips); a chip is worth buy-in / 1000 tokens.
# Tournament (sit and go): the buy-in; prizes are shares of all buy-ins.
CASH_LEVELS = (100_000, 500_000, 1_000_000, 2_000_000, 5_000_000)
SNG_LEVELS = (100_000, 1_000_000, 5_000_000)


async def lobby(workspace_id: str) -> dict[str, Any]:
    """What a player picks from: each table level and size per model class,
    how many are waiting, the cash tables with a free seat, and where this
    workspace stands."""
    require_enabled()
    rules = _rules()
    rules_hash = engine.rules_hash(rules)
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        waiting = (
            (
                await session.execute(
                    text(
                        "SELECT mode, table_size, stake, model_class, count(*) AS n FROM "
                        "federation_arena_queue WHERE state = 'WAITING' AND rules_hash = :h "
                        "AND fid <> :f GROUP BY 1, 2, 3, 4"
                    ),
                    {"h": rules_hash, "f": me},
                )
            )
            .mappings()
            .all()
        )
        open_rows = (
            (
                await session.execute(
                    text(
                        "SELECT match_id, stake, model_class, state, seats FROM "
                        "federation_arena_matches WHERE mode = 'cash' AND table_size = 6 AND "
                        "rules_hash = :h AND state IN ('MATCHED', 'PLAYING') AND "
                        "(game IS NULL OR NOT (game->>'finished')::boolean)"
                    ),
                    {"h": rules_hash},
                )
            )
            .mappings()
            .all()
        )
        mine = (
            (
                await session.execute(
                    text(
                        "SELECT entry_id, mode, table_size, stake, model_class, created_at FROM "
                        "federation_arena_queue WHERE fid = :f AND state = 'WAITING'"
                    ),
                    {"f": me},
                )
            )
            .mappings()
            .first()
        )
        restriction = await active_restriction(session, me)
        policy = await _policy(session, me)
    counts = {
        (r["mode"], int(r["table_size"]), int(r["stake"]), r["model_class"]): int(r["n"])
        for r in waiting
    }
    open_tables: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in open_rows:
        seats = r["seats"] if not isinstance(r["seats"], str) else json.loads(r["seats"])
        taken = [s for s in seats if not s.get("abandoned")]
        open_tables.setdefault((int(r["stake"]), r["model_class"]), []).append(
            {
                "match_id": r["match_id"],
                "players": len(taken),
                "free": 6 - len(seats),
                "in_play": r["state"] == "PLAYING",
            }
        )
    classes = rights.model_classes()

    def tables(mode: str, levels: tuple[int, ...]) -> list[dict[str, Any]]:
        out = []
        for size in TABLE_SIZES:
            for stake in levels:
                item: dict[str, Any] = {
                    "mode": mode,
                    "seats": size,
                    "stake": stake,
                    "stake_m": rights.as_m(stake),
                    "starting_chips": rules["starting_stack"],
                    "blinds": rules["blinds"][0],
                    "waiting": {c: counts.get((mode, size, stake, c), 0) for c in classes},
                }
                if mode == "cash":
                    item["tokens_per_chip"] = stake // rules["starting_stack"]
                    if size == 6:
                        item["open_tables"] = {
                            c: [t for t in open_tables.get((stake, c), []) if t["free"] > 0]
                            for c in classes
                        }
                else:
                    item["prize_pool"] = size * stake
                    item["prize_pool_m"] = rights.as_m(size * stake)
                    item["payouts_percent"] = list(PAYOUTS[size])
                    item["blind_levels"] = rules["blinds"]
                    item["hands_per_level"] = rules["hands_per_level"]
                out.append(item)
        return out

    return {
        "model_classes": classes,
        "table_sizes": list(TABLE_SIZES),
        "cash": tables("cash", CASH_LEVELS),
        "tournaments": tables("sng", SNG_LEVELS),
        "rules": rules,
        "queued": {
            "entry_id": mine["entry_id"],
            "mode": mine["mode"],
            "seats": int(mine["table_size"]),
            "stake": int(mine["stake"]),
            "model_class": mine["model_class"],
            "since": mine["created_at"].isoformat(),
        }
        if mine
        else None,
        "restricted": restriction is not None,
        "policy": policy,
        "simulated": True,
        "economic_value": "none",
        "notice": NOTICE,
    }


async def _closed_results(session: AsyncSession, fid: str | None = None) -> list[dict]:
    query = (
        "SELECT match_id, mode, table_size, model_class, stake, seats, result, updated_at "
        "FROM federation_arena_matches WHERE state IN ('CLOSED', 'DEMO_SETTLED') AND "
        "result IS NOT NULL"
    )
    params: dict[str, Any] = {}
    if fid:
        query += " AND match_id IN (SELECT match_id FROM federation_arena_players WHERE fid = :f)"
        params["f"] = fid
    rows = (
        (await session.execute(text(query + " ORDER BY updated_at DESC LIMIT 2000"), params))
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        result = r["result"] if not isinstance(r["result"], str) else json.loads(r["result"])
        seats = r["seats"] if not isinstance(r["seats"], str) else json.loads(r["seats"])
        out.append({**dict(r), "seats": seats, "proof": result["proof"]})
    return out


def _net_of(row: dict[str, Any], fid: str) -> int | None:
    """A workspace's net over all its sittings in one match."""
    fids = row["proof"]["seats"]
    if fid not in fids:
        return None
    return sum(int(n) for f, n in zip(fids, row["proof"]["nets"], strict=True) if f == fid)


def _tally(rows: list[dict], fid: str) -> dict[str, Any]:
    won = lost = even = net = 0
    for r in rows:
        n = _net_of(r, fid)
        if n is None:
            continue
        net += n
        if n > 0:
            won += 1
        elif n < 0:
            lost += 1
        else:
            even += 1
    return {"played": won + lost + even, "won": won, "lost": lost, "even": even, "net": net}


async def my_record(workspace_id: str) -> dict[str, Any]:
    """This workspace's record and its recent finished matches."""
    require_enabled()
    async with async_session_factory() as session:
        me = await rights.fid_of(session, workspace_id)
        rows = await _closed_results(session, me)
    recent = []
    for r in rows[:20]:
        fids = r["proof"]["seats"]
        seat = fids.index(me)
        ranking = r["proof"].get("ranking") or [] if r["mode"] == "sng" else []
        others = [f for f in fids if f != me]
        recent.append(
            {
                "match_id": r["match_id"],
                "mode": r["mode"],
                "table_size": int(r["table_size"]),
                "model_class": r["model_class"],
                "stake": int(r["stake"]),
                "opponent": others[0] if others else None,
                "opponents": others,
                "place": ranking.index(seat) + 1 if seat in ranking else None,
                "players": len(set(fids)),
                "reason": r["proof"]["reason"],
                "hands": r["proof"]["hands_played"],
                "net": _net_of(r, me),
                "at": r["updated_at"].isoformat(),
            }
        )
    return {
        "fid": me,
        **_tally(rows, me),
        "recent": recent,
        "unit": rights.UNIT,
        "economic_value": "none",
    }


async def leaderboard(limit: int = 20) -> dict[str, Any]:
    """Standings over finished matches: net DEMO_M and wins per workspace.
    Shows a workspace's public handle when it has one."""
    require_enabled()
    from app.services.federation import identity

    async with async_session_factory() as session:
        rows = await _closed_results(session)
        fids = {f for r in rows for f in r["proof"]["seats"]}
        board = []
        for fid in fids:
            board.append(
                {"fid": fid, "handle": await identity._alias(session, fid), **_tally(rows, fid)}
            )
    board.sort(key=lambda b: (-b["net"], -b["won"], b["fid"]))
    return {
        "items": board[: max(1, min(limit, 100))],
        "unit": rights.UNIT,
        "economic_value": "none",
        "notice": NOTICE,
    }


async def admin_obligations(state: str | None = None) -> list[dict[str, Any]]:
    """Operator: obligations to review (in review and restricted by default)."""
    states = [state] if state else ["OVERDUE_REVIEW", "RESTRICTED", "CONFIRMED_UNFULFILLED"]
    async with async_session_factory() as session:
        ids = (
            (
                await session.execute(
                    text(
                        "SELECT obligation_id FROM federation_arena_obligations WHERE state = "
                        "ANY(:s) ORDER BY updated_at DESC LIMIT 200"
                    ),
                    {"s": states},
                )
            )
            .scalars()
            .all()
        )
    return [await get_obligation(i) for i in ids]


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
