"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { ArchivePanel } from "./archive-panel";
import { AutomationModeCard } from "./automation";
import { Pipeline, Roster, Timeline, waited, type Overview } from "./flow-view";
import { HostsPanel } from "./hosts-panel";
import { useWorkspaceStream, type StreamEvent } from "./use-workspace-stream";

/*
 * The Loop is the engine; this page is its dashboard. It owns no work of its
 * own: the diagrams are the plan, and the Loop moves their pieces along. Three
 * lists side by side say what needs a person, how the engine runs and which
 * computers it has; then the pipeline (every piece of work, grouped the way it
 * was planned, with one state and a time in it), the agents and what just
 * happened. It reloads when the workspace stream says something changed.
 */

type Alert = {
  alert_id: string;
  rule: string;
  severity: "high" | "medium" | "low";
  subject: { kind: string; ref: string };
  last_seen: string;
};

type Unplaced = { placement_id: string; task_key: string | null; reason: string; note: string };
type Round = { status: string; reason: string | null; note: string | null; created_at: string;
  ended_at: string | null };

// Why no runner can take a task, in the owner's words.
const UNPLACED_LABEL: Record<string, string> = {
  no_host_online: "No runner is online",
  no_host_for_role: "No runner can take this kind of task",
  no_host_for_namespace: "No runner may work in this namespace",
  no_host_with_capabilities: "No runner has the skills it needs",
  no_host_for_repo: "No runner can reach its repository",
  no_host_with_runtime: "No runner has Claude Code or Codex ready",
  every_host_failed: "Every runner that could take it already failed it",
  hosts_out_of_quota: "Every runner that could take it is out of quota",
  no_reviewer_host: "No reviewer computer can check it",
  no_supervisor_host: "No supervisor computer looks after the Loop",
};

// Reasons a computer fixes: the action goes to the Computers list.
const NEEDS_A_COMPUTER = new Set(["no_host_online", "no_host_with_runtime", "hosts_out_of_quota",
  "no_reviewer_host", "no_host_for_repo"]);

const ROUND_LABEL: Record<string, string> = {
  offered: "Supervisor round running",
  claimed: "Supervisor round running",
  delivered: "Last supervisor round finished",
  failed: "Last supervisor round failed",
  moved: "Last supervisor round was cut short",
  withdrawn: "Last supervisor round found nothing",
};

const MODE_LABEL: Record<string, string> = {
  manual: "Ask me every step",
  semi: "Small calls automatically",
  full: "Do it all, I only accept the result",
};

const RULE_LABEL: Record<string, string> = {
  stuck_task: "A task's holder is gone",
  retry_storm: "An agent keeps retrying",
  idle_spin: "An agent is spinning idle",
  unclaimed_task: "A task waits for someone",
  diagram_board_mismatch: "The diagram disagrees with the board",
  merge_queue_stall: "The merge queue is stuck",
  repeated_orphan: "A task keeps losing its holder",
  conflict_anomaly: "Agents keep colliding on a document",
  cost_anomaly: "Spending jumped",
  index_backlog: "Search indexing is behind",
  taint_spread: "Untrusted content is spreading",
};

const RELOAD_ON = /^(HOST_|TASK_|CHUNK_|DIAGRAM_|LOOP_ROUND|PLACEMENT_|AGENT_PRESENCE|DEPLOY_|task\.)/;

type Need = { key: string; tone: "red" | "amber" | "violet" | "cyan"; title: string; detail?: string;
  action?: { label: string; run?: () => void; href?: string } };

