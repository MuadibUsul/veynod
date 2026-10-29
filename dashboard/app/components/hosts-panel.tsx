"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";

/*
 * The computers that run this workspace's agents (runner hosts): which are
 * online, what each is doing, how much quota is left. The owner can pause a
 * host (its tasks move to others) or take it off the Loop for good. It
 * refreshes when the Loop's event stream says something about hosts or
 * placements; it does not poll.
 */

type Runtime = {
  id: string;
  available: boolean;
  version?: string;
  usage?: { plan?: string; windows: { name: string; used_percent: number }[] };
};

type Host = {
  host_id: string;
  agent_id: string | null;
  os: string;
  arch: string;
  runtimes: Runtime[];
  max_concurrency: number;
  running: string[];
  free_slots: number;
  status: "active" | "paused" | "revoked";
  state: "online" | "offline" | "paused" | "revoked";
  last_seen_at: string | null;
};

const OS_LABEL: Record<string, string> = {
  linux: "Linux",
  darwin: "macOS",
  windows: "Windows",
  freebsd: "FreeBSD",
};

const STATE_LABEL: Record<Host["state"], string> = {
  online: "Online",
  offline: "Offline",
  paused: "Paused",
  revoked: "Removed",
};

const INSTALL = "npm i -g veynod && veynod service install --runner --key <runner token>";

/** Quota left on the host's best ready runtime, in percent, or null if unknown. */
function quotaLeft(host: Host): number | null {
  let best: number | null = null;
  for (const runtime of host.runtimes) {
    if (!runtime.available) continue;
    const windows = runtime.usage?.windows ?? [];
    const used = windows.length ? Math.max(...windows.map((window) => window.used_percent)) : 0;
    best = best === null ? used : Math.min(best, used);
  }
  return best === null ? null : Math.max(0, Math.round(100 - best));
}

export function HostsPanel({
  request,
  notify,
  refreshKey,
  canManage,
}: {
  request: ApiRequest;
  notify: (message: string) => void;
  refreshKey: number;
  canManage: boolean;
}) {
  const { t } = useI18n();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setHosts((await request<{ items: Host[] }>("/v1/runner/hosts")).items);
    } catch {
      setHosts([]);
    }
  }, [request]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function act(host: Host, action: "pause" | "resume" | "revoke") {
    if (action === "revoke"
        && !window.confirm(t("Take this computer off the Loop? Its key stops working and its tasks go to other computers."))) {
      return;
    }
    if (action === "pause"
        && !window.confirm(t("Pause this computer? It gets no new work and its tasks move to other computers."))) {
      return;
    }
    setBusy(host.host_id);
    try {
      await request(`/v1/runner/hosts/${encodeURIComponent(host.host_id)}/${action}`, {
        method: "POST",
        body: action === "revoke" ? JSON.stringify({ reason: "removed from the console" }) : undefined,
      });
      notify(t(action === "pause" ? "Computer paused." : action === "resume"
        ? "Computer working again." : "Computer removed from the Loop."));
      await load();
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  if (hosts === null) {
    return <section className="lin-list" aria-busy="true"><div className="skeleton block" /></section>;
  }

  const online = hosts.filter((host) => host.state === "online").length;
  return (
    <section className="lin-list hosts-list-view" aria-label={t("Computers")}>
      <div className="lin-group">
        <i className={`lamp ${online ? "green" : ""}`} />
        {t("Computers")}
        <span className="lin-count">{online} / {hosts.length} {t("online")}</span>
      </div>
      {hosts.length === 0 ? (
        <div className="lin-row quiet hosts-empty-row">
          <span className="lin-title">
            {t("No computer runs agents for this workspace yet. Issue a runner key under More, Agents, then on the computer that should do the work run:")}
          </span>
          <code>{INSTALL}</code>
        </div>
      ) : (
        hosts.map((host) => {
          const left = quotaLeft(host);
          const runtimes = host.runtimes.filter((runtime) => runtime.available)
            .map((runtime) => runtime.id === "claude" ? "Claude Code" : runtime.id === "codex" ? "Codex" : runtime.id)
            .join(", ");
          return (
            <div key={host.host_id} className={`lin-row host-row state-${host.state}`}>
              <i className={`lamp ${host.state === "online" ? "green" : host.state === "paused" ? "amber" : host.state === "revoked" ? "red" : ""}`} />
              <span className="lin-name">{host.agent_id ?? host.host_id}</span>
              <span className="lin-title">
                {host.running.length ? `${t("Working on")} ${host.running.join(", ")}` : t("Idle")}
              </span>
              <span className="lin-meta">{OS_LABEL[host.os] ?? host.os} · {host.arch}{runtimes ? ` · ${runtimes}` : ""}</span>
              <span className="lin-meta">{t("Free slots")} {host.free_slots}/{host.max_concurrency}</span>
              <span className="lin-meta">{t("Quota left")} {left === null ? "—" : `${left}%`}</span>
              <span className="lin-meta">
                {host.state === "online" ? t(STATE_LABEL[host.state])
                  : host.last_seen_at ? `${t(STATE_LABEL[host.state])} · ${relativeTime(host.last_seen_at)}`
                    : t(STATE_LABEL[host.state])}
              </span>
              {canManage && (
                <span className="lin-actions">
                  {host.status === "active" ? (
                    <button type="button" className="button ghost small" disabled={busy !== null}
                      onClick={() => void act(host, "pause")}>{t("Pause")}</button>
                  ) : (
                    <button type="button" className="button ghost small" disabled={busy !== null}
                      onClick={() => void act(host, "resume")}>{t("Resume")}</button>
                  )}
                  <button type="button" className="button ghost small danger" disabled={busy !== null}
                    onClick={() => void act(host, "revoke")}>{t("Take off the Loop")}</button>
                </span>
              )}
            </div>
          );
        })
      )}
    </section>
  );
}
