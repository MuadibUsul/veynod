"use client";

import { useMemo, useState } from "react";

import { useI18n } from "../i18n";
import { relativeTime } from "../types";
import { AgentFace } from "./agent-face";

/*
 * The Loop's pipeline, its agents and what just happened, from one read
 * (GET /v1/loop/overview). Work is grouped the way it was planned: a diagram,
 * a task with its subtasks, a family of tasks, or the inbox. Every piece has
 * one state and a time in it, so a lane says at a glance where work waits.
 */

export type State = "not_ready" | "ready" | "doing" | "review" | "you" | "deploy" | "done";

export type FlowItem = {
  key: string;
  title: string;
  state: State;
  since: string | null;
  status: string | null;
  holder: string | null;
  where: string | null;
  reviewed_by: string | null;
  task_key?: string | null;
  waiting_on?: string[];
};

export type Lane = {
  lane_id: string;
  kind: "diagram" | "umbrella" | "family" | "inbox";
  key: string;
  title: string | null;
  status?: string;
  items: FlowItem[];
  counts: Record<State, number>;
  total: number;
  stuck: FlowItem | null;
};

export type FlowAgent = {
  agent_id: string;
  display_name: string;
  role: string | null;
  presence: { state: string; online: boolean; last_seen_at?: string | null };
  holding: string[];
  last: { at: string; event_type: string; task_key: string | null } | null;
};

export type FlowEvent = {
  at: string;
  type: string;
  actor: string | null;
  task_key: string | null;
  diagram_key: string | null;
  chunk_key: string | null;
  awaiting_deploy: boolean;
  reason: string | null;
};

export type Overview = {
  lanes: Lane[];
  open: number;
  needs_you: {
    deploy: { key: string; title: string; since: string | null }[];
    // why: "review" when the person is the task's reviewer, "answer" when an agent asked.
    waiting: { key: string; title: string; since: string | null; asked_by: string | null;
      why?: "review" | "answer" }[];
    // Deploy gate v2: failed or rolled-back deploys no one has seen (absent on older servers).
    deploys?: { run_id: string; project: string; target: string; commit: string | null;
      status: "failed" | "rolled_back"; note: string | null; ended_at: string | null }[];
  };
  today: { approved: number; returned: number; done: number; hours: { approved: number; done: number }[] };
  agents: FlowAgent[];
  timeline: FlowEvent[];
};

export const STATES: State[] = ["not_ready", "ready", "doing", "review", "you", "deploy", "done"];

export const STATE_LABEL: Record<State, string> = {
  not_ready: "Not ready yet",
  ready: "Ready to claim",
  doing: "Being worked on",
  review: "Under review",
  you: "Waits for you",
  deploy: "Waits for a deploy",
  done: "Done",
};

const ROLE_LABEL: Record<string, string> = {
  EXECUTOR: "Executor",
  AUDITOR: "Auditor",
  ANALYST: "Analyst",
  SCOUT: "Scout",
  SUPER_ADMIN: "Super admin",
};

// A piece waiting longer than this in a state that should move is stuck.
const STUCK_MS = 2 * 3600 * 1000;

function age(since: string | null): number {
  return since ? Date.now() - new Date(since).getTime() : 0;
}

/** "3 h", "2 d": how long something has waited, short. */
export function waited(since: string | null, t: (key: string, vars?: Record<string, string>) => string): string {
  const ms = age(since);
  if (!since || ms < 60000) return t("just now");
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return t("{n} min", { n: String(minutes) });
  const hours = Math.round(minutes / 60);
  if (hours < 48) return t("{n} h", { n: String(hours) });
  return t("{n} d", { n: String(Math.round(hours / 24)) });
}

function laneTitle(lane: Lane, t: (key: string) => string): string {
  if (lane.kind === "inbox") return t("Inbox");
  return lane.title ?? lane.key;
}

const KIND_LABEL: Record<Lane["kind"], string> = {
  diagram: "Diagram",
  umbrella: "Task with subtasks",
  family: "Family of tasks",
  inbox: "Tasks outside a plan",
};

