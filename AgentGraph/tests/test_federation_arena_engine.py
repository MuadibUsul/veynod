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
    state = e.concede(e.new_match(RULES, SEED), 1, RULES, SEED)
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


# ── 3 to 6 seats ──────────────────────────────────────────────────────────


def _total_chips(state) -> int:
    pot = sum(state["hand"]["contrib"]) if state["hand"] else 0
    pending = sum(c for c in state["pending"] if c is not None)
    return sum(state["stacks"]) + pot + pending + state.get("cashed_out", 0)


def _play_multi(seed, rng, players, mode, size=None, limit=20000):
    state = e.new_match(RULES, seed, players=players, seats=size, mode=mode)
    log = []
    for _ in range(limit):
        if state["finished"]:
            break
        seat = state["hand"]["to_act"]
        action = _random_action(rng, state)
        state = e.apply(RULES, seed, state, seat, action)
        log.append({"seat": seat, "action": action})
        assert _total_chips(state) == sum(state["buy_ins"]) + sum(
            c for c in state["pending"] if c is not None
        )
        assert min(state["stacks"]) >= 0
    return state, log


@pytest.mark.parametrize("players", [3, 4, 5, 6])
@pytest.mark.parametrize("mode", ["sng", "cash"])
@pytest.mark.parametrize("n", range(6))
def test_multi_seat_matches_conserve_finish_and_replay(players, mode, n):
    rng = random.Random(1000 * players + n + (7 if mode == "cash" else 0))
    seed = e.combined_seed(f"{n:064x}", [f"{players:064x}"])
    state, log = _play_multi(seed, rng, players, mode, size=6)
    assert state["finished"]
    assert sum(state["stacks"]) == players * RULES["starting_stack"]
    ranking = state["ranking"]
    assert sorted(ranking) == list(range(players))  # every player is placed once
    if mode == "sng" and state["reason"] in ("bust", "conceded", "forfeit_timeouts"):
        assert state["stacks"][ranking[0]] == players * RULES["starting_stack"]
        assert state["winner"] == ranking[0]
    again = e.replay(RULES, seed, log, players=players, seats=6, mode=mode)
    assert e.sha256_hex(again) == e.sha256_hex(state)


def test_three_handed_positions():
    state = e.new_match(RULES, SEED, players=3)
    hand = state["hand"]
    assert (hand["button"], hand["sb"], hand["bb"]) == (0, 1, 2)
    assert hand["committed"] == [0, 5, 10]
    assert hand["to_act"] == 0  # left of the big blind
    state = e.apply(RULES, SEED, state, 0, {"type": "call"})
    state = e.apply(RULES, SEED, state, 1, {"type": "call"})
    state = e.apply(RULES, SEED, state, 2, {"type": "check"})
    assert state["hand"]["street"] == "flop" and state["hand"]["to_act"] == 1  # left of button
    # The button moves on.
    for seat in (1, 2, 0):
        state = e.apply(RULES, SEED, state, seat, {"type": "check"})
    for _ in range(2):
        for seat in (1, 2, 0):
            state = e.apply(RULES, SEED, state, seat, {"type": "check"})
    assert state["hand"]["no"] == 2 and state["hand"]["button"] == 1


def test_side_pots_go_to_the_right_players():
    """A short all-in only wins what it covered; the rest goes to the best of
    the deeper stacks."""
    checked = 0
    for n in range(300):
        seed = e.combined_seed(f"{n:064x}", ["ab" * 32])
        state = e.new_match(RULES, seed, players=3)
        # Hand 1: seat 0 folds; seats 1 and 2 check down (seat 0 keeps 1000).
        state = e.apply(RULES, seed, state, 0, {"type": "fold"})
        state = e.apply(RULES, seed, state, 1, {"type": "call"})
        state = e.apply(RULES, seed, state, 2, {"type": "check"})
        while state["hand"]["no"] == 1:
            seat = state["hand"]["to_act"]
            state = e.apply(RULES, seed, state, seat, {"type": "check"})
        stacks = list(state["stacks"])
        if len(set(stacks)) < 2:
            continue
        # Hand 2: everyone all in.
        while state["hand"] and state["hand"]["no"] == 2:
            seat = state["hand"]["to_act"]
            state = e.apply(RULES, seed, state, seat, {"type": "all_in"})
        last = state["hands"][-1]
        contrib, won = last["contrib"], last["won"]
        assert sum(won) == sum(contrib) == 3000
        short = min(range(3), key=lambda s: contrib[s])
        assert won[short] <= 3 * contrib[short]
        deep = [s for s in range(3) if s != short]
        score = {s: tuple(last["showdown"][s]["score"]) for s in range(3)}
        if score[short] > max(score[s] for s in deep):
            assert won[short] == 3 * contrib[short]
            mid = sorted(contrib)[1]
            deepest = max(range(3), key=lambda s: contrib[s])
            uncalled = max(contrib) - mid  # only the deepest stack put this in: back to it
            if score[deep[0]] != score[deep[1]]:
                best_deep = max(deep, key=lambda s: score[s])
                other = deep[0] if best_deep == deep[1] else deep[1]
                side = 2 * (mid - contrib[short])
                assert won[best_deep] == side + (uncalled if best_deep == deepest else 0)
                assert won[other] == (uncalled if other == deepest else 0)
            checked += 1
        if checked >= 3:
            return
    assert checked > 0


