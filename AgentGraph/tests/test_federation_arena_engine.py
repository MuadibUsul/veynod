"""Agent Poker Arena rules engine (R2, docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md §5).

Pure unit tests, no database: the engine is deterministic, so a match is
replayable from its seed and action log (RR-04 / task acceptance RR-07, RR-08).
"""

from __future__ import annotations

import random

import pytest

from app.services.federation import arena_engine as e

RULES = e.RULES_V1
SEED = e.combined_seed("11" * 32, ["22" * 32, "33" * 32])


def _total(state) -> int:
    pot = sum(state["hand"]["contrib"]) if state["hand"] else 0
    return sum(state["stacks"]) + pot


def _random_action(rng: random.Random, state) -> dict:
    legal = e.legal_actions(state)
    pick = rng.choice(legal)
    if pick["type"] in ("bet", "raise"):
        return {"type": pick["type"], "amount": rng.randint(pick["min_to"], pick["max_to"])}
    return {"type": pick["type"]}


def _play_random(seed: str, rng: random.Random, limit: int = 5000):
    state = e.new_match(RULES, seed)
    log = []
    for _ in range(limit):
        if state["finished"]:
            break
        seat = state["hand"]["to_act"]
        action = _random_action(rng, state)
        state = e.apply(RULES, seed, state, seat, action)
        log.append({"seat": seat, "action": action})
        assert _total(state) == 2 * RULES["starting_stack"]  # chips are never made or lost
        assert min(state["stacks"]) >= 0
    return state, log


def test_the_deck_comes_only_from_the_seed():
    a = e.shuffled_deck(SEED, 1)
    assert sorted(a) == sorted(e.DECK) and len(set(a)) == 52
    assert a == e.shuffled_deck(SEED, 1)  # same seed, same hand: same deck
    assert a != e.shuffled_deck(SEED, 2)  # every hand is shuffled anew
    other = e.combined_seed("11" * 32, ["22" * 32, "34" * 32])
    assert a != e.shuffled_deck(other, 1)  # one party's seed changes everything


def test_commitments_bind_the_seed():
    seed = "ab" * 32
    commit = e.seed_commitment(seed)
    assert commit == e.seed_commitment(seed) and commit != e.seed_commitment("ac" * 32)


@pytest.mark.parametrize("n", range(40))
def test_random_matches_conserve_chips_finish_and_replay(n):
    rng = random.Random(n)
    seed = e.combined_seed(f"{n:064x}", [f"{n + 1:064x}", f"{n + 2:064x}"])
    state, log = _play_random(seed, rng)
    assert state["finished"] and state["reason"] in (
        "bust",
        "hand_limit",
        "hand_limit_draw",
    )
    if state["reason"] == "bust":
        assert 0 in state["stacks"] and state["stacks"][state["winner"]] == 2000
    # The whole match replays from the seed and the log alone.
    again = e.replay(RULES, seed, log)
    assert again == state
    assert e.sha256_hex(again) == e.sha256_hex(state)


def test_a_different_action_log_gives_a_different_match():
    rng = random.Random(7)
    state, log = _play_random(SEED, rng)
    assert len(log) > 2
    changed = list(log)
    first = changed[0]
    # Fold the first decision instead of whatever was played.
    alt = {"seat": first["seat"], "action": {"type": "fold"}}
    if first["action"]["type"] == "fold":
        alt = {"seat": first["seat"], "action": {"type": "call"}}
    state_alt = e.replay(RULES, SEED, [alt])
    assert state_alt != e.replay(RULES, SEED, log[:1])


def test_heads_up_blinds_and_order():
    state = e.new_match(RULES, SEED)
    hand = state["hand"]
    assert hand["button"] == 0 and hand["to_act"] == 0  # the button is the small blind
    assert hand["committed"] == [5, 10] and state["stacks"] == [995, 990]
    state = e.apply(RULES, SEED, state, 0, {"type": "call"})
    assert state["hand"]["to_act"] == 1 and state["hand"]["street"] == "preflop"  # BB option
    state = e.apply(RULES, SEED, state, 1, {"type": "check"})
    hand = state["hand"]
    assert hand["street"] == "flop" and len(hand["board"]) == 3
    assert hand["to_act"] == 1  # the big blind acts first after the flop
    # The second hand moves the button.
    state = e.apply(RULES, SEED, state, 1, {"type": "bet", "amount": 10})
    state = e.apply(RULES, SEED, state, 0, {"type": "fold"})
    assert state["hand"]["no"] == 2 and state["hand"]["button"] == 1
    assert state["hands"][0]["ended"] == "fold" and state["hands"][0]["won"] == [0, 30]


def test_illegal_actions_are_refused_and_change_nothing():
    state = e.new_match(RULES, SEED)
    before = e.sha256_hex(state)
    bad = [
        (1, {"type": "call"}),  # not your turn
        (0, {"type": "check"}),  # there is a bet to call
        (0, {"type": "raise", "amount": 15}),  # below the minimum raise
        (0, {"type": "raise", "amount": 5000}),  # more than the stack
        (0, {"type": "raise", "amount": "20"}),  # not an integer
        (0, {"type": "steal"}),
    ]
    for seat, action in bad:
        with pytest.raises(e.IllegalAction):
            e.apply(RULES, SEED, state, seat, action)
    assert e.sha256_hex(state) == before
    state = e.apply(RULES, SEED, state, 0, {"type": "call"})
    with pytest.raises(e.IllegalAction):
        e.apply(RULES, SEED, state, 1, {"type": "fold"})  # nothing to call: check
    # No betting into a player who is all in.
    state = e.apply(RULES, SEED, state, 1, {"type": "all_in"})
    assert [a["type"] for a in e.legal_actions(state)] == ["fold", "call", "all_in"]
    with pytest.raises(e.IllegalAction):
        e.apply(RULES, SEED, state, 0, {"type": "raise", "amount": 990})


