"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { SettingRow, SettingsFooter, SettingsGroup, Switch } from "./settings";

/*
 * Settings > Deploys (deploy gate v2): each project can deploy its merged
 * images to its own servers. A target names the deploy computer (`veynod up
 * --deploy` on the server), the branch and the trigger: every release, a time
 * window, or only by hand. CI posts a release per merged commit; a run restarts
 * only the services whose image changed. Only a person turns deploys on,
 * starts one or rolls back.
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
// `up --deploy` first shipped in veynod 0.6.0. `@latest` keeps an older
// global install from answering instead (it does not know the flag).
const DEPLOY_CLI = "0.6.0";
const DEPLOY_DIR = "/path/to/app";
export const DEPLOY_COMMAND =
  `npx veynod@latest up --deploy --compose-dir ${DEPLOY_DIR} --compose-file docker-compose.yml`;
const short = (sha: string | null | undefined) => (sha ? sha.slice(0, 7) : "—");

export function DeploysSettings({ request, notify, canManage }: {
  request: ApiRequest;
  notify: (message: string) => void;
  canManage: boolean;
}) {
  const { t } = useI18n();
  const [projects, setProjects] = useState<ProjectDeploys[] | null>(null);
  const [computers, setComputers] = useState<DeployComputer[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [token, setToken] = useState<{ key: string; value: string } | null>(null);
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
  }, [load]);

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

  const targetPath = (project: string, target: string) =>
    `/v1/projects/${encodeURIComponent(project)}/deploys/${encodeURIComponent(target)}`;

  if (projects === null) {
    return (
      <SettingsGroup>
        <div className="setting-row"><div className="skeleton line w40" /></div>
      </SettingsGroup>
    );
  }

  return (
    <>
      {projects.length === 0 && (
        <SettingsGroup title={t("Projects")}>
          <SettingRow
            label={t("No projects yet.")}
            description={t("Create a project first; each project gets its own deploy targets.")}
          />
        </SettingsGroup>
      )}

      {projects.map((item) => {
        const live = item.targets.filter((target) => target.enabled).length;
        return (
          <SettingsGroup
            key={item.project}
            title={item.project}
            description={item.targets.length
              ? t("{n} of {m} targets deploy", { n: live, m: item.targets.length })
              : t("No deploy target yet")}
          >
            {item.targets.length === 0 && (
              <SettingRow
                label={t("Deploys are off")}
                description={t("Set up a target to send this project's merged images to a server.")}
              >
                {canManage && (
                  <button type="button" className="button ghost small"
                    onClick={() => setEditing(`${item.project}/production`)}>
                    {t("Set up")}
                  </button>
                )}
              </SettingRow>
            )}
            {editing === `${item.project}/production` && item.targets.length === 0 && (
              <TargetForm target={undefined} computers={computers} busy={busy}
                onCancel={() => setEditing(null)}
                onSave={(body) => void call(targetPath(item.project, "production"), body,
                  "Deploy settings saved.", "PUT").then((saved) => saved && setEditing(null))}
                onToken={() => undefined} />
            )}

            {item.targets.map((target) => {
              const key = `${item.project}/${target.name}`;
              const open = editing === key;
              return (
                <div key={key} className="setting-block">
                  <SettingRow
                    lamp={target.enabled ? "green" : ""}
                    label={target.name}
                    description={target.enabled
                      ? `${t(TRIGGER_LABEL[target.trigger])} · ${target.branch} · ${t("live")} ${short(target.deployed.commit)}`
                        + (target.computer ? ` · ${target.computer.agent_id ?? target.computer.host_id}` : "")
                      : t("Deploys are off")}
                  >
                    {canManage && target.enabled && (
                      <button type="button" className="button primary small" disabled={busy}
                        onClick={() => void call(`${targetPath(item.project, target.name)}/run`, {},
                          "Deploy queued.")}>{t("Deploy newest")}</button>
                    )}
                    {canManage && (
                      <button type="button" className="button ghost small"
                        aria-expanded={open}
                        onClick={() => setEditing(open ? null : key)}>{open ? t("Close") : t("Configure")}</button>
                    )}
                  </SettingRow>

                  {open && canManage && (
                    <TargetForm target={target} computers={computers} busy={busy}
                      onCancel={() => setEditing(null)}
                      onSave={(body) => void call(targetPath(item.project, target.name), body,
                        "Deploy settings saved.", "PUT")}
                      onToken={async () => {
                        const result = await call(`${targetPath(item.project, target.name)}/release-token`, {},
                          "New release token made.");
                        if (result?.release_token) setToken({ key, value: String(result.release_token) });
                      }} />
                  )}

                  {token?.key === key && (
                    <SettingRow
                      stack
                      label={t("Release token")}
                      description={t("Save this as the CI secret VEYNOD_RELEASE_TOKEN. It is not shown again.")}
                    >
                      <code className="setting-code">{token.value}</code>
                      <button type="button" className="button ghost small" onClick={() => setToken(null)}>{t("Done")}</button>
                    </SettingRow>
                  )}

                  {target.runs.slice(0, 5).map((run) => (
                    <SettingRow
                      key={run.run_id}
                      lamp={STATUS_TONE[run.status]}
                      tone={run.needs_you ? "need" : undefined}
                      label={
                        <span className="setting-sub">
                          {short(run.commit)} · {t(STATUS_LABEL[run.status])}
                          {run.plan && run.plan.changed.length > 0 && ` · ${t("restarts")} ${run.plan.changed.join(", ")}`}
                          {run.plan && run.plan.changed.length === 0 && run.status === "deployed" && ` · ${t("nothing changed")}`}
                        </span>
                      }
                      description={[run.note || run.reason, run.trigger === "rollback" ? t("rollback") : null,
                        relativeTime(run.ended_at ?? run.created_at)].filter(Boolean).join(" · ")}
                    >
                      {canManage && run.needs_you && (
                        <>
                          {run.from_commit && (
                            <button type="button" className="button ghost small" disabled={busy}
                              onClick={() => void call(`${targetPath(item.project, target.name)}/run`,
                                { commit: run.from_commit }, "Rollback queued.")}>{t("Roll back")}</button>
                          )}
                          <button type="button" className="button ghost small" disabled={busy}
                            onClick={() => void call(`/v1/deploys/runs/${run.run_id}/acknowledge`, {}, "Marked as seen.")}>
                            {t("Seen")}
                          </button>
                        </>
                      )}
                    </SettingRow>
                  ))}
                </div>
              );
            })}
          </SettingsGroup>
        );
      })}

      <SettingsGroup
        title={t("Deploy computer")}
        description={t("The server that runs your app connects out to Veynod; it needs no open port and Veynod holds no SSH key.")}
      >
        <SettingRow
          stack
          label={t("On the server, connect a deploy computer:")}
          description={
            <>
              {computers.length
                ? t("{n} deploy computer(s) connected", { n: computers.length })
                : t("No deploy computer is connected yet.")}
              <span className="setting-desc-line">
                {t("Needs veynod {version} or newer. {dir} is an example: use the folder that holds your compose file, and name the file if it is not docker-compose.yml.",
                  { version: DEPLOY_CLI, dir: DEPLOY_DIR })}
              </span>
            </>
          }
        >
          <code className="setting-code">{DEPLOY_COMMAND}</code>
        </SettingRow>
      </SettingsGroup>
    </>
  );
}

function TargetForm({ target, computers, busy, onSave, onToken, onCancel }: {
  target: Target | undefined;
  computers: DeployComputer[];
  busy: boolean;
  onSave: (body: Record<string, unknown>) => void;
  onToken: () => void;
  onCancel: () => void;
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
  const id = (field: string) => `deploy-${target?.name ?? "new"}-${field}`;

  return (
    <form className="setting-form" onSubmit={(event) => {
      event.preventDefault();
      onSave({ enabled, trigger, branch, host_id: host || null, health_url: health || null,
        auto_rollback: rollback, ...(trigger === "scheduled" ? { schedule } : {}) });
    }}>
      <SettingRow label={t("Deploy to this target")} description={t("When off, nothing reaches this target.")}>
        <Switch checked={enabled} onChange={setEnabled} label={t("Deploy to this target")} />
      </SettingRow>
      <SettingRow htmlFor={id("computer")} label={t("Deploy computer")}
        description={t("The computer on the server that pulls the images and restarts the services.")}>
        <select id={id("computer")} value={host} onChange={(event) => setHost(event.target.value)}>
          <option value="">{t("Choose one")}</option>
          {computers.map((computer) => (
            <option key={computer.host_id} value={computer.host_id}>
              {computer.agent_id ?? computer.host_id} ({t(computer.state)})
            </option>
          ))}
        </select>
      </SettingRow>
      <SettingRow htmlFor={id("when")} label={t("When")} description={t("What starts a deploy.")}>
        <select id={id("when")} value={trigger} onChange={(event) => setTrigger(event.target.value as Trigger)}>
          {(Object.keys(TRIGGER_LABEL) as Trigger[]).map((key) => (
            <option key={key} value={key}>{t(TRIGGER_LABEL[key])}</option>
          ))}
        </select>
      </SettingRow>
      {trigger === "scheduled" && (
        <SettingRow stack label={t("Window")} description={t("Days and hours when a waiting release may go out.")}>
          <div className="setting-days" role="group" aria-label={t("Window")}>
            {DAYS.map((day, index) => {
              const on = schedule.days.includes(index + 1);
              return (
                <button key={day} type="button" aria-pressed={on} className={`setting-chip${on ? " on" : ""}`}
                  onClick={() => setSchedule({ ...schedule, days: on
                    ? schedule.days.filter((item) => item !== index + 1)
                    : [...schedule.days, index + 1].sort() })}>
                  {t(day)}
                </button>
              );
            })}
          </div>
          <div className="setting-inline">
            <input type="time" aria-label={t("From")} value={schedule.start}
              onChange={(event) => setSchedule({ ...schedule, start: event.target.value })} />
            <span className="muted">–</span>
            <input type="time" aria-label={t("Until")} value={schedule.end}
              onChange={(event) => setSchedule({ ...schedule, end: event.target.value })} />
            <input className="setting-tz" value={schedule.tz} aria-label={t("Time zone")}
              onChange={(event) => setSchedule({ ...schedule, tz: event.target.value })} />
          </div>
        </SettingRow>
      )}
      <SettingRow htmlFor={id("branch")} label={t("Branch")} description={t("Only releases from this branch deploy.")}>
        <input id={id("branch")} value={branch} onChange={(event) => setBranch(event.target.value)} />
      </SettingRow>
      <SettingRow htmlFor={id("health")} label={t("Health check URL")}
        description={t("Checked after every deploy.")}>
        <input id={id("health")} value={health} placeholder="https://example.com/healthz"
          onChange={(event) => setHealth(event.target.value)} />
      </SettingRow>
      <SettingRow label={t("Roll back when the health check fails")}
        description={t("Puts the previous release back on its own.")}>
        <Switch checked={rollback} onChange={setRollback} label={t("Roll back when the health check fails")} />
      </SettingRow>
      {target && (
        <SettingRow label={t("Release token")}
          description={t("CI posts each merged release with it, as the secret VEYNOD_RELEASE_TOKEN.")}>
          <button type="button" className="button ghost small" disabled={busy} onClick={onToken}>
            {target.has_release_token ? t("New release token") : t("Make a release token")}
          </button>
        </SettingRow>
      )}
      <SettingsFooter>
        <button type="button" className="button ghost small" onClick={onCancel}>{t("Cancel")}</button>
        <button type="submit" className="button primary small" disabled={busy}>{t("Save")}</button>
      </SettingsFooter>
    </form>
  );
}
