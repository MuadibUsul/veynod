"""No-limit hold'em rules engine for the Agent Poker Arena: 2 to 6 seats.

Contract: docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md §5.

Pure and deterministic: no database, no clock, no randomness of its own.
The deck of every hand comes from one seed (the match's combined
commit/reveal seed) and the hand number, so anyone holding the seeds and the
action log can replay the match and get the same result (``replay``).

* Heads-up (two players in the hand): the button posts the small blind and
  acts first before the flop; the big blind acts first after it.
* Three or more: small blind left of the button, big blind next, the first
  to act before the flop is left of the big blind; after the flop, the first
  player left of the button who can still act.
* Side pots: every pot is split among the players who are still in and put
  in at least that level; odd chips go clockwise from the button.
* Modes: ``sng`` (tournament: busted players are out, the last player with
  chips wins, places are kept) and ``cash`` (players may sit down at an empty
  seat and stand up between hands; the table ends when fewer than two remain).

Chips (ARENA_CHIP) exist only inside this state. They have no ledger, no
price and no conversion: the match result is the final stacks and places.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
from itertools import combinations
from typing import Any

from app.services.federation.canonical import sha256_hex

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = tuple(r + s for r in RANKS for s in SUITS)

RULES_V1: dict[str, Any] = {
    "version": "nlhe-v1",
    "unit": "ARENA_CHIP",
    "starting_stack": 1000,
    # [small blind, big blind] per level; the last level repeats.
    "blinds": [[5, 10], [10, 20], [15, 30], [25, 50], [50, 100], [100, 200], [200, 400]],
    "hands_per_level": 8,
    "max_hands": 80,  # then the table ends: places / settlement by stacks
    "turn_seconds": 60,
    "max_timeouts": 3,  # in a row: out of a tournament; up from a cash table
}
MIN_SEATS, MAX_SEATS = 2, 6
MODES = ("sng", "cash")

ACTIONS = ("fold", "check", "call", "bet", "raise", "all_in")
CATEGORIES = (
    "high_card",
    "pair",
    "two_pair",
    "three_of_a_kind",
    "straight",
    "flush",
    "full_house",
    "four_of_a_kind",
    "straight_flush",
)


class IllegalAction(ValueError):
    """The action is not allowed in this state (the state is unchanged)."""


def rules_hash(rules: dict[str, Any]) -> str:
    return sha256_hex(rules)


def seed_commitment(seed: str) -> str:
    """What a party publishes before anyone reveals: sha256 of the seed text."""
    return hashlib.sha256(seed.encode()).hexdigest()


def combined_seed(server_seed: str, seat_seeds: list[str]) -> str:
    """Every party's seed counts; nobody alone decides the decks."""
    return hashlib.sha256("|".join([server_seed, *seat_seeds]).encode()).hexdigest()


