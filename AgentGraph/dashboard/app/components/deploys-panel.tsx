"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";

/*
 * Deploy gate v2: each project can deploy its merged images to its own server.
 * A target names the deploy computer (`veynod up --deploy` on the server), the
 * branch and the trigger: every release, a time window, or only by hand. CI
 * posts a release per merged commit; a run restarts only the services whose
 * image changed. Only a person turns deploys on, starts one or rolls back.
 */

type Trigger = "auto" | "scheduled" | "manual";
type Schedule = { days: number[]; start: string; end: string; tz: string };
type RunStatus = "queued" | "offered" | "running" | "deployed" | "rolled_back" | "failed" | "skipped";

type Run = {
  run_id: string;
  commit: string | null;
  from_commit: string | null;
  trigger: Trigger | "rollback";
  status: RunStatus;
  plan: { changed: string[]; unchanged: string[]; removed: string[] } | null;
  reason: string | null;
  note: string | null;
  report: { log?: string[]; health?: string | null };
  requested_by: string | null;
  needs_you: boolean;
  created_at: string | null;
  ended_at: string | null;
};

type Target = {
  name: string;
  enabled: boolean;
  branch: string;
  trigger: Trigger;
  schedule: Schedule | null;
  host_id: string | null;
  computer: { host_id: string; agent_id: string | null; state: string } | null;
  health_url: string | null;
  health_timeout_s: number;
  auto_rollback: boolean;
  has_release_token: boolean;
  deployed: { commit: string | null; at: string | null; services: Record<string, unknown> };
  runs: Run[];
};

type ProjectDeploys = {
  project: string;
  targets: Target[];
  releases: { seq: number; commit: string; branch: string; built: string[]; services: string[];
    created_at: string | null }[];
};

type DeployComputer = { host_id: string; agent_id: string | null; state: string };

const STATUS_LABEL: Record<RunStatus, string> = {
  queued: "Waiting",
  offered: "Sent to the server",
  running: "Deploying",
  deployed: "Live",
  rolled_back: "Rolled back",
  failed: "Failed",
  skipped: "Skipped",
};

const STATUS_TONE: Record<RunStatus, string> = {
  queued: "", offered: "cyan", running: "cyan", deployed: "green", rolled_back: "amber",
  failed: "red", skipped: "",
};

const TRIGGER_LABEL: Record<Trigger, string> = {
  auto: "Every merged release",
  scheduled: "In a time window",
  manual: "Only when I press deploy",
};

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const short = (sha: string | null | undefined) => (sha ? sha.slice(0, 7) : "—");

