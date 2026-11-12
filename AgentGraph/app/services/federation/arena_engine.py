"""Heads-up no-limit hold'em rules engine for the Agent Poker Arena (R2).

Contract: docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md §5.

Pure and deterministic: no database, no clock, no randomness of its own.
The deck of every hand comes from one seed (the match's combined
commit/reveal seed) and the hand number, so anyone holding the seeds and the
action log can replay the match and get the same result (``replay``).

Chips (ARENA_CHIP) exist only inside this state. They have no ledger, no
price and no conversion to anything: the match result is only who won.
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
    "version": "hu-nlhe-v1",
    "unit": "ARENA_CHIP",
    "starting_stack": 1000,
    # [small blind, big blind] per level; the last level repeats.
    "blinds": [[5, 10], [10, 20], [15, 30], [25, 50], [50, 100], [100, 200], [200, 400]],
    "hands_per_level": 8,
    "max_hands": 80,  # then the chip leader wins; equal stacks are a draw
    "turn_seconds": 60,
    "max_timeouts": 3,  # this many timeouts in a row forfeit the match
}

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


def new_match(rules: dict[str, Any], seed: str) -> dict[str, Any]:
    state = {
        "rules_hash": rules_hash(rules),
        "stacks": [rules["starting_stack"], rules["starting_stack"]],
        "hand_no": 0,
        "button": 1,  # flips to seat 0 for hand 1
        "hand": None,
        "hands": [],  # summaries of finished hands
        "timeouts": [0, 0],
        "finished": False,
        "winner": None,  # 0, 1, or None for a draw
        "reason": None,
    }
    _start_hand(rules, seed, state)
    return state


def _blinds(rules: dict[str, Any], hand_no: int) -> tuple[int, int]:
    level = min((hand_no - 1) // rules["hands_per_level"], len(rules["blinds"]) - 1)
    sb, bb = rules["blinds"][level]
    return int(sb), int(bb)


def _start_hand(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    state["hand_no"] += 1
    state["button"] = 1 - state["button"]
    no, button = state["hand_no"], state["button"]
    other = 1 - button
    deck = shuffled_deck(seed, no)
    sb, bb = _blinds(rules, no)
    stacks = state["stacks"]
    committed = [0, 0]
    # Heads-up: the button posts the small blind and acts first preflop.
    posted_sb = min(sb, stacks[button])
    posted_bb = min(bb, stacks[other])
    committed[button], committed[other] = posted_sb, posted_bb
    stacks[button] -= posted_sb
    stacks[other] -= posted_bb
    state["hand"] = {
        "no": no,
        "button": button,
        "blinds": [sb, bb],
        "deck_sha256": hashlib.sha256(",".join(deck).encode()).hexdigest(),
        "hole": [deck[0:2], deck[2:4]],
        # burn 4, flop 5-7, burn 8, turn 9, burn 10, river 11
        "runout": [deck[5], deck[6], deck[7], deck[9], deck[11]],
        "board": [],
        "street": "preflop",
        "committed": committed,  # this street
        "contrib": list(committed),  # whole hand
        "min_raise": bb,
        "acted": [False, False],
        "to_act": button,
        "actions": [],
    }
    # A blind that put a player all in for no more than the other's blind
    # leaves nothing to decide: the board runs out.
    for seat in (button, other):
        if stacks[seat] == 0 and committed[seat] <= committed[1 - seat]:
            _close_street(rules, seed, state)
            return


def to_call(state: dict[str, Any]) -> int:
    hand = state["hand"]
    me = hand["to_act"]
    return max(hand["committed"]) - hand["committed"][me]


def legal_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """What the seat to act may do now, with the bet/raise ranges."""
    if state["finished"] or state["hand"] is None:
        return []
    hand = state["hand"]
    me = hand["to_act"]
    stack = state["stacks"][me]
    opp_stack = state["stacks"][1 - me]
    owed = to_call(state)
    out: list[dict[str, Any]] = []
    if owed > 0:
        out.append({"type": "fold"})
        out.append({"type": "call", "amount": min(owed, stack)})
    else:
        out.append({"type": "check"})
    # Nobody bets into a player who is all in.
    if stack > owed and opp_stack > 0:
        top = max(hand["committed"])
        mine = hand["committed"][me]
        most = mine + stack
        least = min(top + hand["min_raise"], most)
        out.append({"type": "bet" if owed == 0 else "raise", "min_to": least, "max_to": most})
    # All in is a bet or raise only while the other player still has chips;
    # otherwise it is an all-in call, offered only when it is one.
    if stack > 0 and (opp_stack > 0 or stack <= owed):
        out.append({"type": "all_in", "amount": stack})
    return out


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
            _finish(state, winner=1 - seat, reason="forfeit_timeouts")
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
        hand["actions"].append(record)
        _award_fold(rules, seed, state, winner=1 - seat)
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
            target = mine + stack
            if stack <= 0:
                raise IllegalAction("no chips left")
            if target <= top:  # an all-in call
                _put(state, seat, stack)
                record.update(type="call", amount=stack, all_in=True)
                hand["acted"][seat] = True
                hand["actions"].append(record)
                return _after_action(rules, seed, state)
        else:
            target = action.get("amount")
            # "bet" and "raise" are the same move here: a "to" amount above
            # the current bet (the big blind's preflop option is a raise to).
            if not isinstance(target, int) or isinstance(target, bool):
                raise IllegalAction("bet and raise take an integer 'to' amount")
        if state["stacks"][1 - seat] == 0:
            raise IllegalAction("the other player is all in: call or fold")
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
            hand["acted"] = [False, False]  # a full raise reopens the action
        _put(state, seat, target - mine)
        hand["acted"][seat] = True
        record.update(type="all_in" if all_in else ("bet" if top == 0 else "raise"), to=target)
    hand["actions"].append(record)
    return _after_action(rules, seed, state)


def _put(state: dict[str, Any], seat: int, amount: int) -> None:
    hand = state["hand"]
    state["stacks"][seat] -= amount
    hand["committed"][seat] += amount
    hand["contrib"][seat] += amount


def _after_action(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> dict[str, Any]:
    hand = state["hand"]
    c = hand["committed"]
    stacks = state["stacks"]
    level = c[0] == c[1]
    # A player all in for less than the bet has nothing more to match.
    for seat in (0, 1):
        if stacks[seat] == 0 and c[seat] < c[1 - seat] and hand["acted"][1 - seat]:
            level = True
    done = level and (all(hand["acted"]) or 0 in stacks)
    if done:
        _close_street(rules, seed, state)
    else:
        hand["to_act"] = 1 - hand["to_act"]
        if stacks[hand["to_act"]] == 0:  # all in already: nothing to decide
            _close_street(rules, seed, state)
    return state


def _close_street(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    hand = state["hand"]
    stacks = state["stacks"]
    runout_all = 0 in stacks
    while True:
        street = hand["street"]
        if street == "river":
            _showdown(rules, seed, state)
            return
        nxt = {"preflop": "flop", "flop": "turn", "turn": "river"}[street]
        hand["street"] = nxt
        hand["board"] = hand["runout"][: {"flop": 3, "turn": 4, "river": 5}[nxt]]
        hand["committed"] = [0, 0]
        hand["acted"] = [False, False]
        hand["min_raise"] = hand["blinds"][1]
        hand["to_act"] = 1 - hand["button"]  # the big blind acts first after the flop
        if not runout_all:
            return


def _summary(hand: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "no": hand["no"],
        "button": hand["button"],
        "blinds": hand["blinds"],
        "deck_sha256": hand["deck_sha256"],
        "board": hand["board"],
        "contrib": hand["contrib"],
        "actions": hand["actions"],
        **extra,
    }


def _award_fold(rules: dict[str, Any], seed: str, state: dict[str, Any], winner: int) -> None:
    hand = state["hand"]
    pot = sum(hand["contrib"])
    state["stacks"][winner] += pot
    state["hands"].append(
        _summary(hand, ended="fold", won=[pot if s == winner else 0 for s in (0, 1)])
    )
    _next(rules, seed, state)


def _showdown(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    hand = state["hand"]
    contrib = hand["contrib"]
    matched = min(contrib)
    won = [contrib[0] - matched, contrib[1] - matched]  # an uncalled excess goes back
    pot = 2 * matched
    scores = []
    for seat in (0, 1):
        score, five = best_hand(hand["hole"][seat] + hand["board"])
        scores.append({"score": list(score), "category": CATEGORIES[score[0]], "best": five})
    if scores[0]["score"] > scores[1]["score"]:
        won[0] += pot
    elif scores[1]["score"] > scores[0]["score"]:
        won[1] += pot
    else:
        half = pot // 2
        odd = pot - 2 * half
        won[0] += half
        won[1] += half
        won[1 - hand["button"]] += odd  # the odd chip to the player out of position
    state["stacks"][0] += won[0]
    state["stacks"][1] += won[1]
    state["hands"].append(
        _summary(hand, ended="showdown", hole=hand["hole"], showdown=scores, won=won)
    )
    _next(rules, seed, state)


def _next(rules: dict[str, Any], seed: str, state: dict[str, Any]) -> None:
    stacks = state["stacks"]
    state["hand"] = None
    if stacks[0] == 0 or stacks[1] == 0:
        _finish(state, winner=0 if stacks[1] == 0 else 1, reason="bust")
        return
    if state["hand_no"] >= rules["max_hands"]:
        if stacks[0] == stacks[1]:
            _finish(state, winner=None, reason="hand_limit_draw")
        else:
            _finish(state, winner=0 if stacks[0] > stacks[1] else 1, reason="hand_limit")
        return
    _start_hand(rules, seed, state)


def _finish(state: dict[str, Any], *, winner: int | None, reason: str) -> None:
    if state["hand"] is not None:
        hand = state["hand"]
        # A forfeit mid-hand: chips in the pot go back before the result.
        state["stacks"][0] += hand["contrib"][0]
        state["stacks"][1] += hand["contrib"][1]
        state["hands"].append(_summary(hand, ended="forfeit", won=hand["contrib"]))
        state["hand"] = None
    state["finished"] = True
    state["winner"] = winner
    state["reason"] = reason


def concede(state: dict[str, Any], seat: int) -> dict[str, Any]:
    state = copy.deepcopy(state)
    if not state["finished"]:
        _finish(state, winner=1 - seat, reason="conceded")
    return state


def replay(rules: dict[str, Any], seed: str, log: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute a match from its seed and action log (each entry: seat and
    action, or ``{"seat": s, "action": {"type": "concede"}}``)."""
    state = new_match(rules, seed)
    for entry in log:
        action = entry["action"]
        if action.get("type") == "concede":
            state = concede(state, entry["seat"])
        else:
            state = apply(rules, seed, state, entry["seat"], action)
    return state


def seat_view(state: dict[str, Any], seat: int | None) -> dict[str, Any]:
    """What one seat may see: its own hole cards, never the opponent's or the
    cards still to come."""
    hand = state["hand"]
    view: dict[str, Any] = {
        "hand_no": state["hand_no"],
        "stacks": state["stacks"],
        "finished": state["finished"],
        "winner": state["winner"],
        "reason": state["reason"],
        "timeouts": state["timeouts"],
        "hands_played": len(state["hands"]),
        "last_hand": state["hands"][-1] if state["hands"] else None,
    }
    if hand is not None:
        view["hand"] = {
            "no": hand["no"],
            "button": hand["button"],
            "blinds": hand["blinds"],
            "street": hand["street"],
            "board": hand["board"],
            "committed": hand["committed"],
            "pot": sum(hand["contrib"]),
            "to_act": hand["to_act"],
            "to_call": to_call(state),
            "my_cards": hand["hole"][seat] if seat in (0, 1) else None,
            "actions": hand["actions"],
            "legal": legal_actions(state) if seat == hand["to_act"] else [],
        }
    return view
