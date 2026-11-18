"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import { BrandMark } from "../components/ui";
import { normalizeBase } from "../types";

/*
 * Veynod Arena: the poker lobby and tables as their own page (federation P6).
 *
 * NO ECONOMIC VALUE. Stakes are DEMO_M (simulated capacity), chips live only
 * inside a match; nothing here can be bought, withdrawn or exchanged.
 *
 * Lobby: cash tables (chips are tokens, settle by final stacks, stand up
 * between hands) and heads-up tournaments (winner takes both buy-ins).
 * Table: the seat's own view (own cards only), legal moves, a timer, the hand
 * log, the result with replay and verification. People play here; agents play
 * the same tables through the API / MCP within the owner's arena policy.
 */

const SESSION_KEY = "agentgraph.session_token";
const TOKEN_KEY = "agentgraph.admin_token";
const BASE_KEY = "agentgraph.api_base";
const RETURN_KEY = "agentgraph.return_to";
const M = 1_000_000;
const POLL_MS = 1500;
const SUIT: Record<string, string> = { s: "♠", h: "♥", d: "♦", c: "♣" };
const LIVE = ["MATCHED", "OWNER_AUTHORIZED", "DEMO_RESERVED", "COMMITTED", "PLAYING",
  "RESULT_VERIFIED", "DISPUTED", "RECONCILING"];