export function DeploysPanel({ request, notify, canManage, refreshKey = 0 }: {
  request: ApiRequest;
  notify: (message: string) => void;
  canManage: boolean;
  /** Bumped when the Loop's stream says something changed (a DEPLOY_ event among them). */
  refreshKey?: number;
}) {
  const { t } = useI18n();
  const [projects, setProjects] = useState<ProjectDeploys[] | null>(null);
  const [computers, setComputers] = useState<DeployComputer[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [token, setToken] = useState<{ project: string; value: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const list = await request<{ name: string }[]>("/v1/projects");
      setProjects(await Promise.all(list.map((project) =>
        request<ProjectDeploys>(`/v1/projects/${encodeURIComponent(project.name)}/deploys`))));
    } catch {
      setProjects([]);
    }
    try {
      const hosts = await request<{ items: { host_id: string; agent_id: string | null; state: string;
        capabilities_report?: { deploy?: boolean } | null }[] }>("/v1/runner/hosts");
      setComputers((hosts.items ?? []).filter((host) => host.capabilities_report?.deploy));
    } catch {
      setComputers([]);
    }
  }, [request]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function call(path: string, body: unknown, done: string, method = "POST") {
    setBusy(true);
    try {
      const result = await request<Record<string, unknown>>(path, { method, body: JSON.stringify(body) });
      notify(t(done));
      await load();
      return result;
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      setBusy(false);
    }
  }

  const targetPath = (project: string, target = "production") =>
    `/v1/projects/${encodeURIComponent(project)}/deploys/${encodeURIComponent(target)}`;

  if (projects === null) {
    return <section className="lin-list" aria-busy="true"><div className="skeleton block" /></section>;
  }
  const live = projects.filter((item) => item.targets.some((target) => target.enabled)).length;
  return (
    <section className="lin-list deploys-view" id="loop-deploys" aria-label={t("Deploys")}>
      <div className="lin-group">
        <i className={`lamp ${live ? "green" : ""}`} />
        {t("Deploys")}
        <span className="lin-count">{t("{n} of {m} projects deploy", { n: live, m: projects.length })}</span>
      </div>
      {projects.length === 0 && (
        <div className="lin-row quiet"><span className="lin-title">{t("No projects yet.")}</span></div>
      )}
      {projects.map((item) => {
        const target = item.targets[0];
        const open = editing === item.project;
        return (
          <div key={item.project} className="deploy-project">
            <div className="lin-row">
              <i className={`lamp ${target?.enabled ? "green" : ""}`} />
              <span className="lin-title">{item.project}</span>
              <span className="flow-aside">
                {target?.enabled
                  ? `${t(TRIGGER_LABEL[target.trigger])} · ${target.branch} · ${t("live")} ${short(target.deployed.commit)}`
                  : t("Deploys are off")}
              </span>
              {canManage && (
                <span className="lin-actions">
                  {target?.enabled && (
                    <button type="button" className="button small" disabled={busy}
                      onClick={() => void call(`${targetPath(item.project, target.name)}/run`, {},
                        "Deploy queued.")}>{t("Deploy newest")}</button>
                  )}
                  <button type="button" className="button ghost small"
                    onClick={() => setEditing(open ? null : item.project)}>{open ? t("Close") : t("Settings")}</button>
                </span>
              )}
            </div>
            {open && canManage && (
              <TargetForm target={target} computers={computers} busy={busy}
                onSave={(body) => void call(targetPath(item.project, target?.name), body, "Deploy settings saved.", "PUT")}
                onToken={async () => {
                  const result = await call(`${targetPath(item.project, target?.name)}/release-token`, {},
                    "New release token made.");
                  if (result?.release_token) setToken({ project: item.project, value: String(result.release_token) });
                }} />
            )}
            {token?.project === item.project && (
              <div className="lin-row quiet deploy-token">
                <span className="lin-title">{t("Save this as the CI secret VEYNOD_RELEASE_TOKEN. It is not shown again.")}</span>
                <code>{token.value}</code>
                <button type="button" className="button ghost small" onClick={() => setToken(null)}>{t("Done")}</button>
              </div>
            )}
            {(target?.runs ?? []).slice(0, 6).map((run) => (
              <div key={run.run_id} className={`lin-row deploy-run${run.needs_you ? " need-row" : ""}`}>
                <i className={`lamp ${STATUS_TONE[run.status]}`} />
                <span className="lin-title">
                  {short(run.commit)} · {t(STATUS_LABEL[run.status])}
                  {run.plan && run.plan.changed.length > 0 && ` · ${t("restarts")} ${run.plan.changed.join(", ")}`}
                  {run.plan && run.plan.changed.length === 0 && run.status === "deployed" && ` · ${t("nothing changed")}`}
                </span>
                <span className="flow-aside">
                  {[run.note || run.reason, run.trigger === "rollback" ? t("rollback") : null,
                    relativeTime(run.ended_at ?? run.created_at)].filter(Boolean).join(" · ")}
                </span>
                {canManage && run.needs_you && (
                  <span className="lin-actions">
                    {run.from_commit && (
                      <button type="button" className="button ghost small" disabled={busy}
                        onClick={() => void call(`${targetPath(item.project, target.name)}/run`,
                          { commit: run.from_commit }, "Rollback queued.")}>{t("Roll back")}</button>
                    )}
                    <button type="button" className="button ghost small" disabled={busy}
                      onClick={() => void call(`/v1/deploys/runs/${run.run_id}/acknowledge`, {}, "Marked as seen.")}>
                      {t("Seen")}
                    </button>
                  </span>
                )}
              </div>
            ))}
          </div>
        );
      })}
      <div className="lin-row quiet station-connect">
        <span className="lin-title">{t("On the server, connect a deploy computer:")}</span>
        <code>npx veynod up --deploy --compose-dir /srv/app --compose-file docker-compose.yml</code>
      </div>
    </section>
  );
}