export function Pipeline({
  overview,
  live,
  onOpenTask,
  onOpenDiagram,
  onOpenArchive,
}: {
  overview: Overview;
  live: boolean;
  onOpenTask: (key: string) => void;
  onOpenDiagram: (key: string, chunk?: string) => void;
  onOpenArchive: () => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [stuckOnly, setStuckOnly] = useState(false);

  const lanes = useMemo(() => {
    const q = query.trim().toLowerCase();
    return overview.lanes
      .map((lane) => {
        if (!q) return lane;
        const items = lane.items.filter((item) => `${item.key} ${item.title} ${item.holder ?? ""} ${item.where ?? ""}`
          .toLowerCase().includes(q));
        const named = `${lane.title ?? ""} ${lane.key}`.toLowerCase().includes(q);
        return named ? lane : items.length ? { ...lane, items } : null;
      })
      .filter((lane): lane is Lane => lane !== null)
      .filter((lane) => !stuckOnly || (lane.stuck !== null && age(lane.stuck.since) > STUCK_MS));
  }, [overview.lanes, query, stuckOnly]);

  return (
    <section className="lin-list flow-lanes" aria-label={t("Pipeline")}>
      <div className="lin-group flow-head">
        <i className={`lamp ${live ? "cyan" : ""}`} title={live ? t("Live") : t("Reconnecting")} />
        {t("Pipeline")}
        <span className="lin-count">{t("{open} open · what needs you first", { open: String(overview.open) })}</span>
        <span className="flow-tools">
          <label className="flow-switch">
            <input type="checkbox" checked={stuckOnly} onChange={(event) => setStuckOnly(event.target.checked)} />
            {t("Only what is stuck")}
          </label>
          <input className="flow-search" value={query} onChange={(event) => setQuery(event.target.value)}
            placeholder={t("Find a task, agent or computer")} aria-label={t("Find a task, agent or computer")} />
          <button type="button" className="button ghost small" onClick={onOpenArchive}>{t("Archive")}</button>
        </span>
      </div>
      <div className="flow-legend" aria-hidden="true">
        {STATES.map((state) => <span key={state}><i className={`flow-dot st-${state}`} />{t(STATE_LABEL[state])}</span>)}
      </div>
      {lanes.length === 0 && (
        <div className="lin-row quiet"><span className="lin-title">{query || stuckOnly ? t("Nothing matches.") : t("No work yet.")}</span></div>
      )}
      {lanes.map((lane) => {
        const expanded = open === lane.lane_id || Boolean(query.trim());
        const counts = STATES.filter((state) => lane.counts[state]);
        return (
          <div key={lane.lane_id} className={`flow-lane-wrap${expanded ? " open" : ""}`}>
            <div className="flow-lane">
              <button type="button" className="flow-lane-main" aria-expanded={expanded}
                onClick={() => setOpen((current) => (current === lane.lane_id ? null : lane.lane_id))}>
                <span className="flow-caret" aria-hidden="true">{expanded ? "⌄" : "›"}</span>
                <span className="flow-name">
                  <b>{laneTitle(lane, t)}</b>
                  <small>{t(KIND_LABEL[lane.kind])} · {lane.kind === "diagram" ? t("{n} chunks", { n: String(lane.total) })
                    : t("{n} tasks", { n: String(lane.total) })}</small>
                </span>
                <span className="flow-stack" aria-hidden="true">
                  {counts.map((state) => <i key={state} className={`st-${state}`} style={{ flexGrow: lane.counts[state] }} />)}
                </span>
                <span className="flow-counts">
                  {counts.map((state) => `${t(STATE_LABEL[state])} ${lane.counts[state]}`).join(" · ")}
                </span>
                <span className="flow-stuck">
                  {lane.stuck ? (
                    <>
                      <b className={`st-text-${lane.stuck.state}`}>{t(STATE_LABEL[lane.stuck.state])} {waited(lane.stuck.since, t)}</b>
                      {" · "}{lane.stuck.title}
                    </>
                  ) : <span className="muted">{t("Nothing waiting")}</span>}
                </span>
              </button>
              <span className="flow-lane-action">
                {lane.kind === "diagram" && (
                  <button type="button" className="button ghost small" onClick={() => onOpenDiagram(lane.key)}>
                    {t("On the diagram")}
                  </button>
                )}
              </span>
            </div>
            {expanded && (
              <div className="flow-items">
                {lane.items.map((item) => (
                  <button key={item.key} type="button" className="flow-item"
                    onClick={() => (lane.kind === "diagram" ? onOpenDiagram(lane.key, item.key) : onOpenTask(item.key))}>
                    <span className={`flow-chip st-${item.state}`}>{t(STATE_LABEL[item.state])}</span>
                    <span className="flow-key">{item.key}</span>
                    <span className="flow-title">{item.title}</span>
                    <span className="flow-who">
                      {item.state === "not_ready" && item.waiting_on?.length
                        ? t("waits for {list}", { list: item.waiting_on.join(", ") })
                        : item.state === "deploy" && item.reviewed_by
                          ? t("approved by {who}", { who: item.reviewed_by })
                          : item.holder ? (
                            <>
                              <AgentFace id={item.holder} size={16} />
                              {item.holder}{item.where ? ` · ${item.where}` : ""}
                            </>
                          ) : null}
                    </span>
                    <span className="flow-age">{item.state === "done" ? "" : waited(item.since, t)}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}

function lastAction(agent: FlowAgent, t: (key: string, vars?: Record<string, string>) => string): string {
  if (agent.holding.length) return t("Holds {list}", { list: agent.holding.join(", ") });
  if (!agent.last) return t("Nothing yet");
  const verb = t(EVENT_VERB[agent.last.event_type] ?? agent.last.event_type);
  return `${verb}${agent.last.task_key ? ` ${agent.last.task_key}` : ""} · ${relativeTime(agent.last.at)}`;
}

export function Roster({ agents }: { agents: FlowAgent[] }) {
  const { t } = useI18n();
  const online = agents.filter((agent) => agent.presence.online).length;
  return (
    <section className="lin-list" aria-label={t("Agents")}>
      <div className="lin-group">
        <i className={`lamp ${online ? "green" : ""}`} />
        {t("Agents")}
        <span className="lin-count">{agents.length} · {online} {t("online")}</span>
        <span className="flow-aside">{t("Connected directly; a computer's seats are under Computers")}</span>
      </div>
      {agents.length === 0 && <div className="lin-row quiet"><span className="lin-title">{t("No agent has connected yet.")}</span></div>}
      {agents.map((agent) => (
        <div key={agent.agent_id} className="lin-row roster-row">
          <AgentFace id={agent.agent_id} role={agent.role} asleep={!agent.presence.online} size={26} />
          <span className="roster-name">
            <b>{agent.display_name}</b>
            <small>
              <i className={`lamp ${agent.presence.online ? "green" : ""}`} />
              {agent.presence.online ? t("Online")
                : agent.presence.state === "unwakeable" ? t("Online only while its session is open")
                  : agent.presence.last_seen_at ? `${t("Offline")} · ${relativeTime(agent.presence.last_seen_at)}` : t("Offline")}
            </small>
          </span>
          <span className="roster-role">{t(ROLE_LABEL[agent.role ?? ""] ?? agent.role ?? "")}</span>
          <span className="lin-title roster-doing">{lastAction(agent, t)}</span>
        </div>
      ))}
    </section>
  );
}

// What an event did, as a verb; the subject and object come from the event.
const EVENT_VERB: Record<string, string> = {
  TASK_CREATED: "published",
  TASK_CLAIMED: "claimed",
  TASK_HANDOFF: "handed off",
  TASK_APPROVED: "approved",
  HUMAN_APPROVED: "approved",
  TASK_REJECTED: "sent back",
  HUMAN_REJECTED: "sent back",
  TASK_RETURNED_FOR_REWORK: "sent back",
  TASK_ESCALATED: "escalated",
  TASK_PLACED: "placed",
  TASK_UNPLACED: "could not place",
  TASK_CANCELLED: "cancelled",
  HUMAN_REVIEW_REQUESTED: "asked you to review",
  HOST_CONNECTED: "connected",
  HOST_PAUSED: "paused",
  HOST_REVOKED: "removed",
  LOOP_ROUND: "was offered a supervisor round",
  LOOP_ROUND_FINISHED: "finished a supervisor round",
  LOOP_ROUND_FAILED: "failed a supervisor round",
  LOOP_ROUND_MOVED: "did not run a supervisor round",
  LOOP_ROUND_SKIPPED: "skipped a supervisor round",
  DEPLOY_VERIFIED: "shipped",
  CHUNK_DISPATCHED: "dispatched",
  CHUNK_SUBMITTED: "submitted",
  CHUNK_MERGED: "merged",
  CHUNK_CI_FAILED: "failed CI on",
  CHUNK_CIRCUIT_BROKEN: "stopped",
  DIAGRAM_CONFIRMED: "confirmed",
  DIAGRAM_CHANGE_PROPOSED: "proposed a change to",
};

// The engine's own workers act as "the Loop", not as agents.
const SYSTEM = new Set(["scheduler", "runners", "lease_reaper", "platform", "system", "deploy_gate",
  "situation", "presence", "timeout_reaper", "escalation_reaper", "dependency_scheduler"]);

const BAD = new Set(["TASK_REJECTED", "HUMAN_REJECTED", "TASK_RETURNED_FOR_REWORK", "TASK_UNPLACED",
  "CHUNK_CI_FAILED", "CHUNK_CIRCUIT_BROKEN", "HOST_REVOKED"]);

export function Timeline({
  events,
  onOpenTask,
  onOpenDiagram,
}: {
  events: FlowEvent[];
  onOpenTask: (key: string) => void;
  onOpenDiagram: (key: string, chunk?: string) => void;
}) {
  const { t } = useI18n();
  const [badOnly, setBadOnly] = useState(false);
  const shown = badOnly ? events.filter((event) => BAD.has(event.type)) : events;
  return (
    <section className="lin-list" aria-label={t("Timeline")}>
      <div className="lin-group">
        <i className="lamp cyan" />
        {t("Timeline")}
        <span className="flow-aside">
          <label className="flow-switch">
            <input type="checkbox" checked={badOnly} onChange={(event) => setBadOnly(event.target.checked)} />
            {t("Only what went wrong")}
          </label>
        </span>
      </div>
      {shown.length === 0 && <div className="lin-row quiet"><span className="lin-title">{t("Nothing yet.")}</span></div>}
      {shown.slice(0, 14).map((event, index) => {
        const object = event.chunk_key && event.diagram_key ? `${event.diagram_key} · ${event.chunk_key}`
          : event.task_key ?? event.diagram_key ?? "";
        const open = () => {
          if (event.diagram_key) onOpenDiagram(event.diagram_key, event.chunk_key ?? undefined);
          else if (event.task_key) onOpenTask(event.task_key);
        };
        return (
          <button key={`${event.at}-${index}`} type="button" className={`lin-row timeline-row${BAD.has(event.type) ? " bad" : ""}`}
            onClick={open} disabled={!object}>
            <span className="timeline-when">{relativeTime(event.at)}</span>
            {event.actor && !SYSTEM.has(event.actor) && !event.type.startsWith("HOST_")
              ? <AgentFace id={event.actor} size={18} /> : <i className={`lamp ${event.type.startsWith("HOST_") ? "green" : "cyan"}`} />}
            <span className="lin-title">
              <b>{!event.actor || SYSTEM.has(event.actor) ? t("The Loop")
                : event.type.startsWith("HOST_") ? `${t("Computer")} ${event.actor}` : event.actor}</b>
              {" "}{t(EVENT_VERB[event.type] ?? event.type)}{" "}
              {object && <code>{object}</code>}
              {event.awaiting_deploy && <span className="muted"> · {t("waits for a deploy")}</span>}
            </span>
          </button>
        );
      })}
    </section>
  );
}
