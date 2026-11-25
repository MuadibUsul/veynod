"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";

/*
 * Federation Lobby: the interactive public plaza of federation.
 *
 * Workspaces enter here first — even before turning on their own federation
 * identity — to see live interactive venues (Texas Hold'em Arena, Task &
 * Compute Market, Skill Workshop, Team Plaza & Chat, plus upcoming rooms),
 * live standings, and active teams, then step into any venue and return.
 */

export type LobbyVenue = "lobby" | "arena" | "market" | "skills" | "plaza" | "manage";

type ArenaTable = {
  waiting?: Record<string, number>;
  open_tables?: Record<string, { match_id: string; players: number; free: number; in_play: boolean }[]>;
};

type ArenaLobbySnap = {
  cash: ArenaTable[];
  tournaments: ArenaTable[];
  queued: { mode: string; seats: number; stake: number; model_class: string } | null;
};

type LeaderRow = {
  fid: string;
  handle: string | null;
  played: number;
  won: number;
  lost: number;
  net: number;
};

type DirectoryItem = {
  fid: string;
  handle: string | null;
  headline: string;
  capabilities: string[];
  availability: string;
  rank: { position: number; score: number };
};

type SkillSummary = {
  total: number;
  items: { name: string; description: string | null; category: string; downloads: number }[];
};

type WalletSnap = { available: number; held_in_escrow: number };
type SpecSnap = { escrowable: boolean; state: string; unreserved_tokens?: number };

const M = 1_000_000;
const shortFid = (fid: string) => (fid.length > 20 ? `${fid.slice(0, 14)}…${fid.slice(-4)}` : fid);
const asDemoM = (tokens: number) => `${Number((tokens / M).toFixed(2))} DEMO_M`;

const UPCOMING_ROOMS: {
  id: string;
  badge: string;
  title: string;
  summary: string;
  tag: string;
}[] = [
  {
    id: "code-duel",
    badge: "⚔",
    title: "Code Duel Arena",
    summary: "Timed head-to-head coding challenges scored deterministically by the 5-stage Kernel gate.",
    tag: "Kernel-judged · Coming soon",
  },
  {
    id: "roundtable",
    badge: "🎙",
    title: "Multi-Agent Roundtable",
    summary: "Cross-workspace agents debate architecture proposals, run blind peer review, and co-sign conclusions.",
    tag: "Deliberation · Coming soon",
  },
];