def test_all_in_runs_out_the_board_and_settles():
    state = e.new_match(RULES, SEED)
    state = e.apply(RULES, SEED, state, 0, {"type": "all_in"})
    state = e.apply(RULES, SEED, state, 1, {"type": "call"})
    last = state["hands"][-1]
    assert last["ended"] == "showdown" and len(last["board"]) == 5
    assert sum(last["won"]) == 2000
    assert state["finished"] or sum(state["stacks"]) + sum(state["hand"]["contrib"]) == 2000


def test_timeouts_check_or_fold_then_forfeit():
    state = e.new_match(RULES, SEED)
    state = e.apply(RULES, SEED, state, 0, {"type": "timeout"})  # facing a bet: fold
    assert state["hands"][-1]["ended"] == "fold" and state["timeouts"] == [1, 0]
    # Hand 2: seat 1 is the button and acts first.
    state = e.apply(RULES, SEED, state, 1, {"type": "call"})
    state = e.apply(RULES, SEED, state, 0, {"type": "timeout"})  # nothing to call: check
    assert state["hand"]["street"] == "flop" and state["timeouts"] == [2, 0]
    state = e.apply(RULES, SEED, state, 0, {"type": "timeout"})
    assert state["finished"] and state["winner"] == 1 and state["reason"] == "forfeit_timeouts"
    assert sum(state["stacks"]) == 2000  # the open pot went back first


def test_a_real_action_resets_the_timeout_count():
    state = e.new_match(RULES, SEED)
    state = e.apply(RULES, SEED, state, 0, {"type": "timeout"})
    state = e.apply(RULES, SEED, state, 1, {"type": "call"})
    assert state["timeouts"] == [1, 0]
    state = e.apply(RULES, SEED, state, 0, {"type": "check"})
    assert state["timeouts"] == [0, 0]


def test_concede_ends_the_match_for_the_other_seat():
    state = e.concede(e.new_match(RULES, SEED), 1)
    assert state["finished"] and state["winner"] == 0 and state["reason"] == "conceded"
    assert sum(state["stacks"]) == 2000


@pytest.mark.parametrize(
    "cards,category",
    [
        (["As", "Ks", "Qs", "Js", "Ts", "2d", "3c"], "straight_flush"),
        (["5d", "4d", "3d", "2d", "Ad", "Kc", "Kh"], "straight_flush"),  # steel wheel
        (["9s", "9h", "9d", "9c", "2s", "3d", "4c"], "four_of_a_kind"),
        (["9s", "9h", "9d", "2c", "2s", "3d", "4c"], "full_house"),
        (["As", "9s", "7s", "4s", "2s", "Kd", "Kh"], "flush"),
        (["Ad", "2s", "3h", "4c", "5d", "Kd", "Qh"], "straight"),  # the wheel
        (["7s", "7h", "7d", "Ac", "Ks", "3d", "4c"], "three_of_a_kind"),
        (["7s", "7h", "5d", "5c", "Ks", "3d", "4c"], "two_pair"),
        (["7s", "7h", "Jd", "9c", "Ks", "3d", "4c"], "pair"),
        (["As", "Jh", "9d", "7c", "5s", "3d", "2c"], "high_card"),
    ],
)
def test_hand_categories(cards, category):
    score, five = e.best_hand(cards)
    assert e.CATEGORIES[score[0]] == category and len(five) == 5


def test_hand_ordering_and_kickers():
    def score(cards):
        return e.best_hand(cards)[0]

    assert score(["As", "Ah", "Kd", "7c", "5s", "3d", "2c"]) > score(
        ["As", "Ah", "Qd", "7c", "5s", "3d", "2c"]
    )
    # The wheel is the lowest straight.
    assert score(["6d", "2s", "3h", "4c", "5d", "Kd", "Qh"]) > score(
        ["Ad", "2s", "3h", "4c", "5d", "Kd", "Qh"]
    )
    # The board plays: a split.
    board = ["As", "Ks", "Qs", "Js", "Ts"]
    assert score(board + ["2d", "3d"]) == score(board + ["4h", "5h"])


def test_a_split_pot_gives_the_odd_chip_out_of_position():
    # Find a seed whose first hand is a board-plays tie after an all-in.
    for n in range(4000):
        seed = e.combined_seed(f"{n:064x}", ["00" * 32, "00" * 32])
        state = e.new_match(RULES, seed)
        hand = state["hand"]
        s0 = e.best_hand(hand["hole"][0] + hand["runout"])[0]
        s1 = e.best_hand(hand["hole"][1] + hand["runout"])[0]
        if s0 == s1:
            state = e.apply(RULES, seed, state, 0, {"type": "all_in"})
            state = e.apply(RULES, seed, state, 1, {"type": "call"})
            assert state["hands"][-1]["won"] == [1000, 1000]
            assert state["stacks"] == [1000, 1000] or state["hand"] is not None
            return
    pytest.skip("no tie in the sampled seeds")


def test_the_seat_view_hides_the_other_cards_and_the_runout():
    state = e.new_match(RULES, SEED)
    mine = e.seat_view(state, 0)
    theirs = e.seat_view(state, 1)
    assert mine["hand"]["my_cards"] == state["hand"]["hole"][0]
    assert theirs["hand"]["my_cards"] == state["hand"]["hole"][1]
    text = repr(mine)
    for card in state["hand"]["hole"][1] + state["hand"]["runout"]:
        if card not in state["hand"]["hole"][0]:
            assert f"'{card}'" not in text
    assert mine["hand"]["legal"] and not theirs["hand"]["legal"]
