"use client";

// The colony: the live board drawn as a space the agents live in. Every
// movement is a state the server reported. An idle agent wanders the commons,
// a working one stands at its task's desk and types, a task waiting for review
// puts its holder at the review bench, a task waiting for a person makes its
// holder raise a hand at the gate, and an offline agent sleeps in the corner.
// Nothing moves for decoration only.

import { useEffect, useMemo, useRef, useState } from "react";

import { useI18n } from "../i18n";

export type ColonyAgent = {
  agent_id: string;
  display_name: string;
  role: string | null;
  presence: { state: "online" | "offline" | "degraded" | "unwakeable"; online: boolean; last_seen_at: string | null };
  activity: "working" | "idle" | "offline";
  working_on: string[];
};

export type ColonyCard = {
  task_key: string;
  title: string;
  status: string;
  column: "todo" | "in_progress" | "resolved" | "archive";
  holder_agent: string | null;
  owner_agent: string | null;
  asked_by_agent?: string | null;
  updated_at: string | null;
};

type Zone = "board" | "commons" | "sleep" | "bench" | "review" | "gate" | "shelf";
type Box = { x0: number; x1: number; y0: number; y1: number };

// Fractions of the stage. The layout reads left to right: waiting work and
// resting agents, then the workbench, then review, the gate and done.
const ZONES: Record<Zone, Box> = {
  board: { x0: 0.02, x1: 0.26, y0: 0.07, y1: 0.4 },
  commons: { x0: 0.02, x1: 0.26, y0: 0.46, y1: 0.68 },
  sleep: { x0: 0.02, x1: 0.26, y0: 0.74, y1: 0.97 },
  bench: { x0: 0.31, x1: 0.67, y0: 0.08, y1: 0.95 },
  review: { x0: 0.71, x1: 0.98, y0: 0.07, y1: 0.4 },
  gate: { x0: 0.71, x1: 0.98, y0: 0.46, y1: 0.74 },
  shelf: { x0: 0.71, x1: 0.98, y0: 0.79, y1: 0.97 },
};

const ZONE_LABEL: Record<Zone, string> = {
  board: "Waiting to be claimed",
  commons: "Commons",
  sleep: "Asleep",
  bench: "Workbench",
  review: "Review",
  gate: "Needs you",
  shelf: "Done",
};

const REVIEW = new Set(["REVIEWING", "HANDOFF"]);
const GATE = new Set(["AWAITING_HUMAN", "ESCALATED_TO_HUMAN", "WAITING_HUMAN"]);
const STAGE_H = 500;
const WALK_MS = 1600;

const BODY = ["#7aa7ff", "#ff9f7a", "#8fd18a", "#f2c14e", "#c49bff", "#5fd3c6", "#ff8fb1", "#a3b1c6"];
const EARS = ["cat", "bear", "bunny", "antenna"] as const;

