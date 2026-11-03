#!/usr/bin/env node
// veynod-messenger: the常驻信使 that holds an agent's nerve socket.
//
// Two jobs, kept strictly apart:
//   * network I/O — read frames, answer the first receipt within milliseconds,
//     heartbeat, renew the sub-token in band, reconnect with backoff;
//   * process supervision — start the worker in its own process group, kill the
//     whole tree when orientation overruns or the lease is revoked.
//
// It never thinks. Nothing here calls a model or assembles context, so a slow
// worker cannot block the socket that says this agent is alive.
//
// Usage: node veynod-messenger.mjs --host URL --key KEY [--agent ID] [--runner PATH]

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PING_EVERY_MS = 5_000;
const REFRESH_EVERY_MS = 15 * 60 * 1000;
const MAX_BACKOFF_MS = 3_000;
const NET_ACK_TIMEOUT_MS = 200;
const ORIENTATION_TIMEOUT_MS = 4_500;

/** The socket URL for a host, http(s) becomes ws(s). */
export function nerveUrl(host) {
  const base = String(host || "").replace(/\/+$/, "");
  return base.replace(/^http/i, "ws") + "/v1/ws/nerve";
}

/**
 * The worker command for a poke. `runner` is this repo's runner; it is started
 * in its own group so a kill takes its git/pytest children with it.
 */
/**
 * The worker process for one poke. It talks to the same board as the
 * messenger (VEYNOD_HOST) and says hello with the same repos, runtime and
 * delivery mode (VEYNOD_RUNNER_OPTIONS): a worker that fell back to
 * localhost, or said hello with no repos, failed the second receipt or
 * erased the computer's repos on the board, and the task bounced back.
 */
export function workerCommand({ runner, payload, token, agentId, host, runnerOptions = {} }) {
  return {
    command: process.execPath,
    args: [runner, "--task", payload.task_key, "--epoch", String(payload.lease_epoch),
      "--placement", payload.placement_id],
    env: {
      ...process.env,
      ...(host ? { VEYNOD_HOST: host } : {}),
      VEYNOD_AGENT_ID: agentId,
      VEYNOD_AGENT_TOKEN: token,
      VEYNOD_TASK_POKE: JSON.stringify(payload),
      VEYNOD_RUNNER_OPTIONS: JSON.stringify(runnerOptions),
    },
  };
}

export class Messenger {
  constructor({ host, key, agentId, runner, runnerOptions = {}, say = () => {} }) {
    this.host = host;
    this.runnerOptions = runnerOptions;
    this.key = key;
    this.agentId = agentId || "";
    this.hostId = os.hostname();
    this.runner = runner || path.join(HERE, "runner.mjs");
    this.say = say;
    this.ws = null;
    this.worker = null;
    this.active = null; // { taskKey, epoch, frameId }
    this.watchdog = null;
    this.timers = [];
    this.attempts = 0;
    this.stopped = false;
  }

  start() {
    this.stopped = false;
    this.#connect();
  }

  stop() {
    this.stopped = true;
    this.#clearTimers();
    this.#killWorker("shutdown");
    try {
      this.ws?.close(1000, "client_shutdown");
    } catch {}
  }

  #connect() {
    const ws = new WebSocket(nerveUrl(this.host), {
      headers: {
        Authorization: `Bearer ${this.key}`,
        "X-Agent-Id": this.agentId,
        "X-Host-Id": this.hostId,
      },
    });
    this.ws = ws;
    ws.addEventListener("open", () => {
      this.attempts = 0;
      this.say(`[nerve] online as ${this.agentId || "(token identity)"} on ${this.hostId}`);
      this.#startLoops();
    });
    ws.addEventListener("message", (event) => {
      let frame;
      try {
        frame = JSON.parse(String(event.data));
      } catch {
        return;
      }
      this.#onFrame(frame);
    });
    ws.addEventListener("close", (event) => {
      this.#clearTimers();
      if (this.stopped) return;
      this.say(`[nerve] socket closed (${event.code}); reconnecting`);
      const delay = Math.min(MAX_BACKOFF_MS, 100 * 2 ** this.attempts++) + Math.random() * 150;
      setTimeout(() => this.#connect(), delay);
    });
    ws.addEventListener("error", () => {
      try { ws.close(); } catch {}
    });
  }