const CASH_NAMES = ["Pebble", "Brook", "Harbor", "Summit", "Orbit"];
const SNG_NAMES = ["Sprint", "Duel", "Crown"];
const MOVE: Record<string, string> = {
  fold: "Fold", check: "Check", call: "Call", bet: "Bet", raise: "Raise", all_in: "All in",
  timeout: "Timed out", timeout_forfeit: "Forfeit on time",
};
const STREET: Record<string, string> = {
  preflop: "Preflop", flop: "Flop", turn: "Turn", river: "River",
  fold: "Won by a fold", showdown: "Showdown", forfeit: "Forfeit",
};
const CATEGORY: Record<string, string> = {
  high_card: "High card", pair: "Pair", two_pair: "Two pair", three_of_a_kind: "Three of a kind",
  straight: "Straight", flush: "Flush", full_house: "Full house", four_of_a_kind: "Four of a kind",
  straight_flush: "Straight flush",
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

type Lobby = {
  model_classes: string[];
  cash: LobbyTable[];
  tournaments: LobbyTable[];
  queued: { mode: string; stake: number; model_class: string; since: string } | null;
  restricted: boolean;
  policy: Policy;
  rules: { starting_stack: number; blinds: number[][]; hands_per_level: number; max_hands: number;
    turn_seconds: number; max_timeouts: number };
};
type LobbyTable = {
  mode: string; stake: number; starting_chips: number; blinds: number[];
  waiting: Record<string, number>; tokens_per_chip?: number; prize?: number;
  blind_levels?: number[][]; hands_per_level?: number;
};
type Policy = { agents_may_play: boolean; max_stake: number; matches_per_day: number };
type Spec = { spec_id: string; model_class: string; escrowable: boolean; state: string;
  unreserved_tokens?: number };
type Legal = { type: string; amount?: number; min_to?: number; max_to?: number };
type Act = { seat: number; type: string; to?: number; amount?: number; timeout?: boolean };
type HandSummary = { no: number; ended: string; won: number[]; board: string[];
  hole?: string[][]; button: number; blinds: number[]; actions: Act[];
  showdown?: { category: string }[] };
type Hand = {
  no: number; button: number; blinds: number[]; street: string; board: string[];
  committed: number[]; pot: number; to_act: number; to_call: number;
  my_cards: string[] | null; legal: Legal[]; actions: Act[];
};
type Game = { hand_no: number; stacks: number[]; finished: boolean; standing: number | null;
  timeouts: number[]; hand?: Hand; last_hand: HandSummary | null };
type Seat = { fid: string; authorized: boolean; agent_play: boolean; commitment: string | null;
  revealed: boolean };
type Match = {
  match_id: string; state: string; my_seat: number; seats: Seat[]; stake: number;
  model_class: string; mode: string; tokens_per_chip: number | null; contract_hash: string;
  action_seq: number; turn_deadline: string | null; setup_deadline: string; game: Game | null;
  server_commit: string; server_seed: string | null;
  rules: { starting_stack: number; max_hands: number; turn_seconds: number };
  result: { proof: { reason: string; winner_seat: number | null; final_stacks: number[];
    settles_tokens: number; hands_played: number } } | null;
  history: { to: string; at: string; note: string | null }[];
};
type Verify = { valid: boolean; checks: Record<string, boolean>; hand_history: HandSummary[] };
type Record_ = { played: number; won: number; lost: number; even: number; net: number;
  recent: { match_id: string; mode: string; model_class: string; stake: number; opponent: string;
    reason: string; hands: number; net: number; at: string }[] };
type Board = { items: { fid: string; handle: string | null; played: number; won: number;
  lost: number; net: number }[] };
type Obligation = { obligation_id: string; obligee: string; amount: number; state: string };

const asM = (tokens: number) => `${Number((tokens / M).toFixed(6))}`;
const short = (fid: string) => `…${fid.slice(-6)}`;
const seedKey = (matchId: string) => `veynod-arena-seed:${matchId}`;

function storeGet(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function storeSet(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* reveal by hand */ }
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

const CSS = `
.va { --bg:#0a0f1d; --panel:#111a2e; --panel2:#16213a; --line:#24304d; --ink:#e8eefc;
  --dim:#93a0bd; --accent:#19c3a6; --accent2:#7c5cff; --gold:#f2b84b; --red:#e05666;
  min-height:100vh; background:var(--bg); color:var(--ink);
  font-family: Inter, system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
.va a { color: inherit; }
.va-top { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:24px;
  padding:12px 24px; background:#0c1324ee; border-bottom:1px solid var(--line); backdrop-filter: blur(6px); }
.va-brand { display:flex; align-items:center; gap:10px; white-space:nowrap; text-decoration:none; font-weight:800; letter-spacing:.3px; font-size:18px; }
.va-brand small { white-space:nowrap; font-weight:600; color:var(--accent); font-size:11px; border:1px solid var(--accent);
  border-radius:4px; padding:1px 5px; margin-left:4px; }
.va-nav { display:flex; gap:4px; flex:1; flex-wrap:wrap; }
.va-nav button { background:none; border:0; color:var(--dim); font-weight:600; font-size:15px;
  padding:8px 12px; border-radius:8px; cursor:pointer; }
.va-nav button.on { color:var(--ink); background:var(--panel2); }
.va-pill { display:flex; align-items:center; gap:8px; background:var(--panel2); border:1px solid var(--line);
  border-radius:999px; padding:6px 12px; font-weight:700; white-space:nowrap; }
.va-chip { width:14px; height:14px; border-radius:50%; background: radial-gradient(circle at 50% 50%, #fff 0 22%, var(--gold) 23% 60%, #c48b22 61%);
  display:inline-block; box-shadow:0 0 0 2px #0002; }
.va-notice { background:linear-gradient(90deg,#1b2a4a,#20365a); color:#cfe3ff; font-size:13px;
  padding:8px 24px; border-bottom:1px solid var(--line); }
.va-main { display:grid; grid-template-columns: minmax(0,1fr) 340px; gap:24px; padding:24px;
  max-width:1360px; margin:0 auto; }
@media (max-width: 980px) { .va-main { grid-template-columns: 1fr; padding:16px; } .va-top { gap:12px; padding:10px 16px; } }
@media (max-width: 700px) { .va-top { flex-wrap:wrap; } .va-nav { order:3; flex:1 0 100%; flex-wrap:nowrap; overflow-x:auto; }
  .va-nav button { white-space:nowrap; } .va-hide-sm { display:none !important; } .va-brand { white-space:nowrap; }
  .va-avatar { min-width:160px; } .va-banner h1 { font-size:20px; } }
.va-h { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:8px 0 14px; flex-wrap:wrap; }
.va-h h2 { margin:0; font-size:24px; }
.va-classes { display:flex; gap:6px; flex-wrap:wrap; }
.va-classes button, .va-btn { border:1px solid var(--line); background:var(--panel2); color:var(--ink);
  border-radius:10px; padding:8px 14px; font-weight:700; cursor:pointer; font-size:14px; }
.va-classes button.on { background:var(--accent2); border-color:var(--accent2); }
.va-btn.primary { background:var(--accent); border-color:var(--accent); color:#04201b; }
.va-btn.warn { background:transparent; border-color:var(--red); color:#ffb3bd; }
.va-btn:disabled { opacity:.5; cursor:not-allowed; }
.va-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap:16px; margin-bottom:32px; }
.va-card { border-radius:16px; overflow:hidden; background:var(--panel); border:1px solid var(--line);
  display:flex; flex-direction:column; }
.va-card-head { padding:12px 14px; background:#1a2440; display:flex; justify-content:space-between; align-items:flex-start; }
.va-card-head.t { background:linear-gradient(135deg,#5b3df0,#3b2bb0); }
.va-card-head .k { font-size:11px; font-weight:800; letter-spacing:.6px; color:#b9c6e6; }
.va-card-head .n { font-size:16px; font-weight:700; margin-top:2px; }
.va-card-body { padding:16px 14px 18px; background:radial-gradient(120% 100% at 50% 0%, #11624f 0%, #0c3f35 70%);
  text-align:center; flex:1; display:flex; flex-direction:column; gap:8px; align-items:center; }
.va-card-body.t { background:radial-gradient(120% 100% at 50% 0%, #2a2f7a 0%, #181c4d 75%); }
.va-big { font-size:22px; font-weight:800; display:flex; gap:8px; align-items:center; justify-content:center; }
.va-sub { color:#cfe; opacity:.85; font-size:13px; }
.va-side { display:flex; flex-direction:column; gap:16px; }
.va-panel { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:16px; }
.va-panel h3 { margin:0 0 10px; font-size:17px; }
.va-row { display:flex; justify-content:space-between; gap:8px; align-items:center; padding:7px 0;
  border-bottom:1px solid #ffffff10; font-size:14px; }
.va-row:last-child { border-bottom:0; }
.va-dim { color:var(--dim); font-size:13px; }
.va-pos { color:#5fe3b5; font-weight:700; } .va-neg { color:#ff8a98; font-weight:700; }
.va-stats { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; text-align:center; }
.va-stats b { display:block; font-size:20px; }
.va-table-wrap { max-width:1360px; margin:0 auto; padding:16px 24px 32px; display:grid;
  grid-template-columns:minmax(0,1fr) 320px; gap:20px; }
@media (max-width: 980px) { .va-table-wrap { grid-template-columns:1fr; padding:12px; } }
.va-felt { position:relative; aspect-ratio: 16/9; min-height:360px; border-radius:200px / 140px;
  background:radial-gradient(ellipse at center, #14805f 0%, #0d5a44 55%, #093d2f 100%);
  border:14px solid #3a2414; box-shadow: inset 0 0 0 4px #0003, inset 0 0 60px #0007, 0 20px 50px #0008; }
@media (max-width: 640px) { .va-felt { border-radius:120px / 90px; aspect-ratio: 4/5; border-width:10px; } }
.va-center { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:12px; }
.va-board { display:flex; gap:8px; }
.va-pot { background:#0006; border-radius:999px; padding:6px 14px; font-weight:800; display:flex; gap:8px; align-items:center; }
.va-seat { position:absolute; left:50%; transform:translateX(-50%); display:flex; flex-direction:column; align-items:center; gap:6px; }
.va-seat.top { top:-36px; } .va-seat.bottom { bottom:-48px; }
.va-avatar { display:flex; align-items:center; gap:10px; background:#0d1424; border:2px solid var(--line);
  border-radius:999px; padding:6px 14px 6px 6px; min-width:200px; }
.va-avatar.turn { border-color:var(--accent); box-shadow:0 0 0 3px #19c3a655; }
.va-avatar .face { width:38px; height:38px; border-radius:50%; display:flex; align-items:center; justify-content:center;
  font-weight:800; background:linear-gradient(135deg,#7c5cff,#19c3a6); color:#fff; }
.va-avatar .who { font-weight:700; font-size:14px; } .va-avatar .st { color:var(--gold); font-weight:800; font-size:14px; }
.va-dealer { width:22px; height:22px; border-radius:50%; background:#fff; color:#111; font-weight:900; font-size:12px;
  display:flex; align-items:center; justify-content:center; }
.va-bet { display:flex; align-items:center; gap:6px; background:#0007; border-radius:999px; padding:3px 10px; font-weight:700; font-size:13px; }
.va-card-face { width:46px; height:66px; border-radius:7px; background:#fff; color:#111; display:flex; flex-direction:column;
  align-items:center; justify-content:center; font-weight:800; font-size:20px; line-height:1; box-shadow:0 3px 8px #0006; }
.va-card-face.red { color:#d2283e; }
.va-card-face.back { background: repeating-linear-gradient(45deg,#3b2bb0 0 6px,#5b3df0 6px 12px); border:2px solid #fff; }
.va-card-face.slot { background:#ffffff14; box-shadow:none; border:1px dashed #ffffff30; }
.va-card-face.big { width:62px; height:88px; font-size:26px; }
.va-cards { display:flex; gap:6px; }
.va-actions { margin-top:64px; background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:14px;
  display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.va-actions input[type=range] { flex:1; min-width:160px; accent-color: var(--accent); }
.va-timer { height:6px; border-radius:999px; background:#ffffff14; overflow:hidden; width:100%; }
.va-timer > i { display:block; height:100%; background:var(--accent); transition: width .5s linear; }
.va-log { max-height:420px; overflow:auto; font-size:13px; }
.va-overlay { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; padding:24px; }
.va-dialog { background:#0d1424f2; border:1px solid var(--line); border-radius:16px; padding:20px; max-width:440px; width:100%; text-align:center; }
.va-dialog h3 { margin:0 0 8px; font-size:20px; }
.va-mono { font-family: ui-monospace, Menlo, monospace; font-size:11px; color:var(--dim); word-break:break-all; }
.va-banner { border-radius:16px; padding:18px 20px; margin-bottom:24px; display:flex; justify-content:space-between; gap:16px;
  align-items:center; flex-wrap:wrap; background:linear-gradient(110deg,#5b3df0 0%,#19c3a6 100%); }
.va-banner h1 { margin:0; font-size:24px; } .va-banner p { margin:4px 0 0; opacity:.9; }
.va-toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#1f2a46; border:1px solid var(--line);
  padding:10px 16px; border-radius:10px; z-index:20; max-width:90vw; }
.va input:not([type=range]):not([type=checkbox]), .va select { background:#0d1424; color:var(--ink); border:1px solid var(--line);
  border-radius:8px; padding:7px 10px; }
`;

function CardFace({ card, big, back, slot }: { card?: string; big?: boolean; back?: boolean;
  slot?: boolean }) {
  const cls = ["va-card-face", big ? "big" : "", back ? "back" : "", slot ? "slot" : "",
    card && (card[1] === "h" || card[1] === "d") ? "red" : ""].join(" ");
  if (!card || back || slot) return <div className={cls} />;
  return (
    <div className={cls}>
      <span>{card[0] === "T" ? "10" : card[0]}</span>
      <span>{SUIT[card[1]]}</span>
    </div>
  );
}

export default function ArenaPage() {
  const { t } = useI18n();
  const [base, setBase] = useState("");
  const [token, setToken] = useState("");
  const [ready, setReady] = useState(false);
  const [off, setOff] = useState<string | null>(null);
  const [view, setView] = useState<"lobby" | "mine" | "board" | "rules">("lobby");
  const [lobby, setLobby] = useState<Lobby | null>(null);
  const [specs, setSpecs] = useState<Spec[]>([]);
  const [matches, setMatches] = useState<Match[]>([]);
  const [record, setRecord] = useState<Record_ | null>(null);
  const [board, setBoard] = useState<Board | null>(null);
  const [owed, setOwed] = useState<Obligation[]>([]);
  const [modelClass, setModelClass] = useState<string>("");
  const [openId, setOpenId] = useState<string | null>(null);
  const [replay, setReplay] = useState<Verify | null>(null);
  const [raiseTo, setRaiseTo] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const revealing = useRef<Set<string>>(new Set());

  useEffect(() => {
    const stored = storeGet(BASE_KEY);
    const b = stored || (window.location.port === "3000" ? "http://localhost:8000"
      : window.location.origin);
    let tk = "";
    try {
      tk = storeGet(SESSION_KEY) || sessionStorage.getItem(TOKEN_KEY) || storeGet(TOKEN_KEY) || "";
    } catch { /* no storage */ }
    if (!tk) {
      try { sessionStorage.setItem(RETURN_KEY, "/dashboard/arena/"); } catch { /* ignore */ }
      window.location.replace("/dashboard/login/");
      return;
    }
    setBase(b);
    setToken(tk);
    setReady(true);
  }, []);

  const api = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const headers = new Headers(init?.headers);
    headers.set("Authorization", `Bearer ${token}`);
    if (init?.body) headers.set("Content-Type", "application/json");
    const response = await fetch(`${normalizeBase(base)}${path}`, { ...init, headers });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      let code = "";
      try {
        const body = await response.json();
        message = body?.error?.message ?? message;
        code = body?.error?.code ?? "";
      } catch { /* keep the status */ }
      const error = new Error(message) as Error & { status?: number; code?: string };
      error.status = response.status;
      error.code = code;
      throw error;
    }
    return (await response.json()) as T;
  }, [base, token]);

  const load = useCallback(async () => {
    try {
      const [l, m, s] = await Promise.all([
        api<Lobby>("/v1/federation/arena/lobby"),
        api<{ items: Match[] }>("/v1/federation/arena/matches"),
        api<{ items: Spec[] }>("/v1/federation/rights/specs"),
      ]);
      setLobby(l);
      setMatches(m.items ?? []);
      setSpecs(s.items ?? []);
      setOff(null);
    } catch (error) {
      const e = error as Error & { status?: number };
      if (e.status === 404) setOff(e.message);
      else if (e.status === 401) {
        try { sessionStorage.setItem(RETURN_KEY, "/dashboard/arena/"); } catch { /* ignore */ }
        window.location.replace("/dashboard/login/");
      } else setToast(e.message);
    }
  }, [api]);

  const loadSide = useCallback(async () => {
    try {
      const [r, b, o] = await Promise.all([
        api<Record_>("/v1/federation/arena/record"),
        api<Board>("/v1/federation/arena/leaderboard?limit=20"),
        api<{ owed_by_me: Obligation[] }>("/v1/federation/arena/obligations"),
      ]);
      setRecord(r);
      setBoard(b);
      setOwed(o.owed_by_me ?? []);
    } catch { /* the lobby call reports errors */ }
  }, [api]);

  useEffect(() => { if (ready) { void load(); void loadSide(); } }, [ready, load, loadSide]);

  const live = useMemo(() => matches.filter((m) => LIVE.includes(m.state)), [matches]);
  const open = matches.find((m) => m.match_id === openId) ?? null;
  const polling = Boolean(lobby?.queued) || live.length > 0 || Boolean(open);

  // Fast while seated or queued; slower otherwise, so the lobby shows who is waiting.
  useEffect(() => {
    if (!ready) return;
    const timer = window.setInterval(() => { void load(); }, polling ? POLL_MS : 5000);
    return () => window.clearInterval(timer);
  }, [ready, polling, load]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!lobby || modelClass) return;
    const mine = specs.find((s) => s.escrowable && s.state === "ACTIVE");
    setModelClass(mine?.model_class ?? lobby.model_classes[0] ?? "");
  }, [lobby, specs, modelClass]);
  // A new match takes you to its table.
  const seen = useRef<Set<string> | null>(null);
  useEffect(() => {
    const ids = new Set(live.map((m) => m.match_id));
    if (seen.current) {
      const fresh = live.find((m) => !seen.current?.has(m.match_id));
      if (fresh) { setOpenId(fresh.match_id); setReplay(null); }
    }
    seen.current = ids;
  }, [live]);
  // Reveal a committed seed automatically.
  useEffect(() => {
    for (const m of matches) {
      const seed = storeGet(seedKey(m.match_id));
      if (m.state === "COMMITTED" && seed && !m.seats[m.my_seat].revealed
          && !revealing.current.has(m.match_id)) {
        revealing.current.add(m.match_id);
        void api(`/v1/federation/arena/matches/${m.match_id}/reveal`,
          { method: "POST", body: JSON.stringify({ seed }) })
          .then(() => load())
          .catch((error) => setToast((error as Error).message))
          .finally(() => revealing.current.delete(m.match_id));
      }
    }
  }, [matches, api, load]);
  useEffect(() => { if (toast) { const id = window.setTimeout(() => setToast(null), 4500);
    return () => window.clearTimeout(id); } }, [toast]);
  // Settled results feed the record and the standings.
  const closedCount = matches.filter((m) => m.state === "CLOSED").length;
  useEffect(() => { if (ready) void loadSide(); }, [closedCount, ready, loadSide]);

  async function call(path: string, body?: unknown, method = "POST") {
    setBusy(true);
    try {
      const result = await api(path, { method,
        body: body === undefined ? undefined : JSON.stringify(body) });
      await load();
      return result;
    } catch (error) {
      setToast((error as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  const spec = specs.find((s) => s.escrowable && s.state === "ACTIVE"
    && s.model_class === modelClass);

  async function join(mode: string, stake: number) {
    if (!spec) { setToast(t("Declare simulated capacity in this model class first")); return; }
    await call("/v1/federation/arena/queue", { spec_id: spec.spec_id, stake, mode });
  }

  async function commit(m: Match) {
    const seed = storeGet(seedKey(m.match_id)) ?? newSeed();
    storeSet(seedKey(m.match_id), seed);
    await call(`/v1/federation/arena/matches/${m.match_id}/commit`,
      { commitment: await sha256Hex(seed) });
  }

  async function verify(m: Match) {
    setBusy(true);
    try {
      setReplay(await api<Verify>(`/v1/federation/arena/matches/${m.match_id}/verify`));
    } catch (error) {
      setToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!ready) return <div className="va"><style dangerouslySetInnerHTML={{ __html: CSS }} /></div>;

  const top = (
    <header className="va-top">
      <a className="va-brand" href="/dashboard/arena/" onClick={(event) => {
        event.preventDefault(); setOpenId(null); setView("lobby"); }}>
        <span style={{ width: 28, height: 28, display: "inline-flex", overflow: "hidden", flex: "0 0 28px" }}>
          <BrandMark size={28} tile={false} />
        </span>
        <span>Veynod Arena</span><small>{t("SIMULATED")}</small>
      </a>
      <nav className="va-nav">
        {([["lobby", "Lobby"], ["mine", "My games"], ["board", "Leaderboard"],
          ["rules", "How it works"]] as const).map(([key, label]) => (
          <button key={key} type="button" className={view === key && !openId ? "on" : ""}
            onClick={() => { setOpenId(null); setView(key); }}>
            {t(label)}{key === "mine" && live.length > 0 ? ` (${live.length})` : ""}
          </button>
        ))}
      </nav>
      {spec && (
        <span className="va-pill va-hide-sm" title={t("Unreserved simulated capacity")}>
          <i className="va-chip" />{asM(spec.unreserved_tokens ?? 0)} DEMO_M
        </span>
      )}
      <LanguageToggle />
      <a className="va-dim va-hide-sm" href="/dashboard/">{t("Console")}</a>
    </header>
  );
  const notice = (
    <div className="va-notice">
      {t("Simulated arena: no economic value. Stakes are DEMO_M; chips exist only inside a match. Nothing can be bought, withdrawn or exchanged.")}
    </div>
  );

  if (off) {
    return (
      <div className="va"><style dangerouslySetInnerHTML={{ __html: CSS }} />{top}{notice}
        <div className="va-main"><div className="va-panel">
          <h3>{t("The arena is off on this deployment")}</h3>
          <p className="va-dim">{off}</p>
          <p className="va-dim">{t("An operator turns on FEDERATION_RESOURCE_RIGHTS_ENABLED and FEDERATION_ARENA_ENABLED.")}</p>
        </div></div>
      </div>
    );
  }

  return (
    <div className="va">
      <style dangerouslySetInnerHTML={{ __html: CSS }} />
      {top}
      {notice}
      {open ? (
        <TableView m={open} t={t} busy={busy} now={now} replay={replay}
          raiseTo={raiseTo} setRaiseTo={setRaiseTo}
          onBack={() => { setOpenId(null); setReplay(null); }}
          onCall={call} onCommit={commit} onVerify={verify} />
      ) : (
        <div className="va-main">
          <div>
            {view === "lobby" && lobby && (
              <LobbyView lobby={lobby} t={t} busy={busy} modelClass={modelClass}
                setModelClass={setModelClass} spec={spec} onJoin={join}
                onLeaveQueue={() => void call("/v1/federation/arena/queue", undefined, "DELETE")} />
            )}
            {view === "mine" && (
              <MineView t={t} live={live} record={record} owed={owed}
                onOpen={(id) => { setOpenId(id); setReplay(null); }} />
            )}
            {view === "board" && <BoardView t={t} board={board} />}
            {view === "rules" && lobby && <RulesView t={t} lobby={lobby} />}
          </div>
          <aside className="va-side">
            <div className="va-panel">
              <h3>{t("My tables")}</h3>
              {live.length === 0 && !lobby?.queued && (
                <p className="va-dim">{t("You are not at any table.")}</p>
              )}
              {lobby?.queued && (
                <div className="va-row">
                  <span>{t("Waiting for an opponent")} · {lobby.queued.mode === "cash"
                    ? t("Cash table") : t("Tournament")} · {asM(lobby.queued.stake)}</span>
                  <button type="button" className="va-btn" disabled={busy}
                    onClick={() => void call("/v1/federation/arena/queue", undefined, "DELETE")}>
                    {t("Cancel")}</button>
                </div>
              )}
              {live.map((m) => (
                <div className="va-row" key={m.match_id}>
                  <span>{m.mode === "cash" ? t("Cash table") : t("Tournament")} · {asM(m.stake)}
                    {" "}<span className="va-dim">{t(STATE[m.state] ?? m.state)}</span></span>
                  <button type="button" className="va-btn primary"
                    onClick={() => { setOpenId(m.match_id); setReplay(null); }}>
                    {t("Open table")}</button>
                </div>
              ))}
            </div>
            {record && (
              <div className="va-panel">
                <h3>{t("My record")}</h3>
                <div className="va-stats">
                  <div><b>{record.played}</b><span className="va-dim">{t("Played")}</span></div>
                  <div><b>{record.won}</b><span className="va-dim">{t("Won")}</span></div>
                  <div><b>{record.lost}</b><span className="va-dim">{t("Lost")}</span></div>
                  <div><b className={record.net >= 0 ? "va-pos" : "va-neg"}>
                    {record.net >= 0 ? "+" : ""}{asM(record.net)}</b>
                    <span className="va-dim">DEMO_M</span></div>
                </div>
              </div>
            )}
            {board && board.items.length > 0 && (
              <div className="va-panel">
                <h3>{t("Top players")}</h3>
                {board.items.slice(0, 5).map((row, i) => (
                  <div className="va-row" key={row.fid}>
                    <span>{i + 1}. {row.handle ?? short(row.fid)}</span>
                    <span className={row.net >= 0 ? "va-pos" : "va-neg"}>
                      {row.net >= 0 ? "+" : ""}{asM(row.net)}</span>
                  </div>
                ))}
              </div>
            )}
            {lobby && <PolicyPanel t={t} initial={lobby.policy} busy={busy}
              onSave={(p) => void call("/v1/federation/arena/policy", p, "PUT")} />}
          </aside>
        </div>
      )}
      {toast && <div className="va-toast" role="status">{toast}</div>}
    </div>
  );
}

type T = (source: string, vars?: Record<string, string | number>) => string;

function LobbyView({ lobby, t, busy, modelClass, setModelClass, spec, onJoin, onLeaveQueue }: {
  lobby: Lobby; t: T; busy: boolean; modelClass: string; setModelClass: (c: string) => void;
  spec: Spec | undefined; onJoin: (mode: string, stake: number) => void;
  onLeaveQueue: () => void;
}) {
  const queued = lobby.queued;
  const button = (mode: string, stake: number, label: string) => {
    const here = queued && queued.mode === mode && queued.stake === stake
      && queued.model_class === modelClass;
    if (here) {
      return <button type="button" className="va-btn warn" disabled={busy}
        onClick={onLeaveQueue}>{t("Waiting… cancel")}</button>;
    }
    return <button type="button" className="va-btn primary"
      disabled={busy || Boolean(queued) || lobby.restricted || !spec}
      onClick={() => onJoin(mode, stake)}>{t(label)}</button>;
  };
  return (
    <>
      <div className="va-banner">
        <div>
          <h1>{t("Heads-up hold'em, people and agents at one table")}</h1>
          <p>{t("Same model class only. Fair shuffles from both players' seeds. Every match replays and verifies.")}</p>
        </div>
      </div>
      {lobby.restricted && (
        <div className="va-panel" style={{ borderColor: "var(--red)", marginBottom: 16 }}>
          {t("New arena matches are paused until an obligation is delivered or resolved")}
        </div>
      )}
      <div className="va-h">
        <h2>{t("Model class")}</h2>
        <div className="va-classes">
          {lobby.model_classes.map((c) => (
            <button key={c} type="button" className={c === modelClass ? "on" : ""}
              onClick={() => setModelClass(c)}>{c}</button>
          ))}
        </div>
      </div>
      {!spec && (
        <p className="va-dim">{t("You have no simulated capacity in {c}. An owner declares it under Federation › resource rights.", { c: modelClass })}</p>
      )}
      <div className="va-h"><h2>{t("Cash tables")}</h2>
        <span className="va-dim">{t("Chips are tokens · settle by your final stack · stand up between hands")}</span></div>
      <div className="va-grid">
        {lobby.cash.map((table, i) => {
          const waiting = table.waiting[modelClass] ?? 0;
          return (
            <div className="va-card" key={table.stake}>
              <div className="va-card-head">
                <div><div className="k">NL HOLD&apos;EM · {modelClass}</div>
                  <div className="n">{t(CASH_NAMES[i] ?? "Table")}</div></div>
                <span className="va-dim">{t("Heads-up")}</span>
              </div>
              <div className="va-card-body">
                <div className="va-big"><i className="va-chip" />{asM(table.stake)} DEMO_M</div>
                <div className="va-sub">{t("1 chip = {n} tokens", { n: table.tokens_per_chip ?? 0 })}
                  {" · "}{t("Blinds {a}/{b}", { a: table.blinds[0], b: table.blinds[1] })}</div>
                <div className="va-sub">{waiting ? t("{n} waiting: you sit down at once",
                  { n: waiting }) : t("Empty table: sit and wait")}</div>
                {button("cash", table.stake, "Sit down")}
              </div>
            </div>
          );
        })}
      </div>
      <div className="va-h"><h2>{t("Tournaments")}</h2>
        <span className="va-dim">{t("Heads-up sit and go · rising blinds · winner takes both buy-ins")}</span></div>
      <div className="va-grid">
        {lobby.tournaments.map((table, i) => {
          const waiting = table.waiting[modelClass] ?? 0;
          return (
            <div className="va-card" key={table.stake}>
              <div className="va-card-head t">
                <div><div className="k">NL HOLD&apos;EM · SNG</div>
                  <div className="n">{t(SNG_NAMES[i] ?? "Tournament")}</div></div>
                <span>🏆</span>
              </div>
              <div className="va-card-body t">
                <div className="va-big">{asM(table.prize ?? 0)} DEMO_M</div>
                <div className="va-sub">{t("Buy-in {n} DEMO_M", { n: asM(table.stake) })}
                  {" · "}{t("blinds up every {n} hands", { n: table.hands_per_level ?? 0 })}</div>
                <div className="va-sub">{waiting ? t("1 player registered: starts when you join")
                  : t("2 players needed")}</div>
                {button("sng", table.stake, "Register")}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

function MineView({ t, live, record, owed, onOpen }: {
  t: T; live: Match[]; record: Record_ | null; owed: Obligation[]; onOpen: (id: string) => void;
}) {
  return (
    <>
      <div className="va-h"><h2>{t("My games")}</h2></div>
      <div className="va-panel" style={{ marginBottom: 16 }}>
        <h3>{t("Open tables")}</h3>
        {live.length === 0 && <p className="va-dim">{t("You are not at any table.")}</p>}
        {live.map((m) => (
          <div className="va-row" key={m.match_id}>
            <span>{m.mode === "cash" ? t("Cash table") : t("Tournament")} · {m.model_class} ·{" "}
              {asM(m.stake)} DEMO_M · {t(STATE[m.state] ?? m.state)}</span>
            <button type="button" className="va-btn primary" onClick={() => onOpen(m.match_id)}>
              {t("Open table")}</button>
          </div>
        ))}
      </div>
      <div className="va-panel" style={{ marginBottom: 16 }}>
        <h3>{t("Recent results")}</h3>
        {!record?.recent.length && <p className="va-dim">{t("No finished matches yet.")}</p>}
        {record?.recent.map((r) => (
          <div className="va-row" key={r.match_id}>
            <span>{r.mode === "cash" ? t("Cash table") : t("Tournament")} · {r.model_class} ·{" "}
              {t("vs {who}", { who: short(r.opponent) })} · {t("{n} hands", { n: r.hands })} ·{" "}
              <span className="va-dim">{t(REASON[r.reason] ?? r.reason)}</span></span>
            <span className={r.net > 0 ? "va-pos" : r.net < 0 ? "va-neg" : "va-dim"}>
              {r.net > 0 ? "+" : ""}{asM(r.net)}</span>
          </div>
        ))}
      </div>
      <div className="va-panel">
        <h3>{t("Obligations")}</h3>
        {!owed.length && <p className="va-dim">{t("You owe no work.")}</p>}
        {owed.map((o) => (
          <div className="va-row" key={o.obligation_id}>
            <span>{t("Owed to {who}", { who: short(o.obligee) })} · {asM(o.amount)} DEMO_M</span>
            <span className="va-dim">{o.state}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function BoardView({ t, board }: { t: T; board: Board | null }) {
  return (
    <>
      <div className="va-h"><h2>{t("Leaderboard")}</h2>
        <span className="va-dim">{t("Net DEMO_M over settled matches")}</span></div>
      <div className="va-panel">
        {!board?.items.length && <p className="va-dim">{t("No finished matches yet.")}</p>}
        {board?.items.map((row, i) => (
          <div className="va-row" key={row.fid}>
            <span>{i + 1}. {row.handle ?? short(row.fid)}</span>
            <span className="va-dim">{t("{w} won · {l} lost", { w: row.won, l: row.lost })}</span>
            <span className={row.net >= 0 ? "va-pos" : "va-neg"}>
              {row.net >= 0 ? "+" : ""}{asM(row.net)}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function RulesView({ t, lobby }: { t: T; lobby: Lobby }) {
  const r = lobby.rules;
  return (
    <>
      <div className="va-h"><h2>{t("How it works")}</h2></div>
      <div className="va-panel" style={{ lineHeight: 1.7 }}>
        <p>{t("Heads-up no-limit hold'em. Each player starts with {n} chips; blinds start at {a}/{b} and rise every {h} hands; at most {m} hands.", { n: r.starting_stack, a: r.blinds[0][0], b: r.blinds[0][1], h: r.hands_per_level, m: r.max_hands })}</p>
        <p>{t("Cash table: one chip is worth buy-in / {n} tokens of the model class. Stand up between hands; each side settles by its final stack. No rebuys.", { n: r.starting_stack })}</p>
        <p>{t("Tournament: the player left with all the chips (or the chip leader at the hand limit) takes both buy-ins.")}</p>
        <p>{t("Fair shuffle: the host commits to its seed, both players commit and reveal theirs, every deck comes from all three. After the match the host seed is shown and anyone can replay it.")}</p>
        <p>{t("You have {s} seconds per move; on time-out you check or fold; {n} time-outs in a row lose the match.", { s: r.turn_seconds, n: r.max_timeouts })}</p>
        <p>{t("Winning gives you a voucher: the loser's agents do delegated work for you, in the same model class, up to the amount. A loser who does not deliver may, after review, be paused from new matches until it delivers.")}</p>
        <p className="va-dim">{t("Simulated arena: no economic value. Stakes are DEMO_M; chips exist only inside a match. Nothing can be bought, withdrawn or exchanged.")}</p>
      </div>
    </>
  );
}

function PolicyPanel({ t, initial, busy, onSave }: {
  t: T; initial: Policy; busy: boolean; onSave: (p: Policy) => void;
}) {
  const [p, setP] = useState(initial);
  useEffect(() => { setP(initial); }, [initial.agents_may_play, initial.max_stake,
    initial.matches_per_day]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="va-panel">
      <h3>{t("Agents at the table")}</h3>
      <label className="va-row" style={{ justifyContent: "flex-start", gap: 10 }}>
        <input type="checkbox" checked={p.agents_may_play}
          onChange={(event) => setP({ ...p, agents_may_play: event.target.checked })} />
        {t("Agents may play on their own")}
      </label>
      <label className="va-row">{t("Max stake per match (DEMO_M)")}
        <input style={{ width: 90 }} value={p.max_stake / M} inputMode="decimal"
          onChange={(event) => setP({ ...p, max_stake: Math.round(Number(event.target.value || 0) * M) })} />
      </label>
      <label className="va-row">{t("Matches per day")}
        <input style={{ width: 70 }} value={p.matches_per_day} inputMode="numeric"
          onChange={(event) => setP({ ...p, matches_per_day: Math.round(Number(event.target.value || 0)) })} />
      </label>
      <button type="button" className="va-btn" disabled={busy} style={{ marginTop: 8 }}
        onClick={() => onSave(p)}>{t("Save agent policy")}</button>
      <p className="va-dim" style={{ marginTop: 8 }}>{t("Only an owner can change this.")}</p>
    </div>
  );
}

function TableView({ m, t, busy, now, replay, raiseTo, setRaiseTo, onBack, onCall, onCommit,
  onVerify }: {
  m: Match; t: T; busy: boolean; now: number; replay: Verify | null;
  raiseTo: number | null; setRaiseTo: (n: number | null) => void; onBack: () => void;
  onCall: (path: string, body?: unknown, method?: string) => Promise<unknown>;
  onCommit: (m: Match) => Promise<void>; onVerify: (m: Match) => Promise<void>;
}) {
  const me = m.my_seat;
  const them = 1 - me;
  const game = m.game;
  const hand = game?.hand;
  const path = `/v1/federation/arena/matches/${m.match_id}`;
  const raise = hand?.legal.find((l) => l.type === "raise" || l.type === "bet");
  const pick = raise ? Math.min(Math.max(raiseTo ?? raise.min_to ?? 0, raise.min_to ?? 0),
    raise.max_to ?? 0) : 0;
  const myTurn = Boolean(hand && hand.legal.length);
  const left = m.turn_deadline ? Math.max(0, new Date(m.turn_deadline).getTime() - now) : 0;
  const pct = m.turn_deadline ? Math.min(100, (left / (m.rules.turn_seconds * 1000)) * 100) : 0;
  const seatName = (s: number) => (s === me ? t("You") : short(m.seats[s].fid));
  const shown = game?.last_hand?.hole;
  const lastWinner = game?.last_hand
    ? (game.last_hand.won[me] > game.last_hand.won[them] ? me
      : game.last_hand.won[them] > game.last_hand.won[me] ? them : null)
    : null;
  const preset = (to: number) => setRaiseTo(Math.round(to));

  const seat = (s: number, position: "top" | "bottom") => {
    const stack = game ? game.stacks[s] : m.rules.starting_stack;
    const bet = hand ? hand.committed[s] : 0;
    const turn = hand && hand.to_act === s && m.state === "PLAYING";
    const cards = s === me ? (hand?.my_cards ?? null) : null;
    const body = (
      <>
        <div className={`va-avatar${turn ? " turn" : ""}`}>
          <div className="face">{s === me ? "★" : m.seats[s].fid.slice(-2).toUpperCase()}</div>
          <div style={{ flex: 1 }}>
            <div className="who">{seatName(s)}{m.seats[s].agent_play ? ` · ${t("agent")}` : ""}</div>
            <div className="st">{stack} {t("chips")}</div>
          </div>
          {hand && hand.button === s && <div className="va-dealer">D</div>}
        </div>
        {turn && m.turn_deadline && <div className="va-timer" style={{ width: 200 }}>
          <i style={{ width: `${pct}%` }} /></div>}
      </>
    );
    const holes = s === me
      ? <div className="va-cards">{cards ? cards.map((c) => <CardFace key={c} card={c} big />) : null}</div>
      : <div className="va-cards">{hand ? <><CardFace back /><CardFace back /></> : null}</div>;
    const betChip = bet > 0 && <div className="va-bet"><i className="va-chip" />{bet}</div>;
    return (
      <div className={`va-seat ${position}`}>
        {position === "top" ? <>{body}{holes}{betChip}</> : <>{betChip}{holes}{body}</>}
      </div>
    );
  };

  const setup = ["MATCHED", "OWNER_AUTHORIZED", "DEMO_RESERVED", "COMMITTED"].includes(m.state);
  const mine = m.seats[me];
  let overlay: React.ReactNode = null;
  if (setup) {
    overlay = (
      <div className="va-dialog">
        <h3>{m.mode === "cash" ? t("Cash table") : t("Tournament")} · {m.model_class}</h3>
        <p className="va-dim">{t("Opponent {who}", { who: short(m.seats[them].fid) })} ·{" "}
          {asM(m.stake)} DEMO_M{m.tokens_per_chip
            ? ` · ${t("1 chip = {n} tokens", { n: m.tokens_per_chip })}` : ""}</p>
        {m.state === "MATCHED" && !mine.authorized && (
          <>
            <p>{t("Authorize the contract: your stake is held until the match settles.")}</p>
            <p className="va-mono">{m.contract_hash}</p>
            <button type="button" className="va-btn primary" disabled={busy}
              onClick={() => void onCall(`${path}/authorize`,
                { contract_hash: m.contract_hash, agent_play: false })}>
              {t("Authorize and hold the stake")}</button>
          </>
        )}
        {m.state === "DEMO_RESERVED" && !mine.commitment && (
          <>
            <p>{t("Add your random seed: the deck needs both players' seeds and the host's.")}</p>
            <button type="button" className="va-btn primary" disabled={busy}
              onClick={() => void onCommit(m)}>{t("Shuffle in my seed")}</button>
          </>
        )}
        {((m.state === "MATCHED" && mine.authorized) || (m.state === "DEMO_RESERVED" && mine.commitment)
          || m.state === "COMMITTED" || m.state === "OWNER_AUTHORIZED") && (
          <p className="va-dim">{t("Waiting for the other side")}…</p>
        )}
        <p className="va-mono">{t("Host seed commitment")}: {m.server_commit}</p>
        <button type="button" className="va-btn" disabled={busy}
          onClick={() => void onCall(`${path}/leave`)}>{t("Leave")}</button>
      </div>
    );
  } else if (m.result) {
    const p = m.result.proof;
    overlay = (
      <div className="va-dialog">
        <h3>{p.winner_seat === null || p.settles_tokens === 0 ? t("No winner: nothing changes hands")
          : p.winner_seat === me ? t("You won {n}", { n: `${asM(p.settles_tokens)} DEMO_M` })
          : t("You lost {n}", { n: `${asM(p.settles_tokens)} DEMO_M` })}</h3>
        <p className="va-dim">{t(REASON[p.reason] ?? p.reason)} · {t("{n} hands", { n: p.hands_played })}
          {" · "}{t("final stacks {a} : {b}", { a: p.final_stacks[me], b: p.final_stacks[them] })}</p>
        <div style={{ display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap" }}>
          <button type="button" className="va-btn primary" disabled={busy}
            onClick={() => void onVerify(m)}>{t("Replay and verify")}</button>
          {m.state === "RESULT_VERIFIED" && (
            <button type="button" className="va-btn" disabled={busy}
              onClick={() => void onCall(`${path}/dispute`,
                { reason: "Checked by a person at the table" })}>{t("Dispute")}</button>
          )}
          <button type="button" className="va-btn" onClick={onBack}>{t("Back to the lobby")}</button>
        </div>
        {replay && <p style={{ marginTop: 10 }} className={replay.valid ? "va-pos" : "va-neg"}>
          {replay.valid ? t("Replayed and verified") : t("Verification failed")}</p>}
      </div>
    );
  } else if (m.state === "ABORTED" || m.state === "TIMED_OUT") {
    overlay = (
      <div className="va-dialog">
        <h3>{t(STATE[m.state])}</h3>
        <p className="va-dim">{m.history[m.history.length - 1]?.note}</p>
        <button type="button" className="va-btn" onClick={onBack}>{t("Back to the lobby")}</button>
      </div>
    );
  }

  return (
    <div className="va-table-wrap">
      <div>
        <div className="va-h">
          <button type="button" className="va-btn" onClick={onBack}>← {t("Lobby")}</button>
          <span className="va-dim">
            {m.mode === "cash" ? t("Cash table") : t("Tournament")} · {m.model_class} ·{" "}
            {asM(m.stake)} DEMO_M{hand ? ` · ${t("Hand {n}", { n: hand.no })} · ${t("Blinds {a}/{b}",
              { a: hand.blinds[0], b: hand.blinds[1] })}` : ""}
          </span>
          <span className="va-dim">{t(STATE[m.state] ?? m.state)}</span>
        </div>
        <div className="va-felt">
          {seat(them, "top")}
          <div className="va-center">
            {hand && (
              <>
                <div className="va-pot"><i className="va-chip" />{t("Pot {n}", { n: hand.pot })}</div>
                <div className="va-board">
                  {[0, 1, 2, 3, 4].map((i) => hand.board[i]
                    ? <CardFace key={i} card={hand.board[i]} /> : <CardFace key={i} slot />)}
                </div>
                <div className="va-dim" style={{ color: "#cfe" }}>{t(STREET[hand.street] ?? hand.street)}</div>
              </>
            )}
            {game?.standing !== null && game?.standing !== undefined && m.state === "PLAYING" && (
              <div className="va-bet">{t("Table ends after this hand")}</div>
            )}
          </div>
          {seat(me, "bottom")}
          {overlay && <div className="va-overlay">{overlay}</div>}
        </div>
        {m.state === "PLAYING" && hand && (
          <div className="va-actions">
            {myTurn ? (
              <>
                {hand.legal.filter((l) => l.type !== "raise" && l.type !== "bet").map((l) => (
                  <button key={l.type} type="button"
                    className={`va-btn${l.type === "fold" ? " warn" : " primary"}`} disabled={busy}
                    onClick={() => { setRaiseTo(null); void onCall(`${path}/act`,
                      { seq: m.action_seq, type: l.type }); }}>
                    {t(MOVE[l.type] ?? l.type)}{l.amount ? ` ${l.amount}` : ""}
                  </button>
                ))}
                {raise && (
                  <>
                    <input type="range" min={raise.min_to} max={raise.max_to} value={pick}
                      aria-label={t("Raise to")}
                      onChange={(event) => setRaiseTo(Number(event.target.value))} />
                    <button type="button" className="va-btn" onClick={() => preset(raise.min_to ?? 0)}>
                      {t("Min")}</button>
                    <button type="button" className="va-btn" onClick={() => preset(Math.min(
                      raise.max_to ?? 0, Math.max(raise.min_to ?? 0,
                        Math.max(...hand.committed) + hand.pot / 2)))}>{t("½ pot")}</button>
                    <button type="button" className="va-btn" onClick={() => preset(Math.min(
                      raise.max_to ?? 0, Math.max(raise.min_to ?? 0,
                        Math.max(...hand.committed) + hand.pot)))}>{t("Pot")}</button>
                    <button type="button" className="va-btn primary" disabled={busy}
                      onClick={() => { setRaiseTo(null); void onCall(`${path}/act`,
                        { seq: m.action_seq, type: raise.type, amount: pick }); }}>
                      {t(raise.type === "bet" ? "Bet {n}" : "Raise to {n}", { n: pick })}
                    </button>
                  </>
                )}
              </>
            ) : (
              <span className="va-dim">{t("Waiting for the other side")}…{" "}
                {m.turn_deadline ? `${Math.ceil(left / 1000)}s` : ""}</span>
            )}
            <span style={{ flex: 1 }} />
            <button type="button" className="va-btn" disabled={busy || game?.standing === me}
              onClick={() => void onCall(`${path}/leave`)}>
              {m.mode === "cash" ? t("Stand up after this hand") : t("Concede")}</button>
          </div>
        )}
      </div>
      <aside className="va-side">
        <div className="va-panel">
          <h3>{t("This hand")}</h3>
          <div className="va-log">
            {!hand?.actions.length && <p className="va-dim">{t("No moves yet.")}</p>}
            {hand?.actions.map((a, i) => (
              <div className="va-row" key={i}>
                <span>{seatName(a.seat)}</span>
                <span>{t(MOVE[a.type] ?? a.type)}{a.to ? ` ${a.to}` : a.amount ? ` ${a.amount}` : ""}
                  {a.timeout ? ` (${t("time")})` : ""}</span>
              </div>
            ))}
          </div>
        </div>
        {game?.last_hand && (
          <div className="va-panel">
            <h3>{t("Last hand")} #{game.last_hand.no}</h3>
            <p>{lastWinner === null ? t("split") : lastWinner === me
              ? t("you won {n}", { n: game.last_hand.won[me] })
              : t("they won {n}", { n: game.last_hand.won[them] })}
              {" · "}<span className="va-dim">{t(STREET[game.last_hand.ended] ?? game.last_hand.ended)}</span></p>
            <div className="va-cards">{game.last_hand.board.map((c) => <CardFace key={c} card={c} />)}</div>
            {shown && (
              <p className="va-dim" style={{ marginTop: 8 }}>{t("their cards")}:{" "}
                {shown[them].map((c) => (c[0] === "T" ? "10" : c[0]) + SUIT[c[1]]).join(" ")}
                {game.last_hand.showdown && ` · ${t(CATEGORY[game.last_hand.showdown[them].category] ?? game.last_hand.showdown[them].category)}`}</p>
            )}
          </div>
        )}
        {replay && (
          <div className="va-panel">
            <h3>{t("Hand history")}</h3>
            <div className="va-log">
              {replay.hand_history.map((h) => (
                <div className="va-row" key={h.no} style={{ flexDirection: "column", alignItems: "flex-start" }}>
                  <div>#{h.no} · {t(STREET[h.ended] ?? h.ended)} · {h.won[me] > h.won[them] ? t("you won {n}", { n: h.won[me] })
                    : h.won[them] > h.won[me] ? t("they won {n}", { n: h.won[them] }) : t("split")}</div>
                  <div className="va-dim">{h.board.map((c) => (c[0] === "T" ? "10" : c[0]) + SUIT[c[1]]).join(" ")}
                    {h.hole ? ` · ${t("you")} ${h.hole[me].join(" ")} · ${t("them")} ${h.hole[them].join(" ")}` : ""}</div>
                </div>
              ))}
            </div>
            <p className="va-dim">{Object.entries(replay.checks).map(([k, v]) => `${k} ${v ? "✓" : "✗"}`).join(" · ")}</p>
          </div>
        )}
      </aside>
    </div>
  );
}
