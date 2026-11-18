"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";

/*
 * Agent Poker Arena (federation P6). NO ECONOMIC VALUE: DEMO_M and chips cannot
 * be bought, withdrawn or exchanged, and never pay a real model.
 *
 * People play here; agents play the same tables through the API or the MCP
 * federation tool, within the owner's arena policy. The seed a person
 * commits is made and kept in this browser (localStorage) and revealed
 * automatically once both sides have committed. The card hides itself while
 * the deployment has the feature off (the routes answer 404).
 */

type Spec = {
  spec_id: string; resource_class: string; model_class: string; escrowable: boolean;
  state: string; unreserved_tokens?: number;
};
type Voucher = {
  voucher_id: string; issuer: string; origin: string; face_amount: number;
  available_amount: number; status: string; model_class: string;
};
type Obligation = { obligation_id: string; obligee: string; amount: number; state: string };
type Legal = { type: string; amount?: number; min_to?: number; max_to?: number };
type Hand = {
  no: number; button: number; blinds: number[]; street: string; board: string[];
  committed: number[]; pot: number; to_act: number; to_call: number;
  my_cards: string[] | null; legal: Legal[]; actions: { seat: number; type: string; to?: number; amount?: number }[];
};
type Game = {
  hand_no: number; stacks: number[]; finished: boolean; standing: number | null;
  hand?: Hand; last_hand: { no: number; ended: string; won: number[]; board: string[];
    hole?: string[][] } | null;
};
type Seat = { fid: string; authorized: boolean; agent_play: boolean; commitment: string | null;
  revealed: boolean };
type Match = {
  match_id: string; state: string; my_seat: number; seats: Seat[]; stake: number;
  model_class: string; mode: string; tokens_per_chip: number | null; contract_hash: string;
  action_seq: number; turn_deadline: string | null; game: Game | null;
  result: { proof: { reason: string; winner_seat: number | null; final_stacks: number[];
    settles_tokens: number } } | null;
};
type Policy = { agents_may_play: boolean; max_stake: number; matches_per_day: number };

const M = 1_000_000;
const LIVE = ["MATCHED", "OWNER_AUTHORIZED", "DEMO_RESERVED", "COMMITTED", "PLAYING",
  "RESULT_VERIFIED", "DISPUTED", "RECONCILING"];
const asM = (tokens: number) => `${Number((tokens / M).toFixed(6))} DEMO_M`;
const SUIT: Record<string, string> = { s: "♠", h: "♥", d: "♦", c: "♣" };
const seedKey = (matchId: string) => `veynod-arena-seed:${matchId}`;
const MOVE: Record<string, string> = {
  fold: "Fold", check: "Check", call: "Call", bet: "Bet", raise: "Raise", all_in: "All in",
};
const REASON: Record<string, string> = {
  bust: "a stack ran out", conceded: "conceded", stood_up: "stood up",
  forfeit_timeouts: "too many timeouts", hand_limit: "hand limit reached",
  hand_limit_draw: "hand limit reached, even stacks",
};
const STATE: Record<string, string> = {
  MATCHED: "Matched", OWNER_AUTHORIZED: "Authorized", DEMO_RESERVED: "Stakes held",
  COMMITTED: "Seeds committed", PLAYING: "Playing", RESULT_VERIFIED: "Result ready",
  DISPUTED: "Disputed", RECONCILING: "Reconciling", DEMO_SETTLED: "Settled",
  CLOSED: "Closed", ABORTED: "Aborted", TIMED_OUT: "Timed out",
};