export function LoopView({
  workspaceId,
  apiBase,
  credential,
  sessionMode,
  request,
  notify,
  waitingReview,
  onOpenTasks,
  onOpenTask,
  onOpenSituation,
  onOpenDiagram,
}: {
  workspaceId: string;
  apiBase: string;
  credential: string;
  sessionMode: boolean;
  request: ApiRequest;
  notify: (message: string) => void;
  waitingReview: number;
  onOpenTasks: () => void;
  onOpenTask: (key: string) => void;
  onOpenSituation: () => void;
  onOpenDiagram: (key: string, chunk?: string) => void;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<string | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [unplaced, setUnplaced] = useState<Unplaced[]>([]);
  const [round, setRound] = useState<Round | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [editingMode, setEditingMode] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [howSupervise, setHowSupervise] = useState(false);
  const [hostsKey, setHostsKey] = useState(0);

  const load = useCallback(async () => {
    try {
      const result = await request<{ items: Alert[] }>(
        `/v1/admin/workspaces/${workspaceId}/alerts?state=open`,
      );
      setAlerts(result.items.filter((item) => item.severity === "high"));
    } catch {
      setAlerts([]);
    }
    try {
      const placements = await request<{ needs_you: Unplaced[] }>(
        "/v1/runner/placements?open_only=true",
      );
      setUnplaced(placements.needs_you);
      const rounds = await request<{ items: Round[] }>(
        "/v1/runner/placements?step=supervise&limit=5",
      );
      setRound(rounds.items.find((item) => item.status !== "unplaced") ?? null);
    } catch {
      setUnplaced([]);
    }
    try {
      setOverview(await request<Overview>("/v1/loop/overview"));
    } catch {
      // keep what is shown; the next event reloads
    }
    if (sessionMode) {
      try {
        setMode((await request<{ mode: string }>("/v1/human/automation")).mode);
      } catch {
        setMode(null);
      }
    }
  }, [request, workspaceId, sessionMode]);

  useEffect(() => {
    void load();
  }, [load]);

  // Reload a moment after the stream says something changed; a burst is one reload.
  const refresh = useRef<number | undefined>(undefined);
  const onEvent = useCallback((event: StreamEvent) => {
    const kind = event.event_type || event.type || "";
    if (!RELOAD_ON.test(kind) && event.table !== "collab_tasks") return;
    window.clearTimeout(refresh.current);
    refresh.current = window.setTimeout(() => {
      setHostsKey((value) => value + 1);
      void load();
    }, 800);
  }, [load]);
  useEffect(() => () => window.clearTimeout(refresh.current), []);
  const live = useWorkspaceStream({
    apiBase,
    credential,
    path: sessionMode
      ? "/v1/stream?tables=collab_tasks,timeline_streams"
      : `/v1/admin/workspaces/${workspaceId}/stream?tables=collab_tasks,timeline_streams`,
    onEvent,
  });

  // --- what needs a person, each with the one thing to do about it.
  const needs: Need[] = [];
  const deploy = overview?.needs_you.deploy ?? [];
  if (deploy.length) {
    needs.push({
      key: "deploy", tone: "violet",
      title: t("{n} approved tasks wait for a deploy", { n: deploy.length }),
      detail: deploy.slice(0, 3).map((item) => item.key).join(", ") + (deploy.length > 3 ? " …" : ""),
      action: { label: t("How to ship"), href: "/dashboard/deploy/" },
    });
  }
  for (const item of (overview?.needs_you.waiting ?? []).slice(0, 3)) {
    needs.push({
      key: `waiting-${item.key}`, tone: "red",
      title: item.asked_by ? t("{who} waits for your answer", { who: item.asked_by }) : t("A task waits for your answer"),
      detail: `${item.title} · ${waited(item.since, t)}`,
      action: { label: t("Answer it"), run: () => onOpenTask(item.key) },
    });
  }
  if (waitingReview > 0) {
    needs.push({
      key: "review", tone: "cyan", title: `${waitingReview} ${t("tasks wait for your review")}`,
      action: { label: t("Review"), run: onOpenTasks },
    });
  }
  const groups = new Map<string, Unplaced[]>();
  for (const item of unplaced) groups.set(item.reason || item.note, [...(groups.get(item.reason || item.note) ?? []), item]);
  for (const [reason, items] of Array.from(groups.entries())) {
    const keys = items.map((item) => item.task_key).filter((key): key is string => Boolean(key));
    const label = t(UNPLACED_LABEL[reason] ?? items[0].note);
    needs.push({
      key: `unplaced-${reason}`, tone: "amber",
      title: items.length > 1 ? t("{label} ({n} tasks)", { label, n: items.length }) : label,
      detail: keys.slice(0, 2).join(", ") + (keys.length > 2 ? " …" : ""),
      action: reason === "no_supervisor_host"
        ? { label: t("How"), run: () => setHowSupervise((value) => !value) }
        : NEEDS_A_COMPUTER.has(reason)
          ? { label: t("Computers"), run: () => document.getElementById("loop-computers")?.scrollIntoView({ behavior: "smooth", block: "center" }) }
          : keys[0] ? { label: t("Open it"), run: () => onOpenTask(keys[0]) } : undefined,
    });
  }
  if (alerts.length) {
    const rules = Array.from(new Set(alerts.map((alert) => t(RULE_LABEL[alert.rule] ?? alert.rule))));
    needs.push({
      key: "alerts", tone: "red",
      title: t("{n} alerts", { n: alerts.length }),
      detail: rules.join(" · "),
      action: { label: t("Situation"), run: onOpenSituation },
    });
  }
  const quotaWaiting = unplaced.filter((item) => item.reason === "hosts_out_of_quota").length;
  const noSupervisor = unplaced.some((item) => item.reason === "no_supervisor_host");
  const today = overview?.today;

  return (
    <div className="loop">
      <div className="loop-pair loop-trio" role="group" aria-label={t("Loop status")}>
        <section className="lin-list">
          <div className="lin-group">
            <i className={`lamp ${needs.length ? "red" : "green"}`} />
            {t("Needs you")}
            <span className="lin-count">{needs.length}</span>
          </div>
          {needs.length === 0 && (
            <div className="lin-row quiet"><span className="lin-title">{t("Nothing right now")}</span></div>
          )}
          {needs.map((need) => (
            <div key={need.key} className="lin-row need-row">
              <i className={`lamp ${need.tone}`} />
              <span className="need-text">
                <b>{need.title}</b>
                {need.detail && <small>{need.detail}</small>}
              </span>
              {need.action?.href && (
                <a className={`button small${need.key === "deploy" ? " primary" : " ghost"}`} href={need.action.href}>
                  {need.action.label}
                </a>
              )}
              {need.action?.run && (
                <button type="button" className="button ghost small" onClick={need.action.run}>{need.action.label}</button>
              )}
            </div>
          ))}
          {howSupervise && (
            <div className="lin-row quiet hosts-empty-row">
              <span className="lin-title">{t("On one computer that should look after the Loop, run:")}</span>
              <code>npx veynod up --supervisor</code>
            </div>
          )}
        </section>

        <section className="lin-list">
          <div className="lin-group">
            <i className="lamp cyan" />
            {t("How the loop runs")}
          </div>
          {mode && (
            <div className="lin-row">
              <i className="lamp" />
              <span className="lin-label">{t("Automation")}</span>
              <span className="lin-title">{t(MODE_LABEL[mode] ?? mode)}</span>
              <button type="button" className="button ghost small"
                aria-expanded={editingMode} onClick={() => setEditingMode((value) => !value)}>
                {editingMode ? t("Done") : t("Change")}
              </button>
            </div>
          )}
          <div className="lin-row">
            <i className={`lamp ${round ? (round.status === "failed" ? "red" : "green") : noSupervisor ? "amber" : ""}`} />
            <span className="lin-label">{t("Supervisor")}</span>
            <span className="need-text">
              <b>{round ? t(ROUND_LABEL[round.status] ?? round.status) : t("No round yet")}</b>
              {round ? <small>{relativeTime(round.ended_at ?? round.created_at)}</small>
                : noSupervisor ? <small>{t("No supervisor computer")}</small> : null}
            </span>
          </div>
          {today && (
            <div className="lin-row">
              <i className="lamp" />
              <span className="lin-label">{t("Last 24 h")}</span>
              <span className="need-text">
                <b>{t("Approved {a} · sent back {r} · done {d}", { a: today.approved, r: today.returned, d: today.done })}</b>
                {today.approved > 0 && today.done === 0 && deploy.length > 0 && (
                  <small>{t("What was approved waits for a deploy")}</small>
                )}
              </span>
            </div>
          )}
          {today && (
            <div className="lin-row flow-spark-row">
              <i className="lamp" />
              <span className="lin-label">{t("Flow")}</span>
              <span className="need-text">
                <Spark hours={today.hours} />
                <small><span className="spark-approved">— {t("approvals")}</span> · <span className="spark-done">— {t("completions")}</span> · {t("per hour")}</small>
              </span>
            </div>
          )}
        </section>

        <HostsPanel request={request} notify={notify} refreshKey={hostsKey} canManage={sessionMode}
          waitingForQuota={quotaWaiting} />
      </div>

      {editingMode && sessionMode && <AutomationModeCard request={request} notify={notify} />}

      {overview ? (
        <Pipeline overview={overview} live={live} onOpenTask={onOpenTask} onOpenDiagram={onOpenDiagram}
          onOpenArchive={() => setArchiveOpen(true)} />
      ) : (
        <section className="lin-list" aria-busy="true"><div className="skeleton block" /></section>
      )}

      {overview && (
        <div className="loop-pair loop-duo">
          <Roster agents={overview.agents} />
          <Timeline events={overview.timeline} onOpenTask={onOpenTask} onOpenDiagram={onOpenDiagram} />
        </div>
      )}

      {archiveOpen && (
        <ArchivePanel
          boardPath={sessionMode ? "/v1/human/blackboard" : `/v1/admin/workspaces/${workspaceId}/blackboard`}
          fetcher={(path) => request(path)}
          onClose={() => setArchiveOpen(false)}
          onOpen={(key) => onOpenTask(key)}
        />
      )}
    </div>
  );
}

/** Approvals and completions per hour over the last day, as two thin lines. */
function Spark({ hours }: { hours: { approved: number; done: number }[] }) {
  const peak = Math.max(1, ...hours.map((hour) => Math.max(hour.approved, hour.done)));
  const line = (pick: (hour: { approved: number; done: number }) => number) =>
    hours.map((hour, index) => `${(index / Math.max(1, hours.length - 1)) * 100},${26 - (pick(hour) / peak) * 22}`).join(" ");
  return (
    <svg className="flow-spark" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">
      <polyline className="spark-approved" points={line((hour) => hour.approved)} />
      <polyline className="spark-done" points={line((hour) => hour.done)} />
    </svg>
  );
}
