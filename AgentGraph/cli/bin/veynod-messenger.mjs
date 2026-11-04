#!/usr/bin/env node
// veynod-messenger: the one process that connects a computer to the Loop.
//
// It holds the computer's nerve socket and is the only way work reaches it.
// Three jobs, kept strictly apart:
//   * network I/O: read frames, answer the first receipt at once, ping,
//     reconnect with backoff;
//   * the host record: hello on start, a heartbeat every 30 s with what runs;
//   * process supervision: one worker process per placement (up to `slots` at
//     a time), each in its own process group, killed as a tree when it overruns
//     orientation or its lease is revoked.
//
// It never thinks. Nothing here calls a model, so a slow worker cannot block
// the socket that says this computer is alive.
//
// Usage: node veynod-messenger.mjs --host URL --key KEY [--agent ID] [--slots N]

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PING_EVERY_MS = 5_000;
const HOST_HEARTBEAT_MS = 30_000;
const MAX_BACKOFF_MS = 3_000;
const ORIENTATION_TIMEOUT_MS = 4_500;

/** The socket URL for a host, http(s) becomes ws(s). */
export function nerveUrl(host) {
  const base = String(host || "").replace(/\/+$/, "");
  return base.replace(/^http/i, "ws") + "/v1/ws/nerve";
}

/**
 * The worker process for one poke. It talks to the same board as the
 * messenger (VEYNOD_HOST) and works as this computer (VEYNOD_RUNNER_OPTIONS:
 * repos, runtime, exec). The full frame payload rides along in
 * VEYNOD_TASK_POKE, so the worker knows the step and the diagram chunk.
 */