function hash(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function zoneOf(card: ColonyCard): Zone {
  if (card.column === "resolved") return "shelf";
  if (card.column === "todo") return "board";
  if (GATE.has(card.status)) return "gate";
  if (REVIEW.has(card.status)) return "review";
  return "bench";
}

// Desks where an agent stands keep room on their left for it.
const STANDING: Zone[] = ["bench", "review", "gate"];

function slot(zone: Zone, index: number, cols: number, w: number, h: number, cellW: number, cellH: number) {
  const box = ZONES[zone];
  const col = index % cols;
  const row = Math.floor(index / cols);
  const room = STANDING.includes(zone) ? 58 : 8;
  return { x: box.x0 * w + room + col * cellW, y: box.y0 * h + (STANDING.includes(zone) ? 40 : 30) + row * cellH };
}

/** One agent, drawn in code from its address: the same agent always looks the same. */
export function Critter({ agent, size = 52 }: { agent: ColonyAgent; size?: number }) {
  const seed = hash(agent.agent_id);
  const body = BODY[seed % BODY.length];
  const ears = EARS[(seed >>> 3) % EARS.length];
  const asleep = agent.activity === "offline";
  const role = (agent.role ?? "").toUpperCase();
  return (
    <svg className="critter-svg" width={size} height={size * 1.12} viewBox="0 0 64 72" aria-hidden>
      <ellipse cx="32" cy="68" rx="15" ry="3.5" fill="rgba(0,0,0,.45)" />
      <g className="feet">
        <ellipse className="foot l" cx="24" cy="63" rx="6" ry="4" fill={body} style={{ filter: "brightness(.72)" }} />
        <ellipse className="foot r" cx="40" cy="63" rx="6" ry="4" fill={body} style={{ filter: "brightness(.72)" }} />
      </g>
      <g className="ears" fill={body}>
        {ears === "cat" && <><path d="M15 26 18 8 28 20Z" /><path d="M49 26 46 8 36 20Z" /></>}
        {ears === "bear" && <><circle cx="17" cy="20" r="7" /><circle cx="47" cy="20" r="7" /></>}
        {ears === "bunny" && <><ellipse cx="22" cy="12" rx="5" ry="12" /><ellipse cx="42" cy="12" rx="5" ry="12" /></>}
        {ears === "antenna" && <><path d="M32 18V8" stroke={body} strokeWidth="3" strokeLinecap="round" /><circle cx="32" cy="7" r="3.5" /></>}
      </g>
      <g className="arms" fill={body} style={{ filter: "brightness(.85)" }}>
        <ellipse className="arm l" cx="12" cy="44" rx="4.5" ry="7" />
        <ellipse className="arm r" cx="52" cy="44" rx="4.5" ry="7" />
      </g>
      <rect x="12" y="18" width="40" height="46" rx="20" fill={body} />
      <rect x="12" y="50" width="40" height="14" rx="7" fill="rgba(11,15,20,.28)" />
      <g className="face">
        {asleep ? (
          <>
            <path d="M22 37q4 3 8 0M34 37q4 3 8 0" fill="none" stroke="#0b0f14" strokeWidth="2" strokeLinecap="round" />
          </>
        ) : (
          <g className="eyes">
            <ellipse cx="25" cy="36" rx="4" ry="5" fill="#0b0f14" />
            <ellipse cx="39" cy="36" rx="4" ry="5" fill="#0b0f14" />
            <circle cx="26.4" cy="34.2" r="1.4" fill="#fff" />
            <circle cx="40.4" cy="34.2" r="1.4" fill="#fff" />
          </g>
        )}
        <circle cx="19" cy="43" r="2.6" fill="#ff8fb1" opacity=".45" />
        <circle cx="45" cy="43" r="2.6" fill="#ff8fb1" opacity=".45" />
        <path d="M29 44q1.5 1.6 3 0q1.5 1.6 3 0" fill="none" stroke="#0b0f14" strokeWidth="1.6" strokeLinecap="round" />
      </g>
      {/* The hat says the role. */}
      {role === "EXECUTOR" && (
        <g><path d="M15 24a17 13 0 0 1 34 0Z" fill="#f2c14e" /><rect x="11" y="22" width="42" height="4" rx="2" fill="#d9a520" /></g>
      )}
      {role === "AUDITOR" && (
        <g fill="none" stroke="#e5e7eb" strokeWidth="1.8"><circle cx="25" cy="36" r="6.5" /><circle cx="39" cy="36" r="6.5" /><path d="M31.5 36h1" /></g>
      )}
      {role === "ANALYST" && <ellipse cx="34" cy="19" rx="15" ry="5" fill="#7c5cff" transform="rotate(-10 34 19)" />}
      {role === "SCOUT" && (
        <g><path d="M16 23a16 11 0 0 1 32 0Z" fill="#5fd3c6" /><rect x="34" y="21" width="18" height="4" rx="2" fill="#3fb0a4" /></g>
      )}
      {(role === "SUPER_ADMIN" || role === "COMMANDER") && (
        <g fill="none" stroke="#e5e7eb" strokeWidth="2.4"><path d="M13 36a19 19 0 0 1 38 0" /><path d="M51 38q2 8-6 10" /><circle cx="44" cy="48" r="1.6" fill="#e5e7eb" /></g>
      )}
    </svg>
  );
}

export function Colony({
  agents,
  cards,
  pulses,
  motion,
  register,
  onOpenCard,
}: {
  agents: ColonyAgent[];
  cards: ColonyCard[];
  pulses: Record<string, string>;
  motion: boolean;
  register: (key: string) => (node: HTMLElement | null) => void;
  onOpenCard: (taskKey: string) => void;
}) {
  const { t } = useI18n();
  const stageRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(960);
  const [tick, setTick] = useState(0);
  const lastPos = useRef<Map<string, { x: number; y: number; at: number; left: boolean }>>(new Map());

  useEffect(() => {
    const node = stageRef.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // Idle agents pick a new spot now and then; only while motion is on.
  useEffect(() => {
    if (!motion) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 4200);
    return () => window.clearInterval(timer);
  }, [motion]);

  const narrow = width < 760;
  const h = STAGE_H;
  const w = width;

  // Desks and notes: where each card sits.
  const places = useMemo(() => {
    const out = new Map<string, { zone: Zone; x: number; y: number }>();
    const byZone: Record<Zone, ColonyCard[]> = { board: [], commons: [], sleep: [], bench: [], review: [], gate: [], shelf: [] };
    for (const card of cards) byZone[zoneOf(card)].push(card);
    byZone.shelf.sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
    const layout: Array<[Zone, number, number, number, number]> = [
      ["bench", narrow ? 1 : 2, 0.175, 74, 9],
      ["review", 1, 0.25, 50, 4],
      ["gate", 1, 0.25, 50, 2],
      ["shelf", 2, 0.13, 30, 6],
      ["board", 2, 0.12, 30, 8],
    ];
    for (const [zone, cols, cellFrac, cellH, cap] of layout) {
      byZone[zone].slice(0, cap).forEach((card, index) => {
        const p = slot(zone, index, cols, w, h, cellFrac * w, cellH);
        out.set(card.task_key, { zone, ...p });
      });
    }
    return out;
  }, [cards, w, h, narrow]);

  const hidden = useMemo(() => {
    const counts: Partial<Record<Zone, number>> = {};
    const all: Record<string, number> = {};
    for (const card of cards) all[zoneOf(card)] = (all[zoneOf(card)] ?? 0) + 1;
    for (const zone of Object.keys(all) as Zone[]) {
      const shown = Array.from(places.values()).filter((place) => place.zone === zone).length;
      if (all[zone] > shown) counts[zone] = all[zone] - shown;
    }
    return counts;
  }, [cards, places]);

  // Where each agent stands, from what the server says it is doing.
  const spots = useMemo(() => {
    const out = new Map<string, { x: number; y: number; mode: string; task?: ColonyCard }>();
    let sleepers = 0;
    const idle = agents.filter((agent) => agent.activity !== "offline"
      && !agent.working_on.length && !cards.some((card) => (card.holder_agent === agent.agent_id && card.column === "in_progress")
        || (card.asked_by_agent === agent.agent_id && GATE.has(card.status))));
    const idleCols = Math.max(1, Math.min(idle.length, Math.floor(((ZONES.commons.x1 - ZONES.commons.x0) * w - 8) / 58)));
    const byKey = new Map(cards.map((card) => [card.task_key, card]));
    const desks = new Map<string, number>();
    for (const agent of agents) {
      const held = agent.working_on.map((key) => byKey.get(key)).find(Boolean)
        ?? cards.find((card) => card.holder_agent === agent.agent_id && card.column === "in_progress")
        ?? cards.find((card) => card.asked_by_agent === agent.agent_id && GATE.has(card.status));
      const place = held ? places.get(held.task_key) : undefined;
      if (agent.activity === "offline") {
        const cols = Math.max(4, Math.floor(((ZONES.sleep.x1 - ZONES.sleep.x0) * w - 16) / 38));
        const col = sleepers % cols;
        const row = Math.floor(sleepers / cols);
        out.set(agent.agent_id, {
          x: ZONES.sleep.x0 * w + 4 + col * 38,
          y: ZONES.sleep.y0 * h + 14 + row * 40,
          mode: "asleep",
        });
        sleepers += 1;
      } else if (held && place) {
        const n = desks.get(held.task_key) ?? 0;
        desks.set(held.task_key, n + 1);
        const mode = place.zone === "gate" ? "asking" : place.zone === "review" ? "reviewing" : "working";
        out.set(agent.agent_id, { x: place.x - 56 - n * 26, y: place.y - 20, mode, task: held });
      } else {
        // Each idle agent owns a patch of the commons and strolls within it.
        const index = idle.indexOf(agent);
        const box = ZONES.commons;
        const cellW = ((box.x1 - box.x0) * w - 8) / idleCols;
        const rows = Math.max(1, Math.ceil(idle.length / idleCols));
        const cellH = ((box.y1 - box.y0) * h - 70) / rows;
        const seed = hash(`${agent.agent_id}:${motion ? Math.floor((tick + (hash(agent.agent_id) % 3)) / 2) : 0}`);
        // Stay inside the patch so neighbours never overlap.
        const jx = ((seed % 1000) / 1000) * Math.max(0, cellW - 58);
        const jy = (((seed >>> 10) % 1000) / 1000) * Math.max(0, cellH - 10);
        out.set(agent.agent_id, {
          x: box.x0 * w + 4 + (index % idleCols) * cellW + jx,
          y: box.y0 * h + 22 + Math.floor(index / idleCols) * cellH + jy,
          mode: "idle",
        });
      }
    }
    return out;
  }, [agents, cards, places, w, h, tick, motion]);

  // Re-render once the walk is over so the legs stop.
  const [, setSettled] = useState(0);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled((value) => value + 1), WALK_MS + 60);
    return () => window.clearTimeout(timer);
  }, [spots]);

  const now = Date.now();
  return (
    <div className={`colony${motion ? "" : " still"}`} ref={stageRef} style={{ height: h }}>
      {(Object.keys(ZONES) as Zone[]).map((zone) => (
        <div
          key={zone}
          className={`colony-zone zone-${zone}`}
          style={{
            left: `${ZONES[zone].x0 * 100}%`,
            top: `${ZONES[zone].y0 * 100}%`,
            width: `${(ZONES[zone].x1 - ZONES[zone].x0) * 100}%`,
            height: `${(ZONES[zone].y1 - ZONES[zone].y0) * 100}%`,
          }}
        >
          <span className="colony-zone-label">
            {t(ZONE_LABEL[zone])}
            {hidden[zone] ? <em> +{hidden[zone]}</em> : null}
          </span>
        </div>
      ))}

      {cards.map((card) => {
        const place = places.get(card.task_key);
        if (!place) return null;
        const small = place.zone === "board" || place.zone === "shelf";
        return (
          <button
            key={card.task_key}
            type="button"
            ref={register(`card:${card.task_key}`)}
            className={`colony-card zone-${place.zone}${small ? " small" : ""}${pulses[`card:${card.task_key}`] ? ` pulse-${pulses[`card:${card.task_key}`]}` : ""}`}
            style={{ transform: `translate(${place.x}px, ${place.y}px)` }}
            title={`${card.title} · ${t(card.status)}`}
            onClick={() => onOpenCard(card.task_key)}
          >
            {!small && <span className="colony-card-title">{card.title}</span>}
            <span className="colony-card-key">{card.task_key}</span>
          </button>
        );
      })}

      {agents.map((agent) => {
        const spot = spots.get(agent.agent_id);
        if (!spot) return null;
        const prev = lastPos.current.get(agent.agent_id);
        let walking = false;
        let left = prev?.left ?? false;
        if (!prev || Math.abs(prev.x - spot.x) > 2 || Math.abs(prev.y - spot.y) > 2) {
          left = prev ? spot.x < prev.x : false;
          lastPos.current.set(agent.agent_id, { x: spot.x, y: spot.y, at: now, left });
          walking = Boolean(prev) && motion;
        } else if (now - prev.at < WALK_MS) {
          walking = motion;
        }
        const pulseKind = pulses[`agent:${agent.agent_id}`];
        const label = `${agent.display_name} · ${t(
          spot.mode === "asleep" ? "Asleep" : spot.mode === "working" ? "Working" : spot.mode === "reviewing" ? "In review"
            : spot.mode === "asking" ? "Needs you" : "Idle",
        )}${spot.task ? ` · ${spot.task.title}` : ""}`;
        return (
          <button
            key={agent.agent_id}
            type="button"
            ref={register(`agent:${agent.agent_id}`)}
            className={`critter mode-${spot.mode}${walking ? " walking" : ""}${left ? " left" : ""}${agent.presence.state === "unwakeable" ? " unwakeable" : ""}`}
            style={{
              transform: `translate(${spot.x}px, ${spot.y}px)`,
              ["--blink-delay" as string]: `${(hash(agent.agent_id) % 4000) / 1000}s`,
            }}
            aria-label={label}
            title={label}
            onClick={() => spot.task && onOpenCard(spot.task.task_key)}
          >
            <span className="critter-body"><Critter agent={agent} size={spot.mode === "asleep" ? 34 : spot.mode === "idle" ? 44 : 50} /></span>
            {spot.mode === "asking" && <span className="critter-bubble alert">!</span>}
            {spot.mode === "asleep" && <span className="critter-bubble zz">z z</span>}
            {pulseKind === "message" && <span className="critter-bubble talk">…</span>}
            {pulseKind === "online" && <span className="critter-bubble hello">{t("Back")}</span>}
            <span className="critter-name">{agent.display_name}</span>
          </button>
        );
      })}
    </div>
  );
}
