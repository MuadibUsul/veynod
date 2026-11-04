"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { AgentFace } from "./agent-face";

/*
 * The computers that run this workspace's agents. A computer is a
 * workstation, not an agent: `veynod up` gives it one key (older setups may
 * still show a "-review" seat; the Kernel gate reviews code); the server groups the keys
 * by machine. The owner can pause a computer (its tasks move to others) or
 * take it off the Loop for good; both act on every seat. It refreshes when the
 * Loop's event stream says something about hosts or placements; it does not
 * poll.
 */

type Runtime = {
  id: string;
  available: boolean;
  version?: string;
  usage?: { plan?: string; windows: { name: string; used_percent: number }[] };
};

type State = "online" | "offline" | "paused" | "revoked";

type Seat = {
  host_id: string;
  agent_id: string | null;
  seat: "work" | "review";
  status: "active" | "paused" | "revoked";
  state: State;
  running: string[];
};

type KernelLease = {
  task_key?: string;
  kernel_task_id?: string;
  worktree?: string;
  lease?: string;
  gate?: string;
  write_set?: string[];
};

type KernelCard = {
  delivery?: string;
  worktree?: string;
  lease?: string;
  gate?: string | null;
  leases?: KernelLease[];
};

type Computer = {
  computer_id: string;
  name: string;
  os: string;
  arch: string;
  runtimes: Runtime[];
  max_concurrency: number;
  running: string[];
  free_slots: number;
  state: State;
  last_seen_at: string | null;
  seats: Seat[];
  kernel?: KernelCard;
  runner_version?: string | null;
  /** Its CLI predates nerve-only dispatch: it looks online but gets no work. */
  outdated?: boolean;
};

const OS_LABEL: Record<string, string> = {
  linux: "Linux",
  darwin: "macOS",
  windows: "Windows",
  freebsd: "FreeBSD",
};

const STATE_LABEL: Record<State, string> = {
  online: "Online",
  offline: "Offline",
  paused: "Paused",
  revoked: "Removed",
};

const SEAT_LABEL: Record<Seat["seat"], string> = {
  work: "Works",
  review: "Reviews",
};

const INSTALL = "npx veynod up --run-tasks";
const RUNTIME_INSTALL = {
  claude: "npm install -g @anthropic-ai/claude-code",
  codex: "npm install -g @openai/codex && codex login",
};
const RECONNECT = "veynod up --run-tasks";
const UPGRADE = "npm install -g veynod@latest && veynod up --run-tasks";

export function runtimeName(id: string): string {
  return id === "claude" ? "Claude Code" : id === "codex" ? "Codex" : id;
}

/** Quota left on the computer's best ready runtime, in percent, or null if unknown. */
function quotaLeft(computer: Computer): number | null {
  let best: number | null = null;
  for (const runtime of computer.runtimes) {
    if (!runtime.available) continue;
    const windows = runtime.usage?.windows ?? [];
    const used = windows.length ? Math.max(...windows.map((window) => window.used_percent)) : 0;
    best = best === null ? used : Math.min(best, used);
  }
  return best === null ? null : Math.max(0, Math.round(100 - best));
}

function lamp(state: State): string {
  return state === "online" ? "green" : state === "paused" ? "amber" : state === "revoked" ? "red" : "";
}

