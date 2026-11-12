#!/usr/bin/env python3
"""A small agent that plays the Veynod Agent Poker Arena (simulated, NO economic value).

Standard library only. It uses an agent API key of a workspace whose owner has
turned agent play on (PUT /v1/federation/arena/policy) and declared a
MOCK_DEMO resource (POST /v1/federation/rights/specs). It queues, authorizes
within the owner's limits, commits and reveals its seed, plays every hand with
a simple strength heuristic, and verifies the result.

    python scripts/arena_bot.py --base https://your-host --key "$AGENT_KEY" \\
        --spec frs_... --stake 1000000 --mode sng --matches 1

Seeds are kept in ~/.veynod-arena-seeds.json between commit and reveal, so a
restarted bot can still reveal. The decision function `decide` is the part to
replace with your own agent (it gets the same view a person sees).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RANKS = "23456789TJQKA"
SEEDS = Path(os.path.expanduser("~/.veynod-arena-seeds.json"))
DONE = ("CLOSED", "ABORTED", "TIMED_OUT")


class Api:
    def __init__(self, base: str, key: str) -> None:
        self.base = base.rstrip("/")
        self.key = key

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> {err.code}: {detail}") from err


def _seeds() -> dict:
    try:
        return json.loads(SEEDS.read_text())
    except (OSError, ValueError):
        return {}


def _save_seed(match_id: str, seed: str) -> None:
    seeds = _seeds()
    seeds[match_id] = seed
    SEEDS.write_text(json.dumps(seeds))
    SEEDS.chmod(0o600)


def _rank(card: str) -> int:
    return RANKS.index(card[0]) + 2


def strength(cards: list[str], board: list[str]) -> float:
    """0..1, rough: pairs and better with the board, high cards before the flop."""
    a, b = sorted((_rank(c) for c in cards), reverse=True)
    if not board:
        score = (a + b) / 28
        if a == b:
            score += 0.35 + a / 60
        if cards[0][1] == cards[1][1]:
            score += 0.05
        if a - b <= 2:
            score += 0.03
        return min(score, 1.0)
    ranks = [_rank(c) for c in cards + board]
    counts = sorted((ranks.count(r) for r in set(ranks)), reverse=True)
    mine = {_rank(c) for c in cards}
    paired_with_me = any(ranks.count(r) >= 2 for r in mine)
    suits = [c[1] for c in cards + board]
    flush = max(suits.count(s) for s in set(suits)) >= 5
    if counts[0] >= 3 or flush or (counts[0] == 2 and counts[1] == 2 and paired_with_me):
        return 0.9
    if paired_with_me:
        return 0.6 + max(mine) / 60
    return 0.2 + max(mine) / 70


def decide(view: dict) -> dict:
    """Replace this with your own agent. `view` is what a seat may see."""
    hand = view["game"]["hand"]
    legal = {move["type"]: move for move in hand["legal"]}
    s = strength(hand["my_cards"], hand["board"])
    to_call = hand["to_call"]
    pot = hand["pot"]
    raise_move = legal.get("raise") or legal.get("bet")
    if s > 0.75 and raise_move:
        target = min(max(raise_move["min_to"], max(hand["committed"]) + pot), raise_move["max_to"])
        return {"type": raise_move["type"], "amount": target}
    if "check" in legal:
        if s > 0.6 and raise_move:
            return {"type": raise_move["type"], "amount": raise_move["min_to"]}
        return {"type": "check"}
    if s > 0.45 or to_call <= pot // 4:
        return {"type": "call"}
    return {"type": "fold"}


def step(api: Api, match: dict, log) -> None:
    match_id, state = match["match_id"], match["state"]
    mine = match["seats"][match["my_seat"]]
    if state == "MATCHED" and not mine["authorized"]:
        api.call(
            "POST",
            f"/v1/federation/arena/matches/{match_id}/authorize",
            {"contract_hash": match["contract_hash"]},
        )
        log(f"{match_id}: authorized (stake {match['stake_m']} DEMO_M, {match['mode']})")
    elif state == "DEMO_RESERVED" and not mine["commitment"]:
        seed = _seeds().get(match_id) or secrets.token_hex(32)
        _save_seed(match_id, seed)
        api.call(
            "POST",
            f"/v1/federation/arena/matches/{match_id}/commit",
            {"commitment": hashlib.sha256(seed.encode()).hexdigest()},
        )
        log(f"{match_id}: committed")
    elif state == "COMMITTED" and not mine["revealed"]:
        seed = _seeds().get(match_id)
        if seed is None:
            log(f"{match_id}: the committed seed is lost; the match will time out")
            return
        api.call("POST", f"/v1/federation/arena/matches/{match_id}/reveal", {"seed": seed})
        log(f"{match_id}: revealed")
    elif state == "PLAYING":
        hand = (match.get("game") or {}).get("hand")
        if hand and hand["legal"]:
            move = decide(match)
            api.call(
                "POST",
                f"/v1/federation/arena/matches/{match_id}/act",
                {"seq": match["action_seq"], **move},
            )
            log(
                f"{match_id}: hand {hand['no']} {hand['street']} {hand['my_cards']} "
                f"{hand['board']} -> {move}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--key", default=os.environ.get("VEYNOD_AGENT_KEY"))
    parser.add_argument("--spec", required=True, help="a MOCK_DEMO resource spec id")
    parser.add_argument("--stake", type=int, default=1_000_000, help="tokens (1 DEMO_M = 1e6)")
    parser.add_argument("--mode", choices=("sng", "cash"), default="sng")
    parser.add_argument("--matches", type=int, default=1)
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args()
    if not args.key:
        parser.error("--key or VEYNOD_AGENT_KEY is required")
    api = Api(args.base, args.key)

    def log(text: str) -> None:
        print(time.strftime("%H:%M:%S"), text, flush=True)

    policy = api.call("GET", "/v1/federation/arena/policy")
    if not policy.get("agents_may_play"):
        log("the owner has not allowed agents to play: PUT /v1/federation/arena/policy")
        return 2
    # Matches from earlier runs are not this run's results.
    seen = {m["match_id"] for m in api.call("GET", "/v1/federation/arena/matches")["items"]}
    finished: set[str] = set()
    queued = False
    while len(finished) < args.matches:
        matches = api.call("GET", "/v1/federation/arena/matches")["items"]
        live = [
            m
            for m in matches
            if m["state"] not in (*DONE, "RESULT_VERIFIED", "DISPUTED")
            and m["match_id"] not in seen
        ]
        for match in matches:
            mid = match["match_id"]
            if mid in seen or mid in finished:
                continue
            if match["state"] in DONE or match["result"]:
                if match["result"]:
                    proof = match["result"]["proof"]
                    check = api.call("GET", f"/v1/federation/arena/matches/{mid}/verify")
                    log(
                        f"{mid}: {proof['reason']}, winner seat {proof['winner_seat']} "
                        f"(I am {match['my_seat']}), {proof['settles_tokens']} tokens, "
                        f"verified={check['valid']}"
                    )
                else:
                    log(f"{mid}: ended {match['state']} before play")
                finished.add(mid)
                queued = False
        if live:
            queued = False  # matched
        if not live and not queued and len(finished) < args.matches:
            joined = api.call(
                "POST",
                "/v1/federation/arena/queue",
                {"spec_id": args.spec, "stake": args.stake, "mode": args.mode},
            )
            queued = True
            log(f"queued: {joined['state']}")
        for match in live:
            try:
                step(api, match, log)
            except RuntimeError as err:  # a stale seq after the other side moved: next poll
                log(str(err)[:300])
        time.sleep(args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
