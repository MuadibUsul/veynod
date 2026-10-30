"use client";

// The live blackboard (agentgraph/spec-live-blackboard v1): an observation
// surface. The first screen is one snapshot; every later change arrives as a
// workspace event from the snapshot's cursor, so nothing is polled. The board
// draws what the server says (P-B) and offers no actions: the operations scope
// is the owner's to decide.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useI18n } from "../i18n";
import { normalizeBase } from "../types";
import { Colony } from "./colony";
import { StatusPill, relativeTime } from "./ui";

// Suggested values from the spec, final once the owner confirms them.
const RESOLVED_LIMIT = 10; // M-03
const TICK_MS = 1000; // M-07: events within one second are shown together
const MERGE_ABOVE = 5; // M-07: more than this in one tick → one merged notice
const FLIGHT_MS = 900;
// A parcel is carried a little slower than a dot is thrown.
const HANDOFF_MS = 1300;
const flightMs = (kind: Kind) => (kind === "handoff" ? HANDOFF_MS : FLIGHT_MS);
const MOTION_KEY = "veynod.liveboard.motion";
const SEEN_MEMORY = 5000;
const RESUME_BEHIND = 200;

type Column = "todo" | "in_progress" | "resolved";
const COLUMNS: Column[] = ["todo", "in_progress", "resolved"];
const KIND_LABEL: Record<string, string> = {
  publish: "Published",
  claim: "Claimed",
  message: "Message",
  complete: "Completed",
  handoff: "Handed off",
  online: "Came online",
  offline: "Went offline",
};
const PRESENCE_LABEL: Record<string, string> = {
  online: "Online",
  offline: "Offline",
  degraded: "Degraded",
  unwakeable: "Unwakeable",
};

export type LiveCard = {
  task_key: string;
  title: string;
  status: string;
  column: Column | "archive";
  priority: number;
  namespace: string;
  holder: string | null;
  holder_agent: string | null;
  owner: string | null;
  owner_agent: string | null;
  created_by: string | null;
  created_by_agent: string | null;
  asked_by_agent?: string | null;
  updated_at: string | null;
};

type Presence = {
  state: "online" | "offline" | "degraded" | "unwakeable";
  online: boolean;
  last_seen_at: string | null;
  wakeable?: boolean;
};

type Avatar = {
  agent_id: string;
  display_name: string;
  role: string | null;
  presence: Presence;
  activity: "working" | "idle" | "offline";
  working_on: string[];
  names?: string[];
};

type Snapshot = {
  cursor: number;
  columns: Record<Column, LiveCard[]>;
  archived: number;
  agents: Avatar[];
};

type StreamEvent = {
  id: number;
  table?: string;
  type?: string | null;
  event_type?: string;
  task_key?: string | null;
  status?: string | null;
  actor?: string | null;
  targets?: string[];
  details?: Record<string, unknown> | null;
  timestamp?: string;
};

type Kind = "publish" | "claim" | "message" | "complete" | "handoff" | "online" | "offline";

type Activity = {
  seq: number;
  kind: Kind;
  label: string;
  task_key?: string | null;
  at: string;
};

type Flight = { id: string; kind: Kind; from: DOMRect; to: DOMRect };

type CardEvents = {
  task_key: string;
  events: { seq: number; type: string | null; actor: string | null; status: string | null; at: string | null; summary: string | null }[];
};

// Events name agents by id, principal or token signature; the snapshot lists them all.
function resolver(agents: Record<string, Avatar>): (ref: unknown) => string | null {
  const byName = new Map<string, string>();
  for (const avatar of Object.values(agents)) {
    for (const name of [avatar.agent_id, ...(avatar.names ?? [])]) byName.set(name, avatar.agent_id);
  }
  return (ref) => {
    if (typeof ref !== "string" || !ref) return null;
    const bare = ref.startsWith("agent:") ? ref.slice(6) : ref;
    return byName.get(ref) ?? byName.get(bare) ?? null;
  };
}