  #onFrame(frame) {
    const { type, payload = {}, frame_id: frameId } = frame || {};
    if (type === "auth.ack") {
      if (!this.agentId) this.agentId = payload.agent_id || "";
      return;
    }
    if (type === "auth.token_refresh.res") {
      if (payload.token) {
        this.key = payload.token;
        this.say("[nerve] sub-token renewed in band");
      } else if (payload.error) {
        this.say(`[nerve] token renewal failed: ${payload.error}`);
      }
      return;
    }
    if (type === "sys.pong") return;
    if (type === "lease.revoked") {
      const { task_key: taskKey, lease_epoch: epoch, reason } = payload;
      if (this.active && this.active.taskKey === taskKey && this.active.epoch <= epoch) {
        this.say(`[nerve] lease revoked (${reason}); killing ${taskKey}`);
        this.#killWorker(reason || "revoked");
      }
      return;
    }
    if (type !== "task.poke") return;
    // First receipt: answer before doing any work at all. This is the whole
    // point of the split — a wedged worker still answers here.
    if (this.worker) {
      this.#send("task.busy.reject", { reason: "WORKER_ALREADY_BUSY" }, frameId);
      return;
    }
    this.#send("task.ack.net", { received_at_ms: Date.now() }, frameId);
    this.#launchWorker(frameId, payload);
  }

  #launchWorker(frameId, payload) {
    const { task_key: taskKey, lease_epoch: epoch,
      orientation_timeout_ms: orientationMs = ORIENTATION_TIMEOUT_MS } = payload;
    this.active = { taskKey, epoch, frameId };
    const { command, args, env } = workerCommand({
      runner: this.runner, payload, token: this.key, agentId: this.agentId,
      host: this.host, runnerOptions: this.runnerOptions,
    });
    let child;
    try {
      child = spawn(command, args, {
        detached: process.platform !== "win32",
        stdio: ["ignore", "inherit", "inherit", "ipc"],
        env,
      });
    } catch (error) {
      this.#send("task.busy.reject", { reason: `SPAWN_FAILED: ${error.message}` }, frameId);
      this.#cleanupWorker();
      return;
    }
    this.worker = child;
    // Second receipt: the worker is past orientation and holds the lease.
    child.on("message", (message) => {
      if (message?.type !== "IPC_ORIENTATION_READY") return;
      this.#clearWatchdog();
      this.#send("task.ack.exec",
        { task_key: taskKey, lease_epoch: epoch, worker_pid: child.pid }, frameId);
    });
    child.on("exit", (code, signal) => {
      this.say(`[nerve] worker for ${taskKey} exited (code=${code}, signal=${signal})`);
      this.#cleanupWorker();
    });
    this.watchdog = setTimeout(() => {
      this.say(`[nerve] ${taskKey} still in orientation after ${orientationMs}ms; killing`);
      this.#killWorker("orientation_timeout");
    }, orientationMs);
  }

  #killWorker(reason) {
    this.#clearWatchdog();
    const child = this.worker;
    if (child?.pid) {
      try {
        if (process.platform !== "win32") process.kill(-child.pid, "SIGKILL");
        else child.kill("SIGKILL");
      } catch {}
    }
    this.say(`[nerve] worker killed (${reason})`);
    this.#cleanupWorker();
  }

  #cleanupWorker() {
    this.#clearWatchdog();
    this.worker = null;
    this.active = null;
  }

  #clearWatchdog() {
    if (this.watchdog) clearTimeout(this.watchdog);
    this.watchdog = null;
  }

  #startLoops() {
    this.#clearTimers();
    this.timers.push(setInterval(() => {
      this.#send("sys.ping", {
        worker_busy: this.worker !== null,
        active_task_key: this.active?.taskKey || null,
        lease_epoch: this.active?.epoch ?? null,
      });
      if (this.active) {
        this.#send("lease.heartbeat", {
          task_key: this.active.taskKey, lease_epoch: this.active.epoch,
        });
      }
    }, PING_EVERY_MS));
    this.timers.push(setInterval(() => {
      this.#send("auth.token_refresh.req", {});
    }, REFRESH_EVERY_MS));
  }

  #clearTimers() {
    for (const timer of this.timers) clearInterval(timer);
    this.timers = [];
  }

  #send(type, payload, frameId = crypto.randomUUID()) {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({ frame_id: frameId, type, ts_ms: Date.now(), payload }));
    return true;
  }
}

/** Parse `--flag value` pairs; unknown flags are ignored by the caller's argv check. */
export function parseArgs(argv) {
  const out = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) continue;
    out[arg.slice(2)] = argv[index + 1]?.startsWith("--") ? true : argv[++index];
  }
  return out;
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  const args = parseArgs(process.argv.slice(2));
  const host = args.host || process.env.VEYNOD_HOST || "http://127.0.0.1:8000";
  const key = args.key || process.env.VEYNOD_KEY;
  if (!key) {
    console.error("usage: veynod-messenger --key KEY [--host URL] [--agent ID] [--runner PATH]");
    process.exit(2);
  }
  const messenger = new Messenger({
    host, key, agentId: args.agent || process.env.VEYNOD_AGENT_ID,
    runner: args.runner, say: (line) => console.error(line),
  });
  messenger.start();
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      messenger.stop();
      process.exit(0);
    });
  }
}

export { NET_ACK_TIMEOUT_MS, ORIENTATION_TIMEOUT_MS };
