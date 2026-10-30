"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";

/*
 * The computers that run this workspace's agents. A computer is a
 * workstation, not an agent: `veynod up` gives it a key for its own seat and,
 * to review on the same machine, a "-review" seat; the server groups the keys
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
}: {
  request: ApiRequest;
  notify: (message: string) => void;
  refreshKey: number;
  canManage: boolean;
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
  return (
    <section className="lin-list hosts-list-view" aria-label={t("Computers")}>
      <div className="lin-group">
        <i className={`lamp ${online ? "green" : ""}`} />
        {t("Computers")}
        <span className="lin-count">{online} / {computers.length} {t("online")}</span>
      </div>
      {computers.length === 0 ? (
        <div className="lin-row quiet hosts-empty-row">
          <span className="lin-title">
            {t("No computer works for this workspace yet. On the computer that should do the work, run:")}
          </span>
          <code>{INSTALL}</code>
        </div>
      ) : (
        computers.map((computer) => {
          const left = quotaLeft(computer);
          const runtimes = computer.runtimes.filter((runtime) => runtime.available)
            .map((runtime) => runtimeName(runtime.id)).join(", ");
          const paused = computer.seats.every((seat) => seat.status !== "active");
          const stateText = computer.last_seen_at && computer.state !== "online"
            ? `${t(STATE_LABEL[computer.state])} · ${relativeTime(computer.last_seen_at)}`
            : t(STATE_LABEL[computer.state]);
          return (
            <div key={computer.computer_id} className={`lin-row host-row state-${computer.state}`}>
              <i className={`lamp ${lamp(computer.state)}`} />
              <span className="lin-name" title={computer.name}>{computer.name}</span>
              <span className="lin-title">
                {computer.running.length ? `${t("Working on")} ${computer.running.join(", ")}`
                  : computer.state === "online" ? t("Idle") : stateText}
              </span>
              {canManage && (
                <span className="lin-actions">
                  {!paused ? (
                    <button type="button" className="button ghost small" disabled={busy !== null}
                      onClick={() => void act(computer, "pause")}>{t("Pause")}</button>
                  ) : (
                    <button type="button" className="button ghost small" disabled={busy !== null}
                      onClick={() => void act(computer, "resume")}>{t("Resume")}</button>
                  )}
                  <button type="button" className="button ghost small danger" disabled={busy !== null}
                    onClick={() => void act(computer, "revoke")}>{t("Take off the Loop")}</button>
                </span>
              )}
              {/* Second line: the seats, then what the machine is. */}
              <span className="host-sub">
                <span className="host-seats" aria-label={t("Seats")}>
                  {computer.seats.map((seat) => (
                    <span key={seat.host_id} className={`host-seat state-${seat.state}`}
                      title={`${seat.agent_id ?? seat.host_id} · ${t(STATE_LABEL[seat.state])}`}>
                      <i className={`lamp ${seat.running.length ? "amber" : lamp(seat.state)}`} />
                      {t(SEAT_LABEL[seat.seat])}
                    </span>
                  ))}
                </span>
                <span className="host-facts">
                  {OS_LABEL[computer.os] ?? computer.os} · {computer.arch}{runtimes ? ` · ${runtimes}` : ""}
                  {" · "}{t("Free slots")} {computer.free_slots}/{computer.max_concurrency}
                  {" · "}{t("Quota left")} {left === null ? "—" : `${left}%`}
                </span>
              </span>
            </div>
          );
        })
      )}
    </section>
  );
}