function kindOf(event: StreamEvent, previous: LiveCard | undefined): Kind | null {
  const type = event.type ?? "";
  if (event.event_type === "AGENT_PRESENCE_CHANGED" || type === "agent.presence_changed") {
    return (event.details?.state as string) === "online" ? "online" : "offline";
  }
  if (event.event_type === "MESSAGE_SENT" || type === "conversation.message") return "message";
  if (event.table !== "collab_tasks") return null;
  if (type === "task.created" || (type === "task.assigned" && !previous)) return "publish";
  if (type === "task.claimed") return "claim";
  if (type === "task.assigned") return "handoff";
  if (type === "task.status_changed") {
    if (event.status === "DONE") return "complete";
    if (event.status === "REVIEWING" || event.status === "HANDOFF") return "handoff";
  }
  return null;
}

export type BoardEvent = StreamEvent;

export function LiveBoard({
  apiBase,
  credential,
  workspaceId,
  sessionMode,
  onEvent,
}: {
  apiBase: string;
  credential: string;
  workspaceId: string;
  /** Every event the board receives, for panels that follow the same stream. */
  onEvent?: (event: StreamEvent) => void;
  sessionMode: boolean;
}) {
  const { t } = useI18n();
  const base = normalizeBase(apiBase);
  const boardPath = sessionMode
    ? "/v1/human/blackboard"
    : `/v1/admin/workspaces/${workspaceId}/blackboard`;
  const streamPath = sessionMode
    ? "/v1/stream?tables=collab_tasks,timeline_streams"
    : `/v1/admin/workspaces/${workspaceId}/stream?tables=collab_tasks,timeline_streams`;

  const [cards, setCards] = useState<Record<string, LiveCard>>({});
  const [agents, setAgents] = useState<Record<string, Avatar>>({});
  const [archived, setArchived] = useState(0);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [motion, setMotion] = useState(true);
  const [flights, setFlights] = useState<Flight[]>([]);
  const [pulses, setPulses] = useState<Record<string, Kind>>({});
  const [activity, setActivity] = useState<Activity[]>([]);
  const [merged, setMerged] = useState<string | null>(null);
  const [selected, setSelected] = useState<CardEvents | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);

  const cursorRef = useRef<number | null>(null);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const queueRef = useRef<StreamEvent[]>([]);
  const seenRef = useRef<Set<number>>(new Set());
  const settledRef = useRef(0);
  const cardsRef = useRef(cards);
  const motionRef = useRef(motion);
  const nodeRefs = useRef<Map<string, HTMLElement>>(new Map());
  cardsRef.current = cards;
  motionRef.current = motion;

  const authed = useCallback(
    async <T,>(path: string): Promise<T> => {
      const response = await fetch(`${base}${path}`, {
        headers: { Authorization: `Bearer ${credential}` },
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) {
        let message = `HTTP ${response.status}`;
        try {
          message = (await response.json())?.error?.message ?? message;
        } catch {
          // keep the status
        }
        throw new Error(message);
      }
      return (await response.json()) as T;
    },
    [base, credential],
  );

  const register = (key: string) => (node: HTMLElement | null) => {
    if (node) nodeRefs.current.set(key, node);
    else nodeRefs.current.delete(key);
  };

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(MOTION_KEY);
      const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      setMotion(stored === null ? !reduced : stored === "on");
    } catch {
      // storage blocked: keep the default
    }
  }, []);

  const toggleMotion = () => {
    setMotion((value) => {
      try {
        window.localStorage.setItem(MOTION_KEY, value ? "off" : "on");
      } catch {
        // per-viewer convenience only
      }
      return !value;
    });
  };

  const loadSnapshot = useCallback(async () => {
    const snap = await authed<Snapshot>(`${boardPath}?resolved_limit=${RESOLVED_LIMIT}`);
    const next: Record<string, LiveCard> = {};
    for (const column of COLUMNS) for (const card of snap.columns[column]) next[card.task_key] = card;
    const people: Record<string, Avatar> = {};
    for (const avatar of snap.agents) people[avatar.agent_id] = avatar;
    cursorRef.current = snap.cursor;
    settledRef.current = snap.cursor;
    queueRef.current = [];
    seenRef.current = new Set();
    setCards(next);
    setAgents(people);
    setArchived(snap.archived);
    setError(null);
  }, [authed, boardPath]);

  // Flights go from one element to another; a missing end simply skips it.
  const fly = useCallback((kind: Kind, fromKey: string | null, toKey: string | null) => {
    if (!motionRef.current || !fromKey || !toKey) return;
    const from = nodeRefs.current.get(fromKey)?.getBoundingClientRect();
    const to = nodeRefs.current.get(toKey)?.getBoundingClientRect();
    if (!from || !to) return;
    const id = `${kind}-${fromKey}-${toKey}-${Math.random().toString(36).slice(2, 8)}`;
    setFlights((items) => [...items, { id, kind, from, to }]);
    window.setTimeout(() => setFlights((items) => items.filter((item) => item.id !== id)), flightMs(kind) + 50);
  }, []);

  const pulse = useCallback((key: string, kind: Kind) => {
    if (!motionRef.current) return;
    setPulses((items) => ({ ...items, [key]: kind }));
    window.setTimeout(
      () =>
        setPulses((items) => {
          const next = { ...items };
          delete next[key];
          return next;
        }),
      1400,
    );
  }, []);

  // One tick: apply every queued event, then animate (or merge) them.
  const flush = useCallback(async () => {
    const batch = queueRef.current.splice(0, queueRef.current.length);
    if (!batch.length) return;
    const before = cardsRef.current;
    const resolve = resolver(agents);
    const keys = Array.from(
      new Set(batch.filter((event) => event.table === "collab_tasks" && event.task_key).map((event) => event.task_key as string)),
    );
    let fresh: LiveCard[] = [];
    let missing: string[] = [];
    if (keys.length) {
      try {
        const subset = await authed<{ cards: LiveCard[]; missing: string[] }>(
          `${boardPath}?task_keys=${encodeURIComponent(keys.join(","))}`,
        );
        fresh = subset.cards;
        missing = subset.missing;
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        return;
      }
    }
    if (batch.some((event) => event.type === "agent.registered")) {
      await loadSnapshot();
      return;
    }

    let archivedDelta = 0;
    const next = { ...before };
    for (const key of missing) delete next[key];
    for (const card of fresh) {
      if (card.column === "archive") {
        if (next[card.task_key]) archivedDelta += 1;
        delete next[card.task_key];
      } else {
        next[card.task_key] = card;
      }
    }
    // M-03: the resolved column keeps the newest N; older ones move to the archive.
    const resolved = Object.values(next)
      .filter((card) => card.column === "resolved")
      .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
    for (const card of resolved.slice(RESOLVED_LIMIT)) {
      delete next[card.task_key];
      archivedDelta += 1;
    }
    setCards(next);
    if (archivedDelta) setArchived((value) => value + archivedDelta);

    setAgents((current) => {
      const people = { ...current };
      for (const event of batch) {
        if (event.event_type !== "AGENT_PRESENCE_CHANGED") continue;
        const agentId = event.details?.agent_id as string | undefined;
        const state = event.details?.state as Presence["state"] | undefined;
        if (!agentId || !state || !people[agentId]) continue;
        people[agentId] = {
          ...people[agentId],
          presence: {
            ...people[agentId].presence,
            state,
            online: state === "online",
            last_seen_at: (event.details?.last_seen_at as string | null) ?? people[agentId].presence.last_seen_at,
          },
        };
      }
      // Who works on what follows the cards the server just returned.
      for (const id of Object.keys(people)) {
        const working = Object.values(next)
          .filter((card) => card.status === "IN_PROGRESS" && card.holder_agent === id)
          .map((card) => card.task_key);
        const online = people[id].presence.online;
        people[id] = {
          ...people[id],
          working_on: working,
          activity: online ? (working.length ? "working" : "idle") : "offline",
        };
      }
      return people;
    });

    const recent = batch.filter((event) => event.id > settledRef.current);
    const described: Activity[] = [];
    for (const event of recent) {
      const kind = kindOf(event, before[event.task_key ?? ""]);
      if (!kind) continue;
      const card = event.task_key ? next[event.task_key] ?? fresh.find((item) => item.task_key === event.task_key) : undefined;
      let label = card?.title ?? "";
      if (kind === "message") {
        const from = resolve(event.details?.sender ?? event.actor) ?? event.actor ?? "?";
        const to = (event.targets ?? []).map((target) => resolve(target) ?? target).join(", ");
        label = `${from} → ${to}`;
      } else if (kind === "online" || kind === "offline") {
        label = String(event.details?.agent_id ?? "");
      }
      described.push({
        seq: event.id,
        kind,
        task_key: event.task_key,
        at: event.timestamp ?? new Date().toISOString(),
        label: label || event.task_key || "",
      });
    }
    if (!described.length) return;
    setActivity((items) => [...described.reverse(), ...items].slice(0, 30));

    if (described.length > MERGE_ABOVE) {
      // M-07: a burst becomes one notice; the state above is already complete.
      setMerged(t("{count} events in the last second", { count: String(described.length) }));
      window.setTimeout(() => setMerged(null), 2500);
      return;
    }
    // Let the new layout render before measuring where things fly.
    window.requestAnimationFrame(() => {
      for (const event of recent) {
        const kind = kindOf(event, before[event.task_key ?? ""]);
        if (!kind) continue;
        const key = event.task_key ?? "";
        const card = next[key];
        if (kind === "online" || kind === "offline") {
          const agentId = event.details?.agent_id as string | undefined;
          if (agentId) pulse(`agent:${agentId}`, kind);
        } else if (kind === "message") {
          const from = resolve(event.details?.sender ?? event.actor);
          for (const target of event.targets ?? []) {
            const to = resolve(target);
            if (from && to) fly("message", `agent:${from}`, `agent:${to}`);
            if (to) pulse(`agent:${to}`, "message");
          }
        } else if (card) {
          pulse(`card:${key}`, kind);
          if (kind === "publish") fly(kind, card.created_by_agent ? `agent:${card.created_by_agent}` : null, `card:${key}`);
          if (kind === "claim") fly(kind, `card:${key}`, card.holder_agent ? `agent:${card.holder_agent}` : null);
          if (kind === "complete") fly(kind, card.owner_agent ? `agent:${card.owner_agent}` : null, `card:${key}`);
          if (kind === "handoff") {
            const previous = before[key];
            fly(kind, previous?.owner_agent ? `agent:${previous.owner_agent}` : null,
              card.owner_agent ? `agent:${card.owner_agent}` : `card:${key}`);
          }
        }
      }
    });
  }, [agents, authed, boardPath, fly, loadSnapshot, pulse, t]);

  // Snapshot first, then follow the stream from its cursor.
  useEffect(() => {
    let stopped = false;
    const controller = new AbortController();
    let retry: number | undefined;
    let lostAt: number | null = null;

    async function follow() {
      if (stopped) return;
      try {
        if (cursorRef.current === null || (lostAt && Date.now() - lostAt > 60000)) {
          await loadSnapshot();
        }
        const headers: Record<string, string> = { Authorization: `Bearer ${credential}` };
        // Resume a little behind the cursor for commits that landed out of id
        // order; what the snapshot already shows only refreshes, never animates.
        if (cursorRef.current !== null) {
          headers["Last-Event-ID"] = String(Math.max(0, cursorRef.current - RESUME_BEHIND));
        }
        const response = await fetch(`${base}${streamPath}`, { headers, signal: controller.signal });
        if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`);
        setLive(true);
        lostAt = null;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let split = buffer.indexOf("\n\n");
          while (split >= 0) {
            const frame = buffer.slice(0, split);
            buffer = buffer.slice(split + 2);
            split = buffer.indexOf("\n\n");
            let name = "message";
            let data = "";
            for (const line of frame.split("\n")) {
              if (line.startsWith("event:")) name = line.slice(6).trim();
              else if (line.startsWith("data:")) data += line.slice(5).trim();
            }
            if (name !== "workspace" || !data) continue;
            try {
              const event = JSON.parse(data) as StreamEvent;
              // A later commit can carry a lower id: dedupe by what arrived,
              // not by the highest id seen.
              if (seenRef.current.has(event.id)) continue;
              seenRef.current.add(event.id);
              if (seenRef.current.size > SEEN_MEMORY) {
                seenRef.current.delete(seenRef.current.values().next().value as number);
              }
              cursorRef.current = Math.max(cursorRef.current ?? 0, event.id);
              queueRef.current.push(event);
              onEventRef.current?.(event);
            } catch {
              // a malformed frame is skipped; the next snapshot corrects it
            }
          }
        }
      } catch (err) {
        if (stopped) return;
        if (err instanceof Error && !err.message.startsWith("SSE")) setError(err.message);
      }
      setLive(false);
      if (lostAt === null) lostAt = Date.now();
      if (!stopped) retry = window.setTimeout(follow, 3000);
    }

    cursorRef.current = null;
    void follow();
    return () => {
      stopped = true;
      controller.abort();
      window.clearTimeout(retry);
    };
  }, [base, credential, loadSnapshot, streamPath]);

  useEffect(() => {
    const timer = window.setInterval(() => void flush(), TICK_MS);
    return () => window.clearInterval(timer);
  }, [flush]);

  const openCard = async (taskKey: string) => {
    try {
      setSelected(await authed<CardEvents>(`${boardPath}/tasks/${encodeURIComponent(taskKey)}/events`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const counts = Object.values(agents).reduce(
    (sum, avatar) => ({ ...sum, [avatar.activity]: (sum[avatar.activity] ?? 0) + 1 }),
    {} as Record<string, number>,
  );

  return (
    <div className="tab-stack live-board">
      <section className="card live-head">
        <div className="live-head-row">
          <div>
            <h2>{t("The space")}</h2>
            <p>{t("Your agents, live: who rests, who works on what, who waits for you.")}</p>
          </div>
          <div className="live-head-actions">
            <span className={`live-dot ${live ? "on" : ""}`}>{live ? t("Live") : t("Reconnecting")}</span>
            <span className="count-chip">{t("Working")} {counts.working ?? 0}</span>
            <span className="count-chip">{t("Idle")} {counts.idle ?? 0}</span>
            <span className="count-chip">{t("Offline")} {counts.offline ?? 0}</span>
            <button type="button" className="button ghost small" onClick={toggleMotion} aria-pressed={motion}>
              {motion ? t("Animations on") : t("Animations off")}
            </button>
            <button type="button" className="button ghost small" onClick={() => setArchiveOpen(true)}>
              {t("Archive")} · {archived}
            </button>
          </div>
        </div>
        {error && <div className="live-error">{error}</div>}
        {merged && <div className="live-merged" role="status">{merged}</div>}
      </section>

      <Colony
        agents={Object.values(agents)}
        cards={Object.values(cards)}
        pulses={pulses}
        motion={motion}
        register={register}
        onOpenCard={(key) => void openCard(key)}
      />

      <section className="card live-activity">
        <div className="card-head compact">
          <h2>{t("Recent activity")}</h2>
        </div>
        {activity.length === 0 ? (
          <p className="live-empty">{t("Events appear here as they happen.")}</p>
        ) : (
          <ol className="live-feed">
            {activity.map((item) => (
              <li key={`${item.seq}-${item.kind}`}>
                <span className={`live-kind kind-${item.kind}`}>{t(KIND_LABEL[item.kind])}</span>
                {item.task_key ? (
                  <button type="button" className="live-link" onClick={() => void openCard(item.task_key as string)}>
                    {item.label}
                  </button>
                ) : (
                  <span>{item.label}</span>
                )}
                <span className="live-seq">evt_{item.seq}</span>
              </li>
            ))}
          </ol>
        )}
      </section>

      {flights.map((flight) => (
        <FlightDot key={flight.id} flight={flight} />
      ))}

      {selected && (
        <div className="live-drawer" role="dialog" aria-label={selected.task_key}>
          <div className="live-drawer-head">
            <h3>{cards[selected.task_key]?.title ?? selected.task_key}</h3>
            <button type="button" className="button ghost small" onClick={() => setSelected(null)}>{t("Close")}</button>
          </div>
          <p className="live-seq">{selected.task_key}</p>
          <ol className="live-feed">
            {selected.events.map((event) => (
              <li key={event.seq}>
                <span className="live-seq">evt_{event.seq}</span>
                <span>{event.type}</span>
                <span>{event.actor}</span>
                <span className="live-seq">{relativeTime(event.at)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {archiveOpen && (
        <ArchivePanel boardPath={boardPath} fetcher={authed} onClose={() => setArchiveOpen(false)} onOpen={openCard} />
      )}
    </div>
  );
}

function FlightDot({ flight }: { flight: Flight }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const fromX = flight.from.left + flight.from.width / 2;
    const fromY = flight.from.top + flight.from.height / 2;
    const toX = flight.to.left + flight.to.width / 2;
    const toY = flight.to.top + flight.to.height / 2;
    // Things are carried in an arc, not slid: a parcel is lifted, then set down.
    const lift = Math.min(80, 24 + Math.hypot(toX - fromX, toY - fromY) / 6);
    node.animate(
      [
        { transform: `translate(${fromX}px, ${fromY}px) scale(.7)`, opacity: 0 },
        { transform: `translate(${fromX}px, ${fromY - 12}px) scale(1)`, opacity: 1, offset: 0.12 },
        { transform: `translate(${(fromX + toX) / 2}px, ${Math.min(fromY, toY) - lift}px) scale(1.05)`, opacity: 1, offset: 0.55 },
        { transform: `translate(${toX}px, ${toY}px) scale(.85)`, opacity: 0 },
      ],
      { duration: flightMs(flight.kind), easing: "cubic-bezier(.3,.6,.3,1)", fill: "forwards" },
    );
  }, [flight]);
  return (
    <span ref={ref} className={`live-flight kind-${flight.kind}`} aria-hidden="true">
      {flight.kind === "handoff" && (
        <svg viewBox="0 0 20 20" width="20" height="20">
          <rect x="2" y="6" width="16" height="12" rx="2" fill="#c9a26b" />
          <rect x="1" y="4" width="18" height="4" rx="1.5" fill="#dcb67e" />
          <rect x="9" y="4" width="2" height="14" fill="#00c2ff" />
        </svg>
      )}
    </span>
  );
}

function ArchivePanel({
  boardPath,
  fetcher,
  onClose,
  onOpen,
}: {
  boardPath: string;
  fetcher: <T>(path: string) => Promise<T>;
  onClose: () => void;
  onOpen: (taskKey: string) => void;
}) {
  const { t } = useI18n();
  const [q, setQ] = useState("");
  const [agent, setAgent] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [items, setItems] = useState<LiveCard[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = useCallback(
    async (cursor: string | null) => {
      setBusy(true);
      setError(null);
      const params = new URLSearchParams({ resolved_limit: String(RESOLVED_LIMIT), limit: "50" });
      if (q.trim()) params.set("q", q.trim());
      if (agent.trim()) params.set("agent", agent.trim());
      if (since) params.set("since", new Date(since).toISOString());
      if (until) params.set("until", new Date(until).toISOString());
      if (cursor) params.set("cursor", cursor);
      try {
        const page = await fetcher<{ items: LiveCard[]; next_cursor: string | null }>(`${boardPath}/archive?${params}`);
        setItems((current) => (cursor ? [...current, ...page.items] : page.items));
        setNext(page.next_cursor);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [agent, boardPath, fetcher, q, since, until],
  );

  useEffect(() => {
    void search(null);
    // first page on open only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="live-drawer live-archive" role="dialog" aria-label={t("Archive")}>
      <div className="live-drawer-head">
        <h3>{t("Archive")}</h3>
        <button type="button" className="button ghost small" onClick={onClose}>{t("Close")}</button>
      </div>
      <form
        className="live-archive-form"
        onSubmit={(event) => {
          event.preventDefault();
          void search(null);
        }}
      >
        <input value={q} onChange={(event) => setQ(event.target.value)} placeholder={t("Keyword")} />
        <input value={agent} onChange={(event) => setAgent(event.target.value)} placeholder={t("Agent")} />
        <label>
          {t("From")}
          <input type="datetime-local" value={since} onChange={(event) => setSince(event.target.value)} />
        </label>
        <label>
          {t("To")}
          <input type="datetime-local" value={until} onChange={(event) => setUntil(event.target.value)} />
        </label>
        <button type="submit" className="button primary small" disabled={busy}>{t("Search")}</button>
      </form>
      {error && <div className="live-error">{error}</div>}
      <ol className="live-feed">
        {items.map((card) => (
          <li key={card.task_key}>
            <StatusPill status={card.status} />
            <button type="button" className="live-link" onClick={() => onOpen(card.task_key)}>{card.title}</button>
            <span>{card.owner ?? "—"}</span>
            <span className="live-seq">{relativeTime(card.updated_at)}</span>
          </li>
        ))}
      </ol>
      {items.length === 0 && !busy && <p className="live-empty">{t("No archived tasks match.")}</p>}
      {next && (
        <button type="button" className="button ghost small" disabled={busy} onClick={() => void search(next)}>
          {t("Load more")}
        </button>
      )}
    </div>
  );
}