export function HostsPanel({
  request,
  notify,
  refreshKey,
  canManage,
  waitingForQuota = 0,
}: {
  request: ApiRequest;
  notify: (message: string) => void;
  refreshKey: number;
  canManage: boolean;
  /** Tasks the Loop could not place because every fitting computer is out of quota. */
  waitingForQuota?: number;
}) {
  const { t } = useI18n();
  const [computers, setComputers] = useState<Computer[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setComputers((await request<{ computers: Computer[] }>("/v1/runner/hosts")).computers ?? []);
    } catch {
      setComputers([]);
    }
  }, [request]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function act(computer: Computer, action: "pause" | "resume" | "revoke") {
    if (action === "revoke"
        && !window.confirm(t("Take this computer off the Loop? Its keys stop working and its tasks go to other computers."))) {
      return;
    }
    if (action === "pause"
        && !window.confirm(t("Pause this computer? It gets no new work and its tasks move to other computers."))) {
      return;
    }
    setBusy(computer.computer_id);
    try {
      // Every seat of the computer: pausing only its reviewer would be a different thing.
      const seats = computer.seats.filter((seat) => seat.status !== "revoked"
        && (action === "revoke" || (action === "pause") === (seat.status === "active")));
      for (const seat of seats) {
        await request(`/v1/runner/hosts/${encodeURIComponent(seat.host_id)}/${action}`, {
          method: "POST",
          body: action === "revoke" ? JSON.stringify({ reason: "removed from the console" }) : undefined,
        });
      }
      notify(t(action === "pause" ? "Computer paused." : action === "resume"
        ? "Computer working again." : "Computer removed from the Loop."));
      await load();
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  if (computers === null) {
    return <section className="lin-list" aria-busy="true"><div className="skeleton block" /></section>;
  }

  const online = computers.filter((computer) => computer.state === "online").length;
  const free = computers.reduce((sum, computer) => sum + computer.free_slots, 0);
  return (
    <section className="lin-list hosts-list-view" id="loop-computers" aria-label={t("Computers")}>
      <div className="lin-group">
        <i className={`lamp ${online ? "green" : ""}`} />
        {t("Computers")}
        <span className="lin-count">{online} / {computers.length} {t("online")}</span>
        {computers.length > 0 && <span className="flow-aside">{t("{n} free seats", { n: free })}</span>}
      </div>
      {computers.map((computer) => {
        const left = quotaLeft(computer);
        const runtimes = computer.runtimes.filter((runtime) => runtime.available).map((runtime) => runtimeName(runtime.id));
        const paused = computer.seats.every((seat) => seat.status !== "active");
        // What keeps an online computer from working: no runtime it can start, or a seat taken off.
        const noRuntime = computer.state !== "revoked" && computer.runtimes.length > 0 && runtimes.length === 0;
        // Only the work seat matters: review is the Kernel gate's or a person's.
        const removedSeats = computer.state === "revoked" ? []
          : computer.seats.filter((seat) => seat.status === "revoked" && seat.seat === "work");
        const stateText = computer.last_seen_at && computer.state !== "online"
          ? `${t(STATE_LABEL[computer.state])} · ${relativeTime(computer.last_seen_at)}`
          : t(STATE_LABEL[computer.state]);
        return (
          <div key={computer.computer_id} className={`station state-${computer.state}`}>
            <div className="station-top">
              <i className={`lamp ${lamp(computer.state)}`} />
              <b title={computer.name}>{computer.name}</b>
              <span className="station-facts">
                {OS_LABEL[computer.os] ?? computer.os} · {computer.arch}{runtimes.length ? ` · ${runtimes.join(", ")}` : ""}
                {computer.state === "online" ? "" : ` · ${stateText}`}
              </span>
            </div>
            <div className="station-seats">
              {computer.seats.map((seat) => {
                const busy = seat.running.length > 0;
                const identity = seat.agent_id || seat.host_id;
                const runtime = runtimes[0];
                return (
                  <div key={seat.host_id} className={`station-seat${busy ? " busy" : ""}`}
                    title={`${identity} · ${t(STATE_LABEL[seat.state])}`}>
                    {busy && (
                      <AgentFace id={identity} role={seat.seat === "review" ? "AUDITOR" : "EXECUTOR"} size={26} />
                    )}
                    <span>
                      <b>{identity}</b>
                      <small>
                        {runtime ? runtimeName(runtime) : t(SEAT_LABEL[seat.seat])}
                        {busy ? ` · ${seat.running.join(", ")}` : seat.state === "online" ? "" : ` · ${t(STATE_LABEL[seat.state])}`}
                      </small>
                    </span>
                  </div>
                );
              })}
            </div>
            <p className="station-kernel">
              {t("Worktree")} {computer.kernel?.worktree || "idle"}
              {" · "}
              {t("Lease")} {computer.kernel?.lease || "none"}
              {computer.kernel?.gate ? ` · ${t("Gate")} ${computer.kernel.gate}` : ""}
              {(computer.kernel?.leases?.[0]?.write_set || []).length
                ? ` · ${computer.kernel!.leases![0].write_set!.join(", ")}`
                : ""}
            </p>
            {left !== null && (
              <div className="station-quota">
                <span>{t("Quota left")}</span>
                <span className={`station-bar${left < 10 ? " low" : ""}`}><i style={{ width: `${Math.max(2, left)}%` }} /></span>
                <span>{left}%</span>
              </div>
            )}
            {computer.outdated && computer.state !== "revoked" && (
              <p className="station-note">
                {t("This computer runs veynod {version}, which cannot receive work any more (work now arrives only over the nerve socket). On it, run:",
                  { version: computer.runner_version ?? "?" })}{" "}
                <code>{UPGRADE}</code>
              </p>
            )}
            {noRuntime && (
              <p className="station-note">
                {t("Neither Claude Code nor Codex runs on this computer, so it cannot take tasks (the desktop apps do not count). On it, install one and sign in:")}{" "}
                <code>{RUNTIME_INSTALL.claude}</code> {t("or")} <code>{RUNTIME_INSTALL.codex}</code>
              </p>
            )}
            {removedSeats.map((seat) => (
              <p key={seat.host_id} className="station-note">
                {t("Its work seat was taken off the Loop, so it cannot build tasks. To connect it again, run on it:")}{" "}
                <code>{RECONNECT}</code>
              </p>
            ))}
            {left === 0 && waitingForQuota > 0 && (
              <p className="station-note">
                {t("Out of quota: {n} tasks wait and start on their own when it resets.", { n: waitingForQuota })}
              </p>
            )}
            {canManage && (
              <div className="station-actions">
                {!paused ? (
                  <button type="button" className="button ghost small" disabled={busy !== null}
                    onClick={() => void act(computer, "pause")}>{t("Pause")}</button>
                ) : (
                  <button type="button" className="button ghost small" disabled={busy !== null}
                    onClick={() => void act(computer, "resume")}>{t("Resume")}</button>
                )}
                <button type="button" className="button ghost small danger" disabled={busy !== null}
                  onClick={() => void act(computer, "revoke")}>{t("Take off the Loop")}</button>
              </div>
            )}
          </div>
        );
      })}
      <div className={`lin-row quiet station-connect${computers.length ? "" : " empty"}`}>
        <span className="lin-title">
          {computers.length ? t("Connect a computer:")
            : t("No computer works for this workspace yet. On the computer that should do the work, run:")}
        </span>
        <code>{INSTALL}</code>
      </div>
    </section>
  );
}