export function workerCommand({ runner, payload, token, agentId, host, runnerOptions = {} }) {
  const args = [runner, "--placement", String(payload.placement_id)];
  if (payload.task_key) args.push("--task", String(payload.task_key));
  if (payload.step) args.push("--step", String(payload.step));
  if (payload.lease_epoch !== undefined) args.push("--epoch", String(payload.lease_epoch));
  return {
    command: process.execPath,
    args,
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
  /**
   * `hostRecord`: an object with `hello()` and `beat()` (createRunner from
   * runner.mjs), or null when something else keeps the host record.
   */
  constructor({ host, key, agentId, runner, runnerOptions = {}, slots = 1, hostRecord = null,
    say = () => {}, spawner = spawn }) {
    this.host = host;
    this.key = key;
    this.agentId = agentId || "";
    this.hostId = os.hostname();
    this.runner = runner || path.join(HERE, "runner.mjs");
    this.runnerOptions = runnerOptions;
    this.slots = Math.max(1, Number(slots) || 1);
    this.hostRecord = hostRecord;
    this.say = say;
    this.spawn = spawner;
    this.ws = null;
    // frameId -> { child, taskKey, epoch, step, watchdog }
    this.workers = new Map();
    // What the heartbeat reports as running: slot key -> frameId.
    this.running = new Map();
    this.timers = [];
    this.hostTimer = null;
    this.attempts = 0;
    this.stopped = false;
  }

  get full() {
    return this.workers.size >= this.slots;
  }

  async start() {
    this.stopped = false;
    if (this.hostRecord) {
      try {
        await this.hostRecord.hello();
      } catch (error) {
        this.say(`[host] hello failed: ${error.message}`);
      }
      this.hostTimer = setInterval(() => void this.hostRecord.beat(), HOST_HEARTBEAT_MS);
      this.hostTimer.unref?.();
    }
    this.#connect();
  }

  stop() {
    this.stopped = true;
    this.#clearTimers();
    if (this.hostTimer) clearInterval(this.hostTimer);
    for (const frameId of [...this.workers.keys()]) this.#killWorker(frameId, "shutdown");
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
      this.say(`[nerve] online as ${this.agentId || "(token identity)"} on ${this.hostId}, ${this.slots} slot(s)`);
      this.#startLoops();
      this.#ping();
    });
    ws.addEventListener("message", (event) => {
      let frame;
      try {
        frame = JSON.parse(String(event.data));
      } catch {
        return;
      }
      this.onFrame(frame);
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

  /** One frame from the hub. Public for tests. */
  onFrame(frame) {
    const { type, payload = {}, frame_id: frameId } = frame || {};
    if (type === "auth.ack") {
      if (!this.agentId) this.agentId = payload.agent_id || "";
      return;
    }
    if (type === "sys.pong") return;
    if (type === "lease.revoked") {
      const { task_key: taskKey, lease_epoch: epoch, reason } = payload;
      for (const [id, worker] of this.workers) {
        if (worker.taskKey === taskKey && (worker.epoch ?? 0) <= (epoch ?? 0)) {
          this.say(`[nerve] lease revoked (${reason}); killing ${taskKey}`);
          this.#killWorker(id, reason || "revoked");
        }
      }
      return;
    }
    if (type !== "task.poke") return;
    // First receipt: answer before doing any work at all. A wedged worker
    // cannot delay it, because workers are other processes.
    if (this.workers.has(frameId)) return;
    if (this.full) {
      this.#send("task.busy.reject", { reason: "WORKER_ALREADY_BUSY" }, frameId);
      return;
    }
    this.#send("task.ack.net", { received_at_ms: Date.now() }, frameId);
    this.#launchWorker(frameId, payload);
  }

  #launchWorker(frameId, payload) {
    const { task_key: taskKey = null, lease_epoch: epoch, step = "work",
      orientation_timeout_ms: orientationMs = ORIENTATION_TIMEOUT_MS } = payload;
    const { command, args, env } = workerCommand({
      runner: this.runner, payload: { ...payload, placement_id: payload.placement_id || frameId },
      token: this.key, agentId: this.agentId, host: this.host, runnerOptions: this.runnerOptions,
    });
    let child;
    try {
      child = this.spawn(command, args, {
        detached: process.platform !== "win32",
        stdio: ["ignore", "inherit", "inherit", "ipc"],
        env,
      });
    } catch (error) {
      this.#send("task.busy.reject", { reason: `SPAWN_FAILED: ${error.message}` }, frameId);
      return;
    }
    const worker = { child, taskKey, epoch, step, watchdog: null };
    this.workers.set(frameId, worker);
    this.running.set(taskKey ?? frameId, frameId);
    // Second receipt: the worker is up and about to work the placement.
    child.on("message", (message) => {
      if (message?.type !== "IPC_ORIENTATION_READY") return;
      this.#clearWatchdog(worker);
      this.#send("task.ack.exec", { task_key: taskKey, lease_epoch: epoch, worker_pid: child.pid }, frameId);
    });
    child.on("exit", (code, signal) => {
      this.say(`[nerve] ${step} ${taskKey ?? frameId} exited (code=${code}, signal=${signal})`);
      this.#forget(frameId);
    });
    worker.watchdog = setTimeout(() => {
      this.say(`[nerve] ${taskKey ?? frameId} still starting after ${orientationMs}ms; killing`);
      this.#killWorker(frameId, "orientation_timeout");
    }, orientationMs);
    this.#ping();
  }

  #killWorker(frameId, reason) {
    const worker = this.workers.get(frameId);
    if (!worker) return;
    this.#clearWatchdog(worker);
    const pid = worker.child?.pid;
    if (pid) {
      try {
        if (process.platform !== "win32") process.kill(-pid, "SIGKILL");
        else worker.child.kill("SIGKILL");
      } catch {}
    }
    this.say(`[nerve] worker killed (${reason})`);
    this.#forget(frameId);
  }

  #forget(frameId) {
    const worker = this.workers.get(frameId);
    if (!worker) return;
    this.#clearWatchdog(worker);
    this.workers.delete(frameId);
    this.running.delete(worker.taskKey ?? frameId);
    this.#ping();
    if (this.hostRecord) void this.hostRecord.beat();
  }

  #clearWatchdog(worker) {
    if (worker?.watchdog) clearTimeout(worker.watchdog);
    if (worker) worker.watchdog = null;
  }

  /** Free slots and what runs, so the hub never pokes a full computer. */
  #ping() {
    const active = [...this.workers.values()];
    this.#send("sys.ping", {
      worker_busy: this.full,
      slots: this.slots,
      active: active.map((worker) => worker.taskKey).filter(Boolean),
      active_count: active.length,
      // One-slot hubs still read these two.
      active_task_key: active[0]?.taskKey || null,
      lease_epoch: active[0]?.epoch ?? null,
    });
  }

  #startLoops() {
    this.#clearTimers();
    this.timers.push(setInterval(() => {
      this.#ping();
      for (const worker of this.workers.values()) {
        if (worker.step === "work" && worker.taskKey) {
          this.#send("lease.heartbeat", { task_key: worker.taskKey, lease_epoch: worker.epoch });
        }
      }
    }, PING_EVERY_MS));
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

/**
 * Put this computer on the Loop: the host record (hello, heartbeats) and the
 * messenger that receives every placement. Resolves once it is connecting;
 * call `stop()` on the result to leave.
 */
export async function runComputer(options, { say = () => {}, version, createRunner, spawner } = {}) {
  const make = createRunner ?? (await import("./runner.mjs")).createRunner;
  const runnerOptions = { repos: options.repos || [], runtime: options.runtime || "", exec: options.exec || "",
    ...(options.deploy ? { deploy: options.deploy } : {}) };
  const messenger = new Messenger({
    host: options.host, key: options.key, agentId: options.agent, slots: options.slots,
    runnerOptions, say, ...(spawner ? { spawner } : {}),
  });
  messenger.hostRecord = await make({ ...options, running: messenger.running }, { version, say });
  await messenger.start();
  return messenger;
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
    console.error("usage: veynod-messenger --key KEY [--host URL] [--agent ID] [--slots N]");
    process.exit(2);
  }
  const messenger = new Messenger({
    host, key, agentId: args.agent || process.env.VEYNOD_AGENT_ID,
    runner: args.runner, slots: args.slots, say: (line) => console.error(line),
  });
  void messenger.start();
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      messenger.stop();
      process.exit(0);
    });
  }
}

export { ORIENTATION_TIMEOUT_MS };