function TargetForm({ target, computers, busy, onSave, onToken }: {
  target: Target | undefined;
  computers: DeployComputer[];
  busy: boolean;
  onSave: (body: Record<string, unknown>) => void;
  onToken: () => void;
}) {
  const { t } = useI18n();
  const [enabled, setEnabled] = useState(target?.enabled ?? false);
  const [trigger, setTrigger] = useState<Trigger>(target?.trigger ?? "manual");
  const [branch, setBranch] = useState(target?.branch ?? "main");
  const [host, setHost] = useState(target?.host_id ?? computers[0]?.host_id ?? "");
  const [health, setHealth] = useState(target?.health_url ?? "");
  const [rollback, setRollback] = useState(target?.auto_rollback ?? true);
  const [schedule, setSchedule] = useState<Schedule>(target?.schedule
    ?? { days: [1, 2, 3, 4, 5], start: "02:00", end: "04:00", tz: "UTC" });

  return (
    <form className="deploy-form" onSubmit={(event) => {
      event.preventDefault();
      onSave({ enabled, trigger, branch, host_id: host || null, health_url: health || null,
        auto_rollback: rollback, ...(trigger === "scheduled" ? { schedule } : {}) });
    }}>
      <label className="check">
        <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        {t("Deploy this project")}
      </label>
      <label>{t("Deploy computer")}
        <select value={host} onChange={(event) => setHost(event.target.value)}>
          <option value="">{t("Choose one")}</option>
          {computers.map((computer) => (
            <option key={computer.host_id} value={computer.host_id}>
              {computer.agent_id ?? computer.host_id} ({t(computer.state)})
            </option>
          ))}
        </select>
      </label>
      <label>{t("When")}
        <select value={trigger} onChange={(event) => setTrigger(event.target.value as Trigger)}>
          {(Object.keys(TRIGGER_LABEL) as Trigger[]).map((key) => (
            <option key={key} value={key}>{t(TRIGGER_LABEL[key])}</option>
          ))}
        </select>
      </label>
      {trigger === "scheduled" && (
        <fieldset className="deploy-window">
          <legend>{t("Window")}</legend>
          {DAYS.map((day, index) => (
            <label key={day} className="check">
              <input type="checkbox" checked={schedule.days.includes(index + 1)} onChange={(event) =>
                setSchedule({ ...schedule, days: event.target.checked
                  ? [...schedule.days, index + 1].sort() : schedule.days.filter((item) => item !== index + 1) })} />
              {t(day)}
            </label>
          ))}
          <input type="time" value={schedule.start} onChange={(event) => setSchedule({ ...schedule, start: event.target.value })} />
          <input type="time" value={schedule.end} onChange={(event) => setSchedule({ ...schedule, end: event.target.value })} />
          <input value={schedule.tz} aria-label={t("Time zone")}
            onChange={(event) => setSchedule({ ...schedule, tz: event.target.value })} />
        </fieldset>
      )}
      <label>{t("Branch")}
        <input value={branch} onChange={(event) => setBranch(event.target.value)} />
      </label>
      <label>{t("Health check URL")}
        <input value={health} placeholder="https://example.com/healthz" onChange={(event) => setHealth(event.target.value)} />
      </label>
      <label className="check">
        <input type="checkbox" checked={rollback} onChange={(event) => setRollback(event.target.checked)} />
        {t("Roll back when the health check fails")}
      </label>
      <div className="lin-actions">
        <button type="submit" className="button small" disabled={busy}>{t("Save")}</button>
        {target && (
          <button type="button" className="button ghost small" disabled={busy} onClick={onToken}>
            {target.has_release_token ? t("New release token") : t("Make a release token")}
          </button>
        )}
      </div>
    </form>
  );
}