def shuffled_deck(seed: str, hand_no: int) -> list[str]:
    """Fisher-Yates driven by HMAC-SHA256(seed, hand:counter), with rejection
    sampling so every permutation is equally likely."""
    key = bytes.fromhex(seed)
    deck = list(DECK)
    counter = 0

    def uniform(bound: int) -> int:
        nonlocal counter
        limit = (1 << 64) - ((1 << 64) % bound)
        while True:
            block = hmac.new(key, f"{hand_no}:{counter}".encode(), hashlib.sha256).digest()
            counter += 1
            value = int.from_bytes(block[:8], "big")
            if value < limit:
                return value % bound

    for i in range(len(deck) - 1, 0, -1):
        j = uniform(i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck


def deck_digest(seed: str, hand_no: int) -> str:
    return hashlib.sha256(",".join(shuffled_deck(seed, hand_no)).encode()).hexdigest()


# ── hand evaluation ───────────────────────────────────────────────────────


def _rank(card: str) -> int:
    return RANKS.index(card[0]) + 2


def evaluate5(cards: list[str]) -> tuple[int, ...]:
    """(category, tie-breakers...): a bigger tuple is a better hand."""
    ranks = sorted((_rank(c) for c in cards), reverse=True)
    flush = len({c[1] for c in cards}) == 1
    distinct = sorted(set(ranks), reverse=True)
    straight_high = 0
    if len(distinct) == 5:
        if distinct[0] - distinct[4] == 4:
            straight_high = distinct[0]
        elif distinct == [14, 5, 4, 3, 2]:  # the wheel
            straight_high = 5
    counts: dict[int, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    # by count, then rank
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = [n for _, n in groups]
    order = [r for r, _ in groups]
    if straight_high and flush:
        return (8, straight_high)
    if shape == [4, 1]:
        return (7, *order)
    if shape == [3, 2]:
        return (6, *order)
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if shape == [3, 1, 1]:
        return (3, *order)
    if shape == [2, 2, 1]:
        return (2, *order)
    if shape == [2, 1, 1, 1]:
        return (1, *order)
    return (0, *ranks)


def best_hand(cards: list[str]) -> tuple[tuple[int, ...], list[str]]:
    best: tuple[tuple[int, ...], list[str]] | None = None
    for five in combinations(cards, 5):
        score = evaluate5(list(five))
        if best is None or score > best[0]:
            best = (score, list(five))
    assert best is not None
    return best


# ── match state ───────────────────────────────────────────────────────────


# ── match state ───────────────────────────────────────────────────────────


def new_match(
    rules: dict[str, Any],
    seed: str,
    *,
    players: int = 2,
    seats: int | None = None,
    mode: str = "sng",
) -> dict[str, Any]:
    """``players`` sit at seats 0..players-1 of a ``seats``-seat table."""
    size = seats or players
    if not MIN_SEATS <= players <= size <= MAX_SEATS:
        raise ValueError("2 to 6 seats, at least 2 players")
    if mode not in MODES:
        raise ValueError("mode is sng or cash")
    start = int(rules["starting_stack"])
    state: dict[str, Any] = {
        "rules_hash": rules_hash(rules),
        "mode": mode,
        "size": size,
        "stacks": [start] * players + [0] * (size - players),
        "status": ["active"] * players + ["empty"] * (size - players),
        "buy_ins": [start] * players + [0] * (size - players),
        # cash: chips taken off the table by players who left (or busted)
        # before their seat was taken again
        "cashed_out": 0,
        "pending": [None] * size,  # cash: chips of a player sitting down next hand
        "standing": [],  # cash: seats leaving after this hand
        "forfeits": [],  # sng: seats out after this hand (conceded or timed out)
        "out_reason": None,
        "hand_no": 0,
        "button": None,
        "hand": None,
        "hands": [],
        "timeouts": [0] * size,
        "eliminated": [],  # sng: seats in the order they went out
        "finished": False,
        "winner": None,
        "ranking": None,
        "reason": None,
    }
    _start_hand(rules, seed, state)
    return state


def _blinds(rules: dict[str, Any], hand_no: int, mode: str = "sng") -> tuple[int, int]:
    """Tournaments raise the blinds by level; cash tables keep the first level."""
    if mode == "cash":
        sb, bb = rules["blinds"][0]
        return int(sb), int(bb)
    level = min((hand_no - 1) // rules["hands_per_level"], len(rules["blinds"]) - 1)
    sb, bb = rules["blinds"][level]
    return int(sb), int(bb)


def _after(seats: list[int], seat: int) -> int:
    """The next seat of ``seats`` clockwise after ``seat`` (seat need not be in it)."""
    later = [s for s in seats if s > seat]
    return later[0] if later else seats[0]


def _start_hand(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    for seat, chips in enumerate(state["pending"]):
        if chips is not None:
            state["status"][seat] = "active"
            state["stacks"][seat] = chips
            state["buy_ins"][seat] += chips
            state["pending"][seat] = None
    size = state["size"]
    players = [s for s in range(size) if state["status"][s] == "active" and state["stacks"][s] > 0]
    state["hand_no"] += 1
    no = state["hand_no"]
    button = players[0] if state["button"] is None else _after(players, state["button"])
    state["button"] = button
    if len(players) == 2:
        sb, bb = button, _after(players, button)
        first = button
    else:
        sb = _after(players, button)
        bb = _after(players, sb)
        first = _after(players, bb)
    deck = shuffled_deck(seed, no)
    hole: list[list[str] | None] = [None] * size
    for k, seat in enumerate(players):
        hole[seat] = deck[2 * k : 2 * k + 2]
    base = 2 * len(players)
    small, big = _blinds(rules, no, state["mode"])
    stacks = state["stacks"]
    committed = [0] * size
    for seat, blind in ((sb, small), (bb, big)):
        paid = min(blind, stacks[seat])
        committed[seat] = paid
        stacks[seat] -= paid
    state["hand"] = {
        "no": no,
        "button": button,
        "sb": sb,
        "bb": bb,
        "players": players,
        "blinds": [small, big],
        "deck_sha256": hashlib.sha256(",".join(deck).encode()).hexdigest(),
        "hole": hole,
        # burn, flop x3, burn, turn, burn, river after the hole cards
        "runout": [deck[base + 1], deck[base + 2], deck[base + 3], deck[base + 5], deck[base + 7]],
        "board": [],
        "street": "preflop",
        "committed": committed,
        "contrib": list(committed),
        "min_raise": big,
        "acted": [False] * size,
        "folded": [False] * size,
        "to_act": first,
        "actions": [],
    }
    _progress(rules, seed, state, start=first)


# ── who acts ──────────────────────────────────────────────────────────────


def _live(hand: dict[str, Any]) -> list[int]:
    return [s for s in hand["players"] if not hand["folded"][s]]


def _needs_action(state: dict[str, Any], seat: int) -> bool:
    hand = state["hand"]
    stacks = state["stacks"]
    if seat not in hand["players"] or hand["folded"][seat] or stacks[seat] == 0:
        return False
    top = max(hand["committed"])
    others = [s for s in _live(hand) if s != seat and stacks[s] > 0]
    if hand["committed"][seat] < top:
        return True
    # Matched: only an unacted player with someone left to bet against decides.
    return not hand["acted"][seat] and bool(others)


def _progress(
    rules: dict[str, Any], seed: str, state: dict[str, Any], *, start: int | None = None
) -> None:
    """After a change: award an uncontested pot, close the street, or move on."""
    hand = state["hand"]
    live = _live(hand)
    if len(live) == 1:
        _award_uncontested(rules, seed, state, live[0])
        return
    order = hand["players"]
    first = start if start is not None else _after(order, hand["to_act"])
    seat = first
    for _ in range(len(order)):
        if _needs_action(state, seat):
            hand["to_act"] = seat
            return
        seat = _after(order, seat)
    _close_street(rules, seed, state)


def to_call(state: dict[str, Any], seat: int | None = None) -> int:
    hand = state["hand"]
    me = hand["to_act"] if seat is None else seat
    return max(hand["committed"]) - hand["committed"][me]


def _may_raise(state: dict[str, Any], seat: int) -> bool:
    hand = state["hand"]
    stacks = state["stacks"]
    owed = to_call(state, seat)
    others = [s for s in _live(hand) if s != seat and stacks[s] > 0]
    if stacks[seat] <= owed or not others:
        return False
    # Facing a short all-in after acting: call or fold only.
    return not (hand["acted"][seat] and owed > 0)


def legal_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """What the seat to act may do now, with the bet/raise ranges."""
    if state["finished"] or state["hand"] is None:
        return []
    hand = state["hand"]
    me = hand["to_act"]
    stack = state["stacks"][me]
    owed = to_call(state)
    out: list[dict[str, Any]] = []
    if owed > 0:
        out.append({"type": "fold"})
        out.append({"type": "call", "amount": min(owed, stack)})
    else:
        out.append({"type": "check"})
    raising = _may_raise(state, me)
    if raising:
        top = max(hand["committed"])
        most = hand["committed"][me] + stack
        least = min(top + hand["min_raise"], most)
        out.append({"type": "bet" if owed == 0 else "raise", "min_to": least, "max_to": most})
    if stack > 0 and (raising or stack <= owed):
        out.append({"type": "all_in", "amount": stack})
    return out


# ── actions ───────────────────────────────────────────────────────────────


def apply(
    rules: dict[str, Any], seed: str, state: dict[str, Any], seat: int, action: dict[str, Any]
) -> dict[str, Any]:
    """A new state with ``action`` by ``seat``; raises IllegalAction.

    Bet and raise amounts are "to" amounts: the seat's total for this street.
    """
    if state["finished"]:
        raise IllegalAction("the match is over")
    hand = state["hand"]
    if seat != hand["to_act"]:
        raise IllegalAction("not your turn")
    kind = action.get("type")
    if kind not in ACTIONS and kind != "timeout":
        raise IllegalAction("unknown action")
    state = copy.deepcopy(state)
    hand = state["hand"]
    timed_out = kind == "timeout"
    if timed_out:
        state["timeouts"][seat] += 1
        kind = "check" if to_call(state) == 0 else "fold"
        if state["timeouts"][seat] >= rules["max_timeouts"]:
            hand["actions"].append({"seat": seat, "type": "timeout_forfeit"})
            _leave_now(state, seat, "forfeit_timeouts")
            hand["folded"][seat] = True
            _progress(rules, seed, state)
            return state
    else:
        state["timeouts"][seat] = 0
    owed = to_call(state)
    stack = state["stacks"][seat]
    mine = hand["committed"][seat]
    top = max(hand["committed"])
    record: dict[str, Any] = {"seat": seat, "type": kind}
    if timed_out:
        record["timeout"] = True
    if kind == "fold":
        if owed == 0:
            raise IllegalAction("nothing to call: check instead of folding")
        hand["folded"][seat] = True
        hand["actions"].append(record)
        _progress(rules, seed, state)
        return state
    if kind == "check":
        if owed != 0:
            raise IllegalAction("there is a bet to call")
        hand["acted"][seat] = True
    elif kind == "call":
        if owed == 0:
            raise IllegalAction("nothing to call")
        pay = min(owed, stack)
        _put(state, seat, pay)
        record["amount"] = pay
        hand["acted"][seat] = True
    else:
        if kind == "all_in":
            if stack <= 0:
                raise IllegalAction("no chips left")
            target = mine + stack
            if target <= top:  # an all-in call
                _put(state, seat, stack)
                record.update(type="call", amount=stack, all_in=True)
                hand["acted"][seat] = True
                hand["actions"].append(record)
                _progress(rules, seed, state)
                return state
        else:
            # "bet" and "raise" are the same move here: a "to" amount above the
            # current bet (the big blind's preflop option is a raise to).
            target = action.get("amount")
            if not isinstance(target, int) or isinstance(target, bool):
                raise IllegalAction("bet and raise take an integer 'to' amount")
        if not _may_raise(state, seat):
            raise IllegalAction("no raise here: call or fold")
        if target - mine > stack:
            raise IllegalAction("more than your stack")
        all_in = target - mine == stack
        least = top + hand["min_raise"]
        if target < least and not all_in:
            raise IllegalAction(f"the smallest bet or raise is to {least}")
        if target <= top:
            raise IllegalAction("a bet or raise must go above the current bet")
        increase = target - top
        if increase >= hand["min_raise"]:
            hand["min_raise"] = increase
            hand["acted"] = [False] * state["size"]  # a full raise reopens the action
        _put(state, seat, target - mine)
        hand["acted"][seat] = True
        record.update(type="all_in" if all_in else ("bet" if top == 0 else "raise"), to=target)
    hand["actions"].append(record)
    _progress(rules, seed, state)
    return state


def _put(state: dict[str, Any], seat: int, amount: int) -> None:
    hand = state["hand"]
    state["stacks"][seat] -= amount
    hand["committed"][seat] += amount
    hand["contrib"][seat] += amount


def _close_street(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    hand = state["hand"]
    while True:
        if hand["street"] == "river":
            _showdown(rules, seed, state)
            return
        nxt = {"preflop": "flop", "flop": "turn", "turn": "river"}[hand["street"]]
        hand["street"] = nxt
        hand["board"] = hand["runout"][: {"flop": 3, "turn": 4, "river": 5}[nxt]]
        hand["committed"] = [0] * state["size"]
        hand["acted"] = [False] * state["size"]
        hand["min_raise"] = hand["blinds"][1]
        able = [s for s in _live(hand) if state["stacks"][s] > 0]
        if len(able) >= 2:
            first = _after(hand["players"], hand["button"])
            seat = first
            for _ in range(len(hand["players"])):
                if _needs_action(state, seat):
                    hand["to_act"] = seat
                    return
                seat = _after(hand["players"], seat)
        # Nobody (or one player) can still bet: run the board out.


def _summary(hand: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "no": hand["no"],
        "button": hand["button"],
        "blinds": hand["blinds"],
        "players": hand["players"],
        "deck_sha256": hand["deck_sha256"],
        "board": hand["board"],
        "contrib": hand["contrib"],
        "actions": hand["actions"],
        **extra,
    }


def _clockwise_from_button(hand: dict[str, Any], seats: list[int]) -> list[int]:
    order = []
    seat = hand["button"]
    for _ in range(len(hand["players"])):
        seat = _after(hand["players"], seat)
        if seat in seats and seat not in order:
            order.append(seat)
    return order


def _award_uncontested(rules: dict[str, Any], seed: str, state: dict[str, Any], winner: int):
    hand = state["hand"]
    pot = sum(hand["contrib"])
    state["stacks"][winner] += pot
    won = [pot if s == winner else 0 for s in range(state["size"])]
    state["hands"].append(_summary(hand, ended="fold", won=won))
    _next(rules, seed, state)


def _showdown(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    hand = state["hand"]
    size = state["size"]
    contrib = hand["contrib"]
    live = _live(hand)
    scores: dict[int, tuple[int, ...]] = {}
    shown: list[dict[str, Any] | None] = [None] * size
    for seat in live:
        score, five = best_hand(hand["hole"][seat] + hand["board"])
        scores[seat] = score
        shown[seat] = {"score": list(score), "category": CATEGORIES[score[0]], "best": five}
    won = [0] * size
    previous = 0
    for level in sorted({c for c in contrib if c > 0}):
        part = sum(min(c, level) - min(c, previous) for c in contrib)
        previous = level
        eligible = [s for s in live if contrib[s] >= level]
        if not eligible:  # only folded money at this level: the deepest live player
            deepest = max(contrib[s] for s in live)
            eligible = [s for s in live if contrib[s] == deepest]
        best = max(scores[s] for s in eligible)
        winners = [s for s in eligible if scores[s] == best]
        share, odd = divmod(part, len(winners))
        for s in winners:
            won[s] += share
        for s in _clockwise_from_button(hand, winners)[:odd]:
            won[s] += 1
    for s in range(size):
        state["stacks"][s] += won[s]
    hole = [hand["hole"][s] if s in live else None for s in range(size)]
    state["hands"].append(_summary(hand, ended="showdown", hole=hole, showdown=shown, won=won))
    _next(rules, seed, state)


def _leave_now(state: dict[str, Any], seat: int, reason: str) -> None:
    """Conceded or timed out: out of a tournament after this hand (its chips
    are shared among the others), up from a cash table (keeps its chips)."""
    if state["mode"] == "cash":
        if seat not in state["standing"]:
            state["standing"].append(seat)
    elif seat not in state["forfeits"]:
        state["forfeits"].append(seat)
        state["out_reason"] = reason


def _next(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    stacks = state["stacks"]
    hand = state["hand"]
    state["hand"] = None
    for seat in state["forfeits"]:
        if state["status"][seat] != "active":
            continue
        others = [
            s
            for s in range(state["size"])
            if state["status"][s] == "active"
            and stacks[s] > 0
            and s != seat
            and s not in state["forfeits"]
        ]
        if stacks[seat] and others:
            share, odd = divmod(stacks[seat], len(others))
            for s in others:
                stacks[s] += share
            for s in _clockwise_from_button(hand, others)[:odd] if hand else others[:odd]:
                stacks[s] += 1
            stacks[seat] = 0
        state["status"][seat] = "out"
        state["eliminated"].append(seat)
    state["forfeits"] = []
    for seat in hand["players"] if hand else []:
        if state["status"][seat] == "active" and stacks[seat] == 0:
            state["status"][seat] = "out"
            state["eliminated"].append(seat)
            if state["out_reason"] is None:
                state["out_reason"] = "bust"
    for seat in state["standing"]:
        if state["status"][seat] == "active":
            state["status"][seat] = "left"
    stood = bool(state["standing"])
    state["standing"] = []
    active = [s for s in range(state["size"]) if state["status"][s] == "active" and stacks[s] > 0]
    waiting = [s for s in range(state["size"]) if state["pending"][s] is not None]
    if state["mode"] == "sng" and len(active) == 1:
        _finish(state, reason=state["out_reason"] or "bust")
        return
    if state["mode"] == "cash" and len(active) + len(waiting) < 2:
        _finish(state, reason="stood_up" if stood or "left" in state["status"] else "bust")
        return
    # The hand limit ends tournaments only; a cash table runs while two sit.
    if state["mode"] == "sng" and state["hand_no"] >= rules["max_hands"]:
        top = max(stacks[s] for s in active)
        leaders = [s for s in active if stacks[s] == top]
        _finish(state, reason="hand_limit" if len(leaders) == 1 else "hand_limit_draw")
        return
    state["out_reason"] = None if state["mode"] == "cash" else state["out_reason"]
    _start_hand(rules, seed, state)


def _finish(state: dict[str, Any], *, reason: str) -> None:
    stacks = state["stacks"]
    seated = [s for s in range(state["size"]) if state["status"][s] != "empty"]
    if state["mode"] == "sng":
        alive = sorted(
            (s for s in seated if state["status"][s] == "active" and stacks[s] > 0),
            key=lambda s: (-stacks[s], s),
        )
        state["ranking"] = alive + list(reversed(state["eliminated"]))
    else:
        state["ranking"] = sorted(seated, key=lambda s: (-stacks[s], s))
    top = state["ranking"][0]
    tie = any(stacks[s] == stacks[top] for s in state["ranking"][1:]) and (
        state["mode"] == "cash" or reason.startswith("hand_limit")
    )
    state["winner"] = None if tie else top
    state["finished"] = True
    state["reason"] = reason
    state["hand"] = None


def sit(state: dict[str, Any], seat: int, chips: int) -> dict[str, Any]:
    """Cash tables: a player takes a free seat (empty, or left by someone who
    stood up or busted); dealt in from the next hand. Chips a previous
    occupant left on the seat leave the table with that player."""
    if state["mode"] != "cash" or state["finished"]:
        raise IllegalAction("seats are taken only at an open cash table")
    if (
        not 0 <= seat < state["size"]
        or state["status"][seat] not in ("empty", "left", "out")
        or (state["pending"][seat] is not None)
    ):
        raise IllegalAction("that seat is not free")
    state = copy.deepcopy(state)
    state["cashed_out"] = state.get("cashed_out", 0) + state["stacks"][seat]
    state["stacks"][seat] = 0
    state["status"][seat] = "empty"
    state["pending"][seat] = int(chips)
    return state


def stand_up(state: dict[str, Any], seat: int) -> dict[str, Any]:
    """Cash tables: leave after the hand in progress, keeping the chips."""
    state = copy.deepcopy(state)
    if state["finished"]:
        return state
    if state["pending"][seat] is not None:  # sat down, never dealt in
        state["buy_ins"][seat] += 0
        state["pending"][seat] = None
        return state
    if state["status"][seat] == "active" and seat not in state["standing"]:
        state["standing"].append(seat)
    return state


def concede(
    state: dict[str, Any], seat: int, rules: dict[str, Any] | None = None, seed: str | None = None
) -> dict[str, Any]:
    """Tournaments: out now. In a hand the seat folds; its remaining chips go
    to the others when the hand ends."""
    state = copy.deepcopy(state)
    if state["finished"] or state["status"][seat] != "active":
        return state
    _leave_now(state, seat, "conceded")
    hand = state["hand"]
    if hand is not None and seat in hand["players"] and not hand["folded"][seat]:
        hand["folded"][seat] = True
        hand["actions"].append({"seat": seat, "type": "concede"})
        if rules is None or seed is None:
            raise ValueError("conceding during a hand needs the rules and the seed")
        if hand["to_act"] == seat:
            _progress(rules, seed, state)
        else:  # the seat to act still acts
            _progress(rules, seed, state, start=hand["to_act"])
    return state


def replay(
    rules: dict[str, Any],
    seed: str,
    log: list[dict[str, Any]],
    *,
    players: int = 2,
    seats: int | None = None,
    mode: str = "sng",
) -> dict[str, Any]:
    """Recompute a match from its seed and action log: each entry is a seat
    and an action, or one of concede / stand_up / sit {chips}."""
    state = new_match(rules, seed, players=players, seats=seats, mode=mode)
    for entry in log:
        action = entry["action"]
        kind = action.get("type")
        if kind == "concede":
            state = concede(state, entry["seat"], rules, seed)
        elif kind == "stand_up":
            state = stand_up(state, entry["seat"])
        elif kind == "sit":
            state = sit(state, entry["seat"], int(action["chips"]))
        else:
            state = apply(rules, seed, state, entry["seat"], action)
    return state


def seat_view(state: dict[str, Any], seat: int | None) -> dict[str, Any]:
    """What one seat may see: its own hole cards, never the others' or the
    cards still to come."""
    hand = state["hand"]
    view: dict[str, Any] = {
        "size": state["size"],
        "mode": state["mode"],
        "hand_no": state["hand_no"],
        "stacks": state["stacks"],
        "status": state["status"],
        "buy_ins": state["buy_ins"],
        "pending": state["pending"],
        "cashed_out": state.get("cashed_out", 0),
        "finished": state["finished"],
        "winner": state["winner"],
        "ranking": state["ranking"],
        "reason": state["reason"],
        "standing": state["standing"][0] if state["standing"] else None,
        "leaving": state["standing"],
        "timeouts": state["timeouts"],
        "eliminated": state["eliminated"],
        "hands_played": len(state["hands"]),
        "last_hand": state["hands"][-1] if state["hands"] else None,
    }
    if hand is not None:
        view["hand"] = {
            "no": hand["no"],
            "button": hand["button"],
            "sb": hand["sb"],
            "bb": hand["bb"],
            "players": hand["players"],
            "folded": hand["folded"],
            "blinds": hand["blinds"],
            "street": hand["street"],
            "board": hand["board"],
            "committed": hand["committed"],
            "pot": sum(hand["contrib"]),
            "to_act": hand["to_act"],
            "to_call": to_call(state, seat) if seat in hand["players"] else 0,
            "my_cards": hand["hole"][seat]
            if seat is not None and 0 <= seat < state["size"]
            else None,
            "actions": hand["actions"],
            "legal": legal_actions(state) if seat == hand["to_act"] else [],
        }
    return view
