"use client";

import { useCallback, useEffect, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import { BrandMark } from "../components/ui";
import { normalizeBase } from "../types";

const TOKEN_KEY = "agentgraph.admin_token";
const BASE_KEY = "agentgraph.api_base";

type Report = {
  changed?: string[];
  how_to_try?: string[];
  rolled_back?: boolean;
  unfinished?: string[];
};

type Run = {
  id: string;
  created_at: string;
  trigger_kind: "webhook" | "poll" | "manual";
  triggered_by: string;
  commit_sha: string | null;
  pr_number: number | null;
  status: "queued" | "running" | "skipped" | "blocked" | "deployed" | "rolled_back" | "failed";
  status_reason: string | null;
  rolled_back: boolean;
  report: Report;
  failed_checks: string[];
  finished_at: string | null;
};

type Check = { id: string; ok: boolean; summary: string };

type RunDetail = Run & { checks: Check[]; log_ref: string | null };

type GateState = {
  configured: boolean;
  webhook_configured: boolean;
  paused: boolean;
  paused_reason: string | null;
  auto_deploy_authorized: boolean;
  // The board owner's switch (3.17); absent on older servers.
  owner?: { linked: boolean; mode?: "manual" | "semi" | "full"; allows: boolean };
  auto_deploy_effective?: boolean;
  auto_deploy_blocked_by?: "operator" | "owner" | null;
  deployed_commit: string | null;
  deployed_at: string | null;
  runner: { status: "online" | "offline"; host: string | null; last_seen_at: string | null };
};

type Overview = { state: GateState; queued: number; runs: Run[] };

const STATUS_LABELS: Record<Run["status"], string> = {
  queued: "Waiting for the gate",
  running: "Being checked",
  skipped: "Skipped",
  blocked: "Stopped before going live",
  deployed: "Live on the server",
  rolled_back: "Rolled back",
  failed: "Did not finish",
};

const TRIGGER_LABELS: Record<Run["trigger_kind"], string> = {
  webhook: "Merged on GitHub",
  poll: "Found by the server",
  manual: "Started by you",
};

function when(value: string | null, locale: string): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(locale, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function connection(): { base: string; token: string } | null {
  const token = sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY);
  if (!token) return null;
  const base =
    localStorage.getItem(BASE_KEY) ||
    (window.location.port === "3000" ? "http://localhost:8000" : window.location.origin);
  return { base: normalizeBase(base), token };
}

export default function DeployGatePage() {
  const { t, lang } = useI18n();
  const locale = lang === "zh" ? "zh-CN" : "en-US";
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, RunDetail>>({});

  const call = useCallback(async <T,>(path: string, method = "GET", body?: unknown): Promise<T> => {
    const conn = connection();
    if (!conn) {
      window.location.assign("/dashboard/connect/");
      throw new Error("not connected");
    }
    const response = await fetch(`${conn.base}/v1/admin/deploy-gate${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${conn.token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(30000),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload?.error?.message ?? `HTTP ${response.status}`);
    return payload as T;
  }, []);

  const load = useCallback(async () => {
    try {
      setData(await call<Overview>("?limit=30"));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [call]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  const act = async (action: string, path: string, confirmText?: string, body?: unknown) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(action);
    try {
      await call(path, "POST", body ?? {});
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const toggleDetail = async (run: Run) => {
    if (details[run.id]) {
      setDetails(({ [run.id]: _, ...rest }) => rest);
      return;
    }
    try {
      const detail = await call<RunDetail>(`/runs/${run.id}`);
      setDetails((current) => ({ ...current, [run.id]: detail }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const state = data?.state;
  const health = !state
    ? null
    : state.runner.status === "offline"
      ? { tone: "below", text: t("Offline: the gate on the server has not checked in for a while. Nothing deploys until it is back.") }
      : state.paused
        ? { tone: "below", text: t("Paused: nothing deploys until you resume.") }
        : { tone: "good", text: t("Running: merged changes are checked as they arrive.") };

  return (
    <div className="growth-page">
      <header className="growth-header">
        <a className="auth-brand" href="/dashboard/">
          <span className="auth-brand-mark"><BrandMark size={34} /></span>
          <span>
            <strong>Veynod<span className="accent">.</span></strong>
            <small>{t("Deploy gate")}</small>
          </span>
        </a>
        <div className="growth-actions">
          <LanguageToggle />
          <a className="auth-operator-link" href="/dashboard/">{t("Back to console")} →</a>
        </div>
      </header>

      <main className="growth-main">
        <div className="growth-title">
          <h1>{t("What reaches veynod.com")}</h1>
          <p>{t("Merged changes go live only after the gate's checks pass, and a change that breaks the site is rolled back on its own. These switches are all you need.")}</p>
        </div>
        {error && <div className="inline-error" role="alert">{error}</div>}
        {!data && !error && <p className="growth-muted">{t("Loading…")}</p>}

        {state && health && (
          <section className="growth-panel deploy-status">
            <div className={`deploy-health state-${health.tone}`}>{health.text}</div>
            {!state.configured && (
              <p className="growth-muted">{t("The gate is not set up on this server yet. See docs/DEPLOY_GATE.md.")}</p>
            )}
            <dl className="deploy-facts">
              <div>
                <dt>{t("Automatic deploys")}</dt>
                <dd>
                  {!state.auto_deploy_authorized
                    ? t("Not allowed: only “Deploy now” goes live")
                    : state.auto_deploy_blocked_by === "owner"
                      ? t("Allowed here, but the owner has not allowed them")
                      : t("Allowed")}
                </dd>
              </div>
              {state.owner?.linked && (
                <div>
                  <dt>{t("Owner's choice")}</dt>
                  <dd>
                    {state.owner.allows
                      ? t("Automatic deploys allowed")
                      : state.owner.mode === "manual"
                        ? t("Ask me every step: nothing goes live on its own")
                        : t("Automatic deploys not allowed")}
                  </dd>
                </div>
              )}
              <div>
                <dt>{t("Last deploy")}</dt>
                <dd>{when(state.deployed_at, locale)}</dd>
              </div>
              <div>
                <dt>{t("Waiting")}</dt>
                <dd>{data.queued}</dd>
              </div>
            </dl>
            <div className="deploy-switches">
              <button
                type="button"
                className="button primary"
                disabled={busy !== null || state.paused}
                onClick={() => void act("trigger", "/trigger", t("Check and deploy the newest merged change now?"))}
              >
                {busy === "trigger" ? t("Starting…") : t("Deploy now")}
              </button>
              {state.paused ? (
                <button type="button" className="button ghost" disabled={busy !== null}
                        onClick={() => void act("resume", "/resume")}>
                  {t("Resume")}
                </button>
              ) : (
                <button type="button" className="button ghost" disabled={busy !== null}
                        onClick={() => void act("pause", "/pause", t("Pause the gate? Nothing will deploy until you resume."))}>
                  {t("Pause")}
                </button>
              )}
              {state.auto_deploy_authorized ? (
                <button type="button" className="button ghost" disabled={busy !== null}
                        onClick={() => void act("revoke", "/revoke", t("Stop automatic deploys? You can still use “Deploy now”."))}>
                  {t("Stop automatic deploys")}
                </button>
              ) : (
                <button type="button" className="button ghost" disabled={busy !== null}
                        onClick={() => void act("authorize", "/authorize", t("Let changes that pass every check go live on their own?"))}>
                  {t("Allow automatic deploys")}
                </button>
              )}
            </div>
          </section>
        )}

        {data && (
          <section className="growth-panel">
            <h2>{t("Recent deploys")}</h2>
            {data.runs.length === 0 && <p className="growth-muted">{t("Nothing has gone through the gate yet.")}</p>}
            <ol className="deploy-runs">
              {data.runs.map((run) => {
                const report = run.report || {};
                const detail = details[run.id];
                return (
                  <li key={run.id} className={`deploy-run status-${run.status}`}>
                    <div className="deploy-run-top">
                      <strong>{t(STATUS_LABELS[run.status])}</strong>
                      <span className="growth-muted">
                        {t(TRIGGER_LABELS[run.trigger_kind])} · {when(run.created_at, locale)}
                      </span>
                    </div>
                    {!!report.changed?.length && (
                      <div className="deploy-report">
                        <h3>{t("What changed")}</h3>
                        <ul>{report.changed.map((line) => <li key={line}>{line}</li>)}</ul>
                      </div>
                    )}
                    {!!report.how_to_try?.length && (
                      <div className="deploy-report">
                        <h3>{t("How to try it")}</h3>
                        <ul>{report.how_to_try.map((line) => <li key={line}>{line}</li>)}</ul>
                      </div>
                    )}
                    {!!report.unfinished?.length && (
                      <div className="deploy-report">
                        <h3>{t("Not done yet")}</h3>
                        <ul>{report.unfinished.map((line) => <li key={line}>{line}</li>)}</ul>
                      </div>
                    )}
                    <button type="button" className="button ghost small" onClick={() => void toggleDetail(run)}>
                      {detail ? t("Hide technical details") : t("Technical details")}
                    </button>
                    {detail && (
                      <div className="deploy-detail">
                        <p className="mono">
                          {detail.commit_sha ? detail.commit_sha.slice(0, 12) : "—"}
                          {detail.pr_number ? ` · #${detail.pr_number}` : ""}
                          {detail.status_reason ? ` · ${detail.status_reason}` : ""}
                        </p>
                        <ul>
                          {detail.checks.map((check) => (
                            <li key={check.id} className={check.ok ? "ok" : "fail"}>
                              <span className="mono">{check.id}</span> {check.ok ? "✓" : "✗"} {check.summary}
                            </li>
                          ))}
                        </ul>
                        {detail.log_ref && <p className="growth-muted mono">{detail.log_ref}</p>}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          </section>
        )}
      </main>
    </div>
  );
}
