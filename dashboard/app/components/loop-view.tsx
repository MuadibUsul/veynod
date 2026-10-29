"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { AutomationModeCard } from "./automation";
import { HostsPanel } from "./hosts-panel";
import { LiveBoard, type BoardEvent } from "./live-board";

/*
 * How the agent loop is doing, second only to the diagram. One strip says
 * what needs a person (if anything), then the live board shows the agents at
 * work. Everything else lives under "More".
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

export function LoopView({
  workspaceId,
  apiBase,
  credential,
  sessionMode,
  request,
  notify,
  waitingReview,
  onOpenTasks,
  onOpenSituation,
}: {
  workspaceId: string;
  apiBase: string;
  credential: string;
  sessionMode: boolean;
  request: ApiRequest;
  notify: (message: string) => void;
  waitingReview: number;
  onOpenTasks: () => void;
  onOpenSituation: () => void;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<string | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [unplaced, setUnplaced] = useState<Unplaced[]>([]);
  const [round, setRound] = useState<Round | null>(null);
  const [editingMode, setEditingMode] = useState(false);

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
    const timer = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(timer);
  }, [load]);

  const needs = alerts.length + waitingReview + unplaced.length;

  // Hosts and placements follow the board's own event stream, not a timer.
  const [hostsKey, setHostsKey] = useState(0);
  const refresh = useRef<number | undefined>(undefined);
  const onEvent = useCallback((event: BoardEvent) => {
    const kind = event.event_type || event.type || "";
    if (!/^(HOST_|TASK_PLACED|TASK_UNPLACED|TASK_SCHEDULER|LOOP_ROUND|CHUNK_|AGENT_PRESENCE|task\.claimed)/.test(kind)) {
      return;
    }
    window.clearTimeout(refresh.current);
    refresh.current = window.setTimeout(() => {
      setHostsKey((value) => value + 1);
      void load();
    }, 800);
  }, [load]);
  useEffect(() => () => window.clearTimeout(refresh.current), []);

  return (
    <div className="loop">
      <section className="loop-strip" aria-label={t("Loop status")}>
        {mode && (
          <div className="loop-mode">
            <span className="loop-label">{t("Automation")}</span>
            <strong>{t(MODE_LABEL[mode] ?? mode)}</strong>
            <button type="button" className="button ghost small"
              aria-expanded={editingMode} onClick={() => setEditingMode((value) => !value)}>
              {editingMode ? t("Done") : t("Change")}
            </button>
          </div>
        )}
        {round && (
          <div className="loop-mode loop-round">
            <span className="loop-label">{t("Supervisor")}</span>
            <strong>{t(ROUND_LABEL[round.status] ?? round.status)}</strong>
            <small>{relativeTime(round.ended_at ?? round.created_at)}</small>
          </div>
        )}
        <div className={`loop-needs${needs ? " has" : ""}`}>
          <span className="loop-label">{t("Needs you")}</span>
          {needs === 0 ? (
            <strong>{t("Nothing right now")}</strong>
          ) : (
            <ul>
              {waitingReview > 0 && (
                <li>
                  <button type="button" className="loop-link" onClick={onOpenTasks}>
                    {waitingReview} {t("tasks wait for your review")}
                  </button>
                </li>
              )}
              {unplaced.slice(0, 3).map((item) => (
                <li key={item.placement_id}>
                  <button type="button" className="loop-link" onClick={onOpenTasks}>
                    {t(UNPLACED_LABEL[item.reason] ?? item.note)}
                    {item.task_key && <small> · {item.task_key}</small>}
                  </button>
                </li>
              ))}
              {unplaced.length > 3 && (
                <li>
                  <button type="button" className="loop-link" onClick={onOpenTasks}>
                    {t("and")} {unplaced.length - 3} {t("more tasks wait for a runner")}
                  </button>
                </li>
              )}
              {alerts.slice(0, 3).map((alert) => (
                <li key={alert.alert_id}>
                  <button type="button" className="loop-link" onClick={onOpenSituation}>
                    {t(RULE_LABEL[alert.rule] ?? alert.rule)}
                    <small> · {alert.subject.ref} · {relativeTime(alert.last_seen)}</small>
                  </button>
                </li>
              ))}
              {alerts.length > 3 && (
                <li>
                  <button type="button" className="loop-link" onClick={onOpenSituation}>
                    {t("and")} {alerts.length - 3} {t("more")}
                  </button>
                </li>
              )}
            </ul>
          )}
        </div>
      </section>

      {editingMode && sessionMode && <AutomationModeCard request={request} notify={notify} />}

      <HostsPanel request={request} notify={notify} refreshKey={hostsKey} canManage={sessionMode} />

      <LiveBoard
        apiBase={apiBase}
        credential={credential}
        workspaceId={workspaceId}
        sessionMode={sessionMode}
        onEvent={onEvent}
      />
    </div>
  );
}