def test_cash_seats_open_and_close():
    state = e.new_match(RULES, SEED, players=2, seats=6, mode="cash")
    state = e.sit(state, 3, 1000)
    with pytest.raises(e.IllegalAction):
        e.sit(state, 3, 1000)  # taken
    with pytest.raises(e.IllegalAction):
        e.sit(state, 0, 1000)
    assert state["hand"]["players"] == [0, 1]  # dealt in from the next hand
    state = e.apply(RULES, SEED, state, state["hand"]["to_act"], {"type": "fold"})
    assert state["hand"]["players"] == [0, 1, 3] and state["buy_ins"][3] == 1000
    # Seat 0 stands up: it plays this hand out, then leaves with its chips.
    state = e.stand_up(state, 0)
    while state["hand"]["no"] == 2:
        seat = state["hand"]["to_act"]
        legal = {a["type"] for a in e.legal_actions(state)}
        state = e.apply(RULES, SEED, state, seat, {"type": "check" if "check" in legal else "fold"})
    assert state["status"][0] == "left" and state["hand"]["players"] == [1, 3]
    state = e.stand_up(state, 1)
    while not state["finished"]:
        seat = state["hand"]["to_act"]
        legal = {a["type"] for a in e.legal_actions(state)}
        state = e.apply(RULES, SEED, state, seat, {"type": "check" if "check" in legal else "fold"})
    assert state["reason"] == "stood_up"
    assert sum(state["stacks"]) == sum(state["buy_ins"]) == 3000
    assert sorted(state["ranking"]) == [0, 1, 3]


def test_tournament_places_follow_eliminations():
    state = e.new_match(RULES, SEED, players=4)
    state = e.concede(state, 2, RULES, SEED)
    while state["hand"] and state["hand"]["no"] == 1:
        seat = state["hand"]["to_act"]
        legal = {a["type"] for a in e.legal_actions(state)}
        state = e.apply(RULES, SEED, state, seat, {"type": "check" if "check" in legal else "call"})
    assert state["status"][2] == "out" and state["eliminated"] == [2]
    assert _total_chips(state) == 4000 and state["stacks"][2] == 0
    for seat in (0, 3):
        state = e.concede(state, seat, RULES, SEED)
    while not state["finished"]:
        seat = state["hand"]["to_act"]
        legal = {a["type"] for a in e.legal_actions(state)}
        state = e.apply(RULES, SEED, state, seat, {"type": "check" if "check" in legal else "call"})
    assert state["winner"] == 1 and state["ranking"][0] == 1
    assert state["ranking"][-1] == 2 and state["stacks"][1] == 4000


def test_cash_blinds_stay_and_there_is_no_hand_limit():
    state = e.new_match(RULES, SEED, players=2, seats=6, mode="cash")
    for _ in range(20000):
        if state["finished"] or state["hand_no"] > RULES["max_hands"] + 5:
            break
        seat = state["hand"]["to_act"]
        legal = {a["type"] for a in e.legal_actions(state)}
        state = e.apply(RULES, SEED, state, seat, {"type": "check" if "check" in legal else "call"})
    assert not state["finished"] and state["hand_no"] > RULES["max_hands"]
    assert state["hand"]["blinds"] == RULES["blinds"][0]


def test_a_left_chair_takes_a_new_player_and_chips_stay_counted():
    state = e.new_match(RULES, SEED, players=3, seats=6, mode="cash")
    state = e.stand_up(state, 2)
    while state["hand"]["no"] == 1:
        seat = state["hand"]["to_act"]
        legal = {a["type"] for a in e.legal_actions(state)}
        state = e.apply(RULES, SEED, state, seat, {"type": "check" if "check" in legal else "call"})
    assert state["status"][2] == "left"
    left_with = state["stacks"][2]
    state = e.sit(state, 2, 1000)
    assert state["cashed_out"] == left_with and state["pending"][2] == 1000
    assert _total_chips(state) == sum(state["buy_ins"]) + 1000