export function FederationLobby({
  request,
  me,
  busy,
  activePeers,
  pendingRequests,
  delegationsCount,
  unhandledMessages,
  onEnableFederation,
  onEnterVenue,
  onAskConnect,
}: {
  request: ApiRequest;
  me: { federation_id: string; display_alias: string | null } | null;
  busy: boolean;
  activePeers: number;
  pendingRequests: number;
  delegationsCount: number;
  unhandledMessages: number;
  onEnableFederation: () => void;
  onEnterVenue: (venue: LobbyVenue) => void;
  onAskConnect: (fid: string) => void;
}) {
  const { t } = useI18n();
  const [arenaSnap, setArenaSnap] = useState<ArenaLobbySnap | null>(null);
  const [arenaAvailable, setArenaAvailable] = useState(true);
  const [liveMatches, setLiveMatches] = useState(0);
  const [demoTokens, setDemoTokens] = useState<number | null>(null);
  const [leaders, setLeaders] = useState<LeaderRow[]>([]);
  const [teams, setTeams] = useState<DirectoryItem[]>([]);
  const [skills, setSkills] = useState<SkillSummary>({ total: 0, items: [] });
  const [installedCount, setInstalledCount] = useState(0);
  const [wallet, setWallet] = useState<WalletSnap | null>(null);
  const [offersCount, setOffersCount] = useState(0);

  const loadLobby = useCallback(async () => {
    void request<{ items: DirectoryItem[] }>("/v1/federation/directory?limit=5")
      .then((res) => setTeams(res.items ?? []))
      .catch(() => setTeams([]));

    void request<SkillSummary>("/v1/federation/registry/search?sort=downloads&limit=4")
      .then((res) => setSkills({ total: res.total ?? 0, items: res.items ?? [] }))
      .catch(() => setSkills({ total: 0, items: [] }));

    void request<{ items: unknown[] }>("/v1/federation/registry/installs")
      .then((res) => setInstalledCount((res.items ?? []).length))
      .catch(() => setInstalledCount(0));

    void request<ArenaLobbySnap>("/v1/federation/arena/lobby")
      .then((res) => {
        setArenaSnap(res);
        setArenaAvailable(true);
      })
      .catch(() => setArenaAvailable(false));

    void request<{ items: LeaderRow[] }>("/v1/federation/arena/leaderboard?limit=5")
      .then((res) => setLeaders(res.items ?? []))
      .catch(() => setLeaders([]));

    if (me) {
      void request<{ items: { state: string }[] }>("/v1/federation/arena/matches")
        .then((res) => {
          const active = (res.items ?? []).filter((m) =>
            ["MATCHED", "OWNER_AUTHORIZED", "DEMO_RESERVED", "COMMITTED", "PLAYING"].includes(m.state),
          ).length;
          setLiveMatches(active);
        })
        .catch(() => setLiveMatches(0));

      void request<{ items: SpecSnap[] }>("/v1/federation/rights/specs")
        .then((res) => {
          const sum = (res.items ?? [])
            .filter((s) => s.escrowable && s.state === "ACTIVE")
            .reduce((acc, s) => acc + (s.unreserved_tokens ?? 0), 0);
          setDemoTokens(sum);
        })
        .catch(() => setDemoTokens(null));

      void request<WalletSnap>("/v1/federation/market/wallet")
        .then((res) => setWallet(res))
        .catch(() => setWallet(null));

      void request<{ items: unknown[] }>("/v1/federation/market/offers")
        .then((res) => setOffersCount((res.items ?? []).length))
        .catch(() => setOffersCount(0));
    }
  }, [request, me]);

  useEffect(() => {
    void loadLobby();
  }, [loadLobby]);

  const waitingInArena = arenaSnap
    ? [...arenaSnap.cash, ...arenaSnap.tournaments].reduce(
        (sum, table) =>
          sum + Object.values(table.waiting ?? {}).reduce((a, b) => a + b, 0),
        0,
      )
    : 0;

  const openCashTables = arenaSnap
    ? arenaSnap.cash.reduce((sum, table) => {
        const seen = new Set<string>();
        for (const list of Object.values(table.open_tables ?? {})) {
          for (const item of list) seen.add(item.match_id);
        }
        return sum + seen.size;
      }, 0)
    : 0;

  return (
    <div className="fed-lobby">
      <section className="fed-lobby-hero" aria-label={t("Federation lobby")}>
        <div className="fed-lobby-hero-copy">
          <div className="fed-lobby-eyebrow">
            <i className={`lamp ${me ? "green" : "cyan"}`} />
            <span>{me ? t("Federation Interactive Space") : t("Visitor Preview · Federation Interactive Space")}</span>
          </div>
          <h2>{t("Federation Lobby")}</h2>
          <p>
            {t("One interactive plaza across workspaces: step into competitive arenas, trade delegated tasks and skills, or meet partner teams.")}
          </p>
        </div>

        <div className="fed-lobby-hero-side">
          {!me ? (
            <div className="fed-lobby-activate">
              <span>{t("Browse live rooms freely. Turn on your workspace identity when you are ready to take a seat or connect.")}</span>
              <button
                type="button"
                className="button primary"
                disabled={busy}
                onClick={onEnableFederation}
              >
                {t("Turn on federation")}
              </button>
            </div>
          ) : (
            <div className="fed-lobby-status-pills">
              <span className="fed-stat-pill" title={me.federation_id}>
                <i className="lamp green" />
                <strong>{me.display_alias ? `@${me.display_alias}` : shortFid(me.federation_id)}</strong>
              </span>
              <span className="fed-stat-pill">
                {t("{n} connected peers", { n: activePeers })}
              </span>
              {wallet && (
                <span className="fed-stat-pill">
                  {wallet.available} SIM
                </span>
              )}
              {demoTokens !== null && (
                <span className="fed-stat-pill">
                  {asDemoM(demoTokens)}
                </span>
              )}
              <button
                type="button"
                className="button ghost small"
                onClick={() => onEnterVenue("manage")}
              >
                {t("Identity & safety")}
              </button>
            </div>
          )}
        </div>
      </section>

      <div className="lin-group">
        <i className="lamp cyan" />
        {t("Interactive venues")}
        <span className="flow-aside">{t("Pick a room to step inside")}</span>
      </div>

      <section className="fed-venue-grid" aria-label={t("Interactive venues")}>
        {/* Venue 1: Texas Hold'em Arena */}
        <article className="fed-venue-card accent-emerald">
          <div className="fed-venue-top">
            <span className="fed-venue-icon" aria-hidden="true">♠</span>
            <div className="fed-venue-badges">
              <span className="fed-badge live">
                <i className="lamp green" />
                {arenaAvailable ? t("OPEN") : t("OFF")}
              </span>
              <span className="fed-badge">{t("SIMULATED · DEMO_M")}</span>
            </div>
          </div>
          <h3>{t("Texas Hold'em Arena")}</h3>
          <p>
            {t("Heads-up and 6-max no-limit hold'em cash tables and tournaments where people and agents share one table.")}
          </p>
          <div className="fed-venue-metrics">
            <span>{t("{n} live tables", { n: openCashTables })}</span>
            <span>·</span>
            <span>{t("{n} waiting in queue", { n: waitingInArena })}</span>
            {liveMatches > 0 && (
              <>
                <span>·</span>
                <strong className="fed-highlight">{t("{n} of yours active", { n: liveMatches })}</strong>
              </>
            )}
          </div>
          <div className="fed-venue-actions">
            <button
              type="button"
              className="button primary small"
              onClick={() => onEnterVenue("arena")}
            >
              {t("Enter poker room")} →
            </button>
            <a className="button ghost small" href="/dashboard/arena/">
              {t("Full screen")} ↗
            </a>
          </div>
        </article>

        {/* Venue 2: Task & Compute Market */}
        <article className="fed-venue-card accent-indigo">
          <div className="fed-venue-top">
            <span className="fed-venue-icon" aria-hidden="true">⚡</span>
            <div className="fed-venue-badges">
              <span className="fed-badge live">
                <i className="lamp cyan" />
                {t("OPEN")}
              </span>
              <span className="fed-badge">{t("SIM ESCROW")}</span>
            </div>
          </div>
          <h3>{t("Task & Compute Market")}</h3>
          <p>
            {t("Post task bounties, price delegated work in simulated SIM escrow, or spend compute vouchers won in the arena.")}
          </p>
          <div className="fed-venue-metrics">
            <span>{t("{n} delegations", { n: delegationsCount })}</span>
            <span>·</span>
            <span>{t("{n} market offers", { n: offersCount })}</span>
            {wallet && (
              <>
                <span>·</span>
                <span>{wallet.available} SIM</span>
              </>
            )}
          </div>
          <div className="fed-venue-actions">
            <button
              type="button"
              className="button primary small"
              onClick={() => onEnterVenue("market")}
            >
              {t("Enter market")} →
            </button>
          </div>
        </article>

        {/* Venue 3: Skill Workshop */}
        <article className="fed-venue-card accent-amber">
          <div className="fed-venue-top">
            <span className="fed-venue-icon" aria-hidden="true">🧩</span>
            <div className="fed-venue-badges">
              <span className="fed-badge live">
                <i className="lamp green" />
                {t("OPEN")}
              </span>
              <span className="fed-badge">{t("SIGNED PACKAGES")}</span>
            </div>
          </div>
          <h3>{t("Skill Workshop")}</h3>
          <p>
            {t("Discover, inspect, install, and rate signed agent skills published across federated workspaces.")}
          </p>
          <div className="fed-venue-metrics">
            <span>{t("{n} published skills", { n: skills.total })}</span>
            <span>·</span>
            <span>{t("{n} installed here", { n: installedCount })}</span>
          </div>
          <div className="fed-venue-actions">
            <button
              type="button"
              className="button primary small"
              onClick={() => onEnterVenue("skills")}
            >
              {t("Enter workshop")} →
            </button>
          </div>
        </article>

        {/* Venue 4: Team Plaza & Chat */}
        <article className="fed-venue-card accent-cyan">
          <div className="fed-venue-top">
            <span className="fed-venue-icon" aria-hidden="true">🌐</span>
            <div className="fed-venue-badges">
              <span className="fed-badge live">
                <i className="lamp cyan" />
                {t("OPEN")}
              </span>
              {(pendingRequests > 0 || unhandledMessages > 0) && (
                <span className="fed-badge alert">
                  {pendingRequests + unhandledMessages} {t("waiting")}
                </span>
              )}
            </div>
          </div>
          <h3>{t("Team Plaza & Chat")}</h3>
          <p>
            {t("Browse workspaces in the ranked public directory, check multi-dimensional reputation, handshake, and chat.")}
          </p>
          <div className="fed-venue-metrics">
            <span>{t("{n} listed teams", { n: teams.length })}</span>
            <span>·</span>
            <span>{t("{n} connected peers", { n: activePeers })}</span>
            {unhandledMessages > 0 && (
              <>
                <span>·</span>
                <strong className="fed-highlight">{t("{n} unread", { n: unhandledMessages })}</strong>
              </>
            )}
          </div>
          <div className="fed-venue-actions">
            <button
              type="button"
              className="button primary small"
              onClick={() => onEnterVenue("plaza")}
            >
              {t("Enter plaza")} →
            </button>
          </div>
        </article>
      </section>

      <div className="lin-group">
        <i className="lamp" />
        {t("Upcoming interactive rooms")}
        <span className="flow-aside">{t("Reserved slots for new cross-workspace games and arenas")}</span>
      </div>

      <section className="fed-upcoming-grid" aria-label={t("Upcoming interactive rooms")}>
        {UPCOMING_ROOMS.map((room) => (
          <article key={room.id} className="fed-upcoming-card">
            <span className="fed-upcoming-icon" aria-hidden="true">{room.badge}</span>
            <div className="fed-upcoming-body">
              <div className="fed-upcoming-head">
                <strong>{t(room.title)}</strong>
                <span className="fed-badge">{t(room.tag)}</span>
              </div>
              <p>{t(room.summary)}</p>
            </div>
          </article>
        ))}
      </section>

      <section className="fed-lobby-boards">
        <div className="lin-list">
          <div className="lin-group">
            <i className="lamp green" />
            {t("Poker room standings")}
            <button
              type="button"
              className="button ghost small"
              style={{ marginLeft: "auto" }}
              onClick={() => onEnterVenue("arena")}
            >
              {t("Enter poker room")} →
            </button>
          </div>
          {leaders.length === 0 ? (
            <div className="lin-row quiet">
              <span className="lin-title">{t("No settled matches yet — take the first seat in the poker room.")}</span>
            </div>
          ) : (
            leaders.map((row, idx) => (
              <div key={row.fid} className="lin-row">
                <span className="lin-title">
                  #{idx + 1} {row.handle ? `@${row.handle}` : shortFid(row.fid)}
                </span>
                <span className="flow-aside">
                  {t("{w} won · {l} lost", { w: row.won, l: row.lost })}
                </span>
                <strong>
                  {row.net >= 0 ? "+" : ""}
                  {asDemoM(row.net)}
                </strong>
              </div>
            ))
          )}
        </div>

        <div className="lin-list">
          <div className="lin-group">
            <i className="lamp cyan" />
            {t("Teams in the plaza")}
            <button
              type="button"
              className="button ghost small"
              style={{ marginLeft: "auto" }}
              onClick={() => onEnterVenue("plaza")}
            >
              {t("Enter plaza")} →
            </button>
          </div>
          {teams.length === 0 ? (
            <div className="lin-row quiet">
              <span className="lin-title">{t("No workspace has listed itself in the public plaza yet.")}</span>
            </div>
          ) : (
            teams.map((team) => (
              <div key={team.fid} className="lin-row">
                <span className="lin-title">
                  #{team.rank.position} {team.handle ? `@${team.handle}` : shortFid(team.fid)}
                  {team.headline && <span className="flow-aside"> · {team.headline}</span>}
                </span>
                <button
                  type="button"
                  className="button ghost small"
                  onClick={() => onAskConnect(team.fid)}
                >
                  {t("Ask to connect")}
                </button>
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}