function storeGet(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function storeSet(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* private mode: reveal by hand */ }
}
async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}
function newSeed(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function Card({ card }: { card: string }) {
  const red = card[1] === "h" || card[1] === "d";
  return (
    <span style={{ display: "inline-block", minWidth: "2.2em", padding: "2px 4px", margin: "0 2px",
      border: "1px solid currentColor", borderRadius: 4, textAlign: "center",
      color: red ? "#c0392b" : undefined, fontWeight: 600 }}>
      {card[0] === "T" ? "10" : card[0]}{SUIT[card[1]]}
    </span>
  );
}

export function ArenaCard({ request, notify }: {
  request: ApiRequest; notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [off, setOff] = useState(false);
  const [specs, setSpecs] = useState<Spec[]>([]);
  const [held, setHeld] = useState<Voucher[]>([]);
  const [owed, setOwed] = useState<Obligation[]>([]);
  const [restricted, setRestricted] = useState(false);
  const [matches, setMatches] = useState<Match[]>([]);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [draft, setDraft] = useState({ spec: "", stake: "1", mode: "sng" });
  const [raiseTo, setRaiseTo] = useState<Record<string, number>>({});
  const [verified, setVerified] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [queued, setQueued] = useState(false);
  const revealing = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      const [s, v] = await Promise.all([
        request<{ items: Spec[] }>("/v1/federation/rights/specs"),
        request<{ held: Voucher[] }>("/v1/federation/rights/vouchers"),
      ]);
      setSpecs(s.items ?? []);
      setHeld(v.held ?? []);
    } catch {
      setOff(true); // resource rights are off on this deployment
      return;
    }
    try {
      const [o, m, p] = await Promise.all([
        request<{ owed_by_me: Obligation[]; restricted: boolean }>(
          "/v1/federation/arena/obligations"),
        request<{ items: Match[] }>("/v1/federation/arena/matches"),
        request<Policy>("/v1/federation/arena/policy"),
      ]);
      setOwed(o.owed_by_me ?? []);
      setRestricted(Boolean(o.restricted));
      setMatches(m.items ?? []);
      setPolicy(p);
    } catch {
      // the arena is off; resource rights still show
    }
  }, [request]);

  useEffect(() => { void load(); }, [load]);

  // Poll while waiting in the queue or while a match is live; reveal a
  // committed seed automatically.
  const live = matches.filter((m) => LIVE.includes(m.state));
  useEffect(() => {
    if (live.length) setQueued(false); // matched
  }, [live.length]);
  useEffect(() => {
    if (!live.length && !queued) return;
    const timer = window.setInterval(() => { void load(); }, 2000);
    return () => window.clearInterval(timer);
  }, [live.length, queued, load]);
  useEffect(() => {
    for (const m of matches) {
      const seed = storeGet(seedKey(m.match_id));
      if (m.state === "COMMITTED" && seed && !m.seats[m.my_seat].revealed
          && !revealing.current.has(m.match_id)) {
        revealing.current.add(m.match_id);
        void request(`/v1/federation/arena/matches/${m.match_id}/reveal`,
          { method: "POST", body: JSON.stringify({ seed }) })
          .then(() => load())
          .catch((error) => notify((error as Error)?.message ?? t("Something went wrong.")))
          .finally(() => revealing.current.delete(m.match_id));
      }
    }
  }, [matches, request, load, notify, t]);

  async function post(path: string, body?: unknown, method = "POST") {
    setBusy(true);
    try {
      const result = await request(path, { method,
        body: body === undefined ? undefined : JSON.stringify(body) });
      await load();
      return result;
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function commit(m: Match) {
    const seed = storeGet(seedKey(m.match_id)) ?? newSeed();
    storeSet(seedKey(m.match_id), seed);
    await post(`/v1/federation/arena/matches/${m.match_id}/commit`,
      { commitment: await sha256Hex(seed) });
  }

  async function verify(m: Match) {
    const result = await post(`/v1/federation/arena/matches/${m.match_id}/verify`,
      undefined, "GET") as { valid: boolean } | null;
    if (result) setVerified({ ...verified, [m.match_id]: result.valid });
  }

  if (off) return null;
  const stakeable = specs.filter((s) => s.escrowable && s.state === "ACTIVE");
  const spec = draft.spec || stakeable[0]?.spec_id || "";

  return (
    <section className="lin-list" aria-label={t("Arena (simulated)")}>
      <div className="lin-group">
        <i className="lamp amber" />{t("Arena (simulated)")}
        <span className="flow-aside">{t("No economic value; not redeemable or exchangeable")}</span>
      </div>

      {restricted && (
        <div className="lin-row">
          <i className="lamp red" />
          <span className="lin-title">
            {t("New arena matches are paused until an obligation is delivered or resolved")}
          </span>
        </div>
      )}
      {held.filter((v) => v.status === "ACTIVE").map((v) => (
        <div className="lin-row" key={v.voucher_id}>
          <span className="lin-title">{t("Voucher from {who}", { who: v.issuer })}</span>
          <span className="flow-aside">
            {v.model_class} · {asM(v.available_amount)} / {asM(v.face_amount)}
          </span>
        </div>
      ))}
      {owed.filter((o) => !["FULFILLED", "REMEDIATED", "RESOLVED"].includes(o.state)).map((o) => (
        <div className="lin-row" key={o.obligation_id}>
          <span className="lin-title">{t("Owed to {who}", { who: o.obligee })}</span>
          <span className="flow-aside">{asM(o.amount)} · {o.state}</span>
        </div>
      ))}

      {live.map((m) => {
        const me = m.my_seat;
        const seat = m.seats[me];
        const hand = m.game?.hand;
        const myTurn = Boolean(hand && hand.legal.length);
        const raise = hand?.legal.find((l) => l.type === "raise" || l.type === "bet");
        const target = raise ? Math.min(Math.max(raiseTo[m.match_id] ?? raise.min_to ?? 0,
          raise.min_to ?? 0), raise.max_to ?? 0) : 0;
        return (
          <div className="lin-row" key={m.match_id} style={{ flexWrap: "wrap", gap: 8 }}>
            <span className="lin-title">
              {m.mode === "cash" ? t("Cash table") : t("Sit and go")} · {m.model_class} ·{" "}
              {asM(m.stake)}
              {m.tokens_per_chip ? ` · ${t("1 chip = {n} tokens", { n: m.tokens_per_chip })}` : ""}
            </span>
            <span className="flow-aside">{t(STATE[m.state] ?? m.state)}</span>

            {m.state === "MATCHED" && !seat.authorized && (
              <button type="button" className="button primary small" disabled={busy}
                title={m.contract_hash}
                onClick={() => void post(`/v1/federation/arena/matches/${m.match_id}/authorize`,
                  { contract_hash: m.contract_hash, agent_play: false })}>
                {t("Authorize and hold the stake")}
              </button>
            )}
            {m.state === "DEMO_RESERVED" && !seat.commitment && (
              <button type="button" className="button primary small" disabled={busy}
                onClick={() => void commit(m)}>{t("Shuffle in my seed")}</button>
            )}
            {["MATCHED", "OWNER_AUTHORIZED", "DEMO_RESERVED", "COMMITTED"].includes(m.state) && (
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => void post(`/v1/federation/arena/matches/${m.match_id}/leave`)}>
                {t("Leave")}
              </button>
            )}

            {m.state === "PLAYING" && m.game && (
              <div style={{ width: "100%" }}>
                <div>
                  {t("Hand {n}", { n: m.game.hand_no })} · {t("Stacks")}{" "}
                  {t("me {a} · them {b}", { a: m.game.stacks[me], b: m.game.stacks[1 - me] })}
                  {m.game.standing !== null && ` · ${t("Table ends after this hand")}`}
                </div>
                {m.game.last_hand && (
                  <div className="flow-aside">
                    {t("Last hand: {r}", {
                      r: m.game.last_hand.won[me] > m.game.last_hand.won[1 - me]
                        ? t("you won {n}", { n: m.game.last_hand.won[me] })
                        : m.game.last_hand.won[me] < m.game.last_hand.won[1 - me]
                          ? t("they won {n}", { n: m.game.last_hand.won[1 - me] })
                          : t("split"),
                    })}
                    {m.game.last_hand.hole && (
                      <> · {t("their cards")}{" "}
                        {m.game.last_hand.hole[1 - me].map((c) => <Card key={c} card={c} />)}</>
                    )}
                  </div>
                )}
                {hand && (
                  <>
                    <div>
                      {t("My cards")} {hand.my_cards?.map((c) => <Card key={c} card={c} />)}
                      {" · "}{t("Board")}{" "}
                      {hand.board.length ? hand.board.map((c) => <Card key={c} card={c} />) : "—"}
                    </div>
                    <div className="flow-aside">
                      {t("Pot {n}", { n: hand.pot })} · {t("Blinds {a}/{b}",
                        { a: hand.blinds[0], b: hand.blinds[1] })} ·{" "}
                      {hand.button === me ? t("I am on the button") : t("They are on the button")}
                      {hand.actions.length > 0 && ` · ${hand.actions.slice(-3).map((a) =>
                        `${a.seat === me ? t("me") : t("them")} ${t(MOVE[a.type] ?? a.type)}${
                          a.to ? ` ${a.to}` : ""}`)
                        .join(", ")}`}
                    </div>
                    {myTurn ? (
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>
                        {hand.legal.filter((l) => l.type !== "raise" && l.type !== "bet").map((l) => (
                          <button key={l.type} type="button" className="button primary small"
                            disabled={busy}
                            onClick={() => void post(`/v1/federation/arena/matches/${m.match_id}/act`,
                              { seq: m.action_seq, type: l.type })}>
                            {t(MOVE[l.type] ?? l.type)}{l.amount ? ` ${l.amount}` : ""}
                          </button>
                        ))}
                        {raise && (
                          <>
                            <input type="range" min={raise.min_to} max={raise.max_to} value={target}
                              aria-label={t("Raise to")}
                              onChange={(event) => setRaiseTo({ ...raiseTo,
                                [m.match_id]: Number(event.target.value) })} />
                            <button type="button" className="button primary small" disabled={busy}
                              onClick={() => void post(
                                `/v1/federation/arena/matches/${m.match_id}/act`,
                                { seq: m.action_seq, type: raise.type, amount: target })}>
                              {t(raise.type === "bet" ? "Bet {n}" : "Raise to {n}", { n: target })}
                            </button>
                          </>
                        )}
                      </div>
                    ) : (
                      <div className="flow-aside">{t("Waiting for the other side")}</div>
                    )}
                  </>
                )}
                <button type="button" className="button ghost small" disabled={busy}
                  onClick={() => void post(`/v1/federation/arena/matches/${m.match_id}/leave`)}>
                  {m.mode === "cash" ? t("Stand up after this hand") : t("Concede")}
                </button>
              </div>
            )}

            {m.result && (
              <div style={{ width: "100%" }} className="flow-aside">
                {m.result.proof.winner_seat === null ? t("No winner: nothing changes hands")
                  : m.result.proof.winner_seat === me ? t("You won {n}",
                    { n: asM(m.result.proof.settles_tokens) })
                  : t("You lost {n}", { n: asM(m.result.proof.settles_tokens) })}
                {" · "}{t(REASON[m.result.proof.reason] ?? m.result.proof.reason)}
                {" · "}
                <button type="button" className="button ghost small" disabled={busy}
                  onClick={() => void verify(m)}>{t("Verify")}</button>
                {m.match_id in verified && (verified[m.match_id]
                  ? ` ${t("Replayed and verified")}` : ` ${t("Verification failed")}`)}
                {m.state === "RESULT_VERIFIED" && (
                  <button type="button" className="button ghost small" disabled={busy}
                    onClick={() => void post(`/v1/federation/arena/matches/${m.match_id}/dispute`,
                      { reason: "Checked by a person from the console" })}>{t("Dispute")}</button>
                )}
              </div>
            )}
          </div>
        );
      })}

      {matches.filter((m) => !LIVE.includes(m.state)).slice(0, 3).map((m) => (
        <div className="lin-row" key={m.match_id}>
          <span className="lin-title">
            {m.mode === "cash" ? t("Cash table") : t("Sit and go")} · {m.model_class}
          </span>
          <span className="flow-aside">
            {m.result ? (m.result.proof.winner_seat === null ? t("No winner: nothing changes hands")
              : m.result.proof.winner_seat === m.my_seat
                ? t("You won {n}", { n: asM(m.result.proof.settles_tokens) })
                : t("You lost {n}", { n: asM(m.result.proof.settles_tokens) }))
              : t(STATE[m.state] ?? m.state)}
          </span>
        </div>
      ))}

      {stakeable.length > 0 && (
        <form className="lin-row" onSubmit={(event) => {
          event.preventDefault();
          void post("/v1/federation/arena/queue", {
            spec_id: spec, stake: Math.round(Number(draft.stake) * M), mode: draft.mode,
          }).then((joined) => {
            if ((joined as { state?: string } | null)?.state === "WAITING") setQueued(true);
          });
        }}>
          <select value={spec} aria-label={t("Simulated capacity")}
            onChange={(event) => setDraft({ ...draft, spec: event.target.value })}>
            {stakeable.map((s) => (
              <option key={s.spec_id} value={s.spec_id}>
                {s.model_class} · {asM(s.unreserved_tokens ?? 0)}
              </option>
            ))}
          </select>
          <select value={draft.mode} aria-label={t("Table")}
            onChange={(event) => setDraft({ ...draft, mode: event.target.value })}>
            <option value="sng">{t("Sit and go")}</option>
            <option value="cash">{t("Cash table")}</option>
          </select>
          <input value={draft.stake} inputMode="decimal" aria-label={t("Stake (DEMO_M)")}
            placeholder={t("Stake (DEMO_M)")}
            onChange={(event) => setDraft({ ...draft, stake: event.target.value })} />
          <button type="submit" className="button primary small"
            disabled={busy || restricted || queued || !Number(draft.stake)}>
            {queued ? t("Waiting for an opponent") : t("Find a match")}
          </button>
          <button type="button" className="button ghost small" disabled={busy}
            onClick={() => void post("/v1/federation/arena/queue", undefined, "DELETE")
              .then(() => setQueued(false))}>
            {t("Leave the queue")}
          </button>
        </form>
      )}

      {policy && (
        <form className="lin-row" style={{ flexWrap: "wrap", gap: 12 }} onSubmit={(event) => {
          event.preventDefault();
          void post("/v1/federation/arena/policy", policy, "PUT");
        }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
            <input type="checkbox" checked={policy.agents_may_play}
              style={{ width: "auto", flex: "0 0 auto", margin: 0 }}
              onChange={(event) => setPolicy({ ...policy, agents_may_play: event.target.checked })} />
            {t("Agents may play on their own")}
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
            {t("Max stake per match (DEMO_M)")}
            <input value={policy.max_stake / M} inputMode="decimal"
              style={{ width: "6em", flex: "0 0 6em", minWidth: 0 }}
              onChange={(event) => setPolicy({ ...policy,
                max_stake: Math.round(Number(event.target.value || 0) * M) })} />
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
            {t("Matches per day")}
            <input value={policy.matches_per_day} inputMode="numeric"
              style={{ width: "4em", flex: "0 0 4em", minWidth: 0 }}
              onChange={(event) => setPolicy({ ...policy,
                matches_per_day: Math.round(Number(event.target.value || 0)) })} />
          </label>
          <button type="submit" className="button ghost small" disabled={busy}>
            {t("Save agent policy")}
          </button>
        </form>
      )}
    </section>
  );
}
