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
// A poke passes three points, and none stands in for the next: the computer
// read it (task.ack.net), a worker process is up (task.ack.exec), and the worker
// checked its placement with the board and began (the worker's own `start`
// call). A worker that cannot start, dies while starting or overruns the
// watchdog is refused to the hub at once, with the reason; one that ends
// without having reported is reported (task.worker.exit), so nothing is found
// out only when a timer runs out.
//
// Workers are prepared ahead: `warm` standby processes have Node and the
// runner's modules loaded and wait for a placement over IPC, so a poke does not
// pay a cold start. A supervise round runs in a lane of its own, beside the
// work slots, so a long task never stops the Loop from being looked after.
//
// It never thinks. Nothing here calls a model, so a slow worker cannot block
// the socket that says this computer is alive.
//
// Usage: node veynod-messenger.mjs --host URL --key KEY [--agent ID] [--slots N] [--warm N]

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PING_EVERY_MS = 5_000;
const HOST_HEARTBEAT_MS = 30_000;
const MAX_BACKOFF_MS = 3_000;
const ORIENTATION_TIMEOUT_MS = 4_500;
// Rounds that look after the Loop run beside the work slots, one at a time.
const SUPERVISE_SLOTS = 1;
// A standby worker is replaced after this long, so what it loaded is never old.
const STANDBY_MAX_AGE_MS = 10 * 60_000;
// Standby workers that die this many times in a row turn the pool off: pokes
// then start their worker cold, as before, instead of feeding a crash loop.
const STANDBY_FAILURES = 3;

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
  // Without a payload this is a standby worker: it loads, says it is ready and
  // waits for its placement over IPC.
  const args = payload ? [runner, "--placement", String(payload.placement_id)] : [runner, "--standby"];
  if (payload?.task_key) args.push("--task", String(payload.task_key));
  if (payload?.step) args.push("--step", String(payload.step));
  if (payload && payload.lease_epoch !== undefined) args.push("--epoch", String(payload.lease_epoch));
  return {
    command: process.execPath,
    args,
    env: {
      ...process.env,
      ...(host ? { VEYNOD_HOST: host } : {}),
      VEYNOD_AGENT_ID: agentId,
      VEYNOD_AGENT_TOKEN: token,
      ...(payload ? { VEYNOD_TASK_POKE: JSON.stringify(payload) } : {}),
      VEYNOD_RUNNER_OPTIONS: JSON.stringify(runnerOptions),
    },
  };
}

/** Which lane a step runs in: supervise rounds have their own, beside the work slots. */
export const laneOf = (step) => (step === "supervise" ? "supervise" : "work");

export class Messenger {
  /**
   * `hostRecord`: an object with `hello()` and `beat()` (createRunner from
   * runner.mjs), or null when something else keeps the host record.
   */
  constructor({ host, key, agentId, runner, runnerOptions = {}, slots = 1, hostRecord = null,
    say = () => {}, spawner = spawn, startLogin = null, warm = 0, exists = existsSync,
    onRunnerMissing = null }) {
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
    // frameId -> { child, taskKey, epoch, step, lane, watchdog, ready, reported, warm }
    this.workers = new Map();
    // What the heartbeat reports as running: slot key -> frameId. Work-lane
    // placements only: a supervise round does not take a slot.
    this.running = new Map();
    // Prepared workers waiting for a placement: { child, ready, since }.
    this.warm = Math.max(0, Number(warm) || 0);
    this.standby = [];
    this.standbyFailures = 0;
    this.standbyTimer = null;
    this.exists = exists;
    // The runner file went away under this hub (its version was removed): it
    // can start nothing any more, so whoever owns the process restarts it.
    this.onRunnerMissing = onRunnerMissing;
    // Finishing the running work before a restart: nothing new is taken.
    this.draining = false;
    this.drained = [];
    this.timers = [];
    this.hostTimer = null;
    this.attempts = 0;
    this.stopped = false;
    // login_id -> a sign-in running here for the console (login.mjs).
    this.logins = new Map();
    this.startLogin = startLogin;
  }

  /** Workers in a lane right now. */
  count(lane) {
    let total = 0;
    for (const worker of this.workers.values()) if (worker.lane === lane) total += 1;
    return total;
  }

  /** Every work slot is taken (a supervise round does not count). */
  get full() {
    return this.count("work") >= this.slots;
  }

  /**
   * Stop taking placements and resolve once the running ones have ended. For a
   * restart on a new version: the old hub lets its workers finish instead of
   * cutting a session short, and the hub is told (in the ping) not to send more.
   */
  drain() {
    this.draining = true;
    this.#dropStandby();
    this.#ping();
    if (this.workers.size === 0) return Promise.resolve();
    return new Promise((resolve) => this.drained.push(resolve));
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
    this.#fillStandby();
    if (this.warm) {
      this.standbyTimer = setInterval(() => this.#recycleStandby(), 60_000);
      this.standbyTimer.unref?.();
    }
    this.#connect();
  }

  stop() {
    this.stopped = true;
    this.#clearTimers();
    if (this.hostTimer) clearInterval(this.hostTimer);
    if (this.standbyTimer) clearInterval(this.standbyTimer);
    this.#dropStandby();
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
      const { task_key: taskKey, lease_epoch: epoch, reason, placement_id: placementId } = payload;
      for (const [id, worker] of this.workers) {
        // A step without a task (a supervise round) is named by its placement.
        const named = placementId ? id === placementId : false;
        const leased = Boolean(taskKey) && worker.taskKey === taskKey && (worker.epoch ?? 0) <= (epoch ?? 0);
        if (named || leased) {
          this.say(`[nerve] lease revoked (${reason}); killing ${worker.taskKey ?? id}`);
          this.#killWorker(id, reason || "revoked");
        }
      }
      return;
    }
    if (type === "login.start" || type === "login.code" || type === "login.cancel") {
      // A sign-in that goes wrong says so; it never takes the hub down.
      this.#login(type, payload).catch((error) => {
        this.say(`[login] ${error.message}`);
        const id = typeof payload.login_id === "string" ? payload.login_id.slice(0, 64) : "";
        if (id) {
          if (this.logins.get(id) === null) this.logins.delete(id);
          this.#send("login.update", { login_id: id, state: "failed",
            message: "The sign-in could not start on this computer." });
        }
      });
      return;
    }
    if (type !== "task.poke") return;
    // First receipt: answer before doing any work at all. A wedged worker
    // cannot delay it, because workers are other processes.
    if (this.workers.has(frameId)) return;
    const lane = laneOf(payload.step);
    if (this.draining) {
      this.#send("task.busy.reject", { reason: "HUB_DRAINING" }, frameId);
      return;
    }
    if (lane === "supervise" ? this.count("supervise") >= SUPERVISE_SLOTS : this.full) {
      this.#send("task.busy.reject", { reason: "WORKER_ALREADY_BUSY" }, frameId);
      return;
    }
    this.#send("task.ack.net", { received_at_ms: Date.now() }, frameId);
    this.#launchWorker(frameId, payload, lane);
  }

  /** Why the worker for a frame did not start, said to the hub at once. */
  #refuse(frameId, reason) {
    this.say(`[nerve] ${frameId} not started: ${reason}`);
    this.#send("task.busy.reject", { reason }, frameId);
  }

  #launchWorker(frameId, payload, lane = laneOf(payload.step)) {
    const { task_key: taskKey = null, lease_epoch: epoch, step = "work",
      orientation_timeout_ms: orientationMs = ORIENTATION_TIMEOUT_MS } = payload;
    const full = { ...payload, placement_id: payload.placement_id || frameId };
    if (!this.exists(this.runner)) {
      // This hub's version was removed under it. Refuse, then let the service
      // start the version that is installed now.
      this.#refuse(frameId, `RUNNER_MISSING: ${this.runner}`);
      this.onRunnerMissing?.(this.runner);
      return;
    }
    let child = null;
    let warm = false;
    const prepared = this.standby.findIndex((item) => item.ready && item.child.connected !== false);
    if (prepared >= 0) {
      const [taken] = this.standby.splice(prepared, 1);
      try {
        taken.child.send({ type: "IPC_PLACEMENT", payload: full });
        taken.child.removeAllListeners("exit");
        taken.child.removeAllListeners("error");
        taken.child.removeAllListeners("message");
        child = taken.child;
        warm = true;
      } catch {
        this.#kill(taken.child);
      }
    }
    if (!child) {
      const { command, args, env } = workerCommand({
        runner: this.runner, payload: full,
        token: this.key, agentId: this.agentId, host: this.host, runnerOptions: this.runnerOptions,
      });
      try {
        child = this.spawn(command, args, {
          detached: process.platform !== "win32",
          stdio: ["ignore", "inherit", "inherit", "ipc"],
          env,
        });
      } catch (error) {
        this.#refuse(frameId, `WORKER_START_FAILED: ${error.message}`);
        return;
      }
    }
    const worker = { child, taskKey, epoch, step, lane, watchdog: null, ready: false, reported: false, warm };
    this.workers.set(frameId, worker);
    if (lane === "work") this.running.set(taskKey ?? frameId, frameId);
    child.on("message", (message) => {
      if (message?.type === "IPC_REPORTED" || message?.type === "IPC_SKIPPED") {
        // The worker told the board how its step ended (or that it did nothing).
        worker.reported = true;
        return;
      }
      if (message?.type !== "IPC_ORIENTATION_READY" || worker.ready) return;
      // Second receipt: the worker is up. Whether it works the placement is
      // the worker's own question to the board (`start`), not this receipt.
      worker.ready = true;
      this.#clearWatchdog(worker);
      this.#send("task.ack.exec", { task_key: taskKey, lease_epoch: epoch, worker_pid: child.pid, warm }, frameId);
    });
    // A spawn that fails later (ENOENT, EACCES, EAGAIN) arrives as an event.
    // Unhandled, it would take the whole hub down with it.
    child.on("error", (error) => {
      if (!this.workers.has(frameId)) return;
      if (!worker.ready) this.#refuse(frameId, `WORKER_START_FAILED: ${error.message}`);
      else this.#workerExit(frameId, worker, { reason: `the worker process failed: ${error.message}` });
      this.#forget(frameId);
    });
    child.on("exit", (code, signal) => {
      this.say(`[nerve] ${step} ${taskKey ?? frameId} exited (code=${code}, signal=${signal})`);
      if (!this.workers.has(frameId)) return;
      if (!worker.ready) this.#refuse(frameId, `WORKER_EXITED_BEFORE_READY: code=${code}, signal=${signal}`);
      else this.#workerExit(frameId, worker, { code, signal });
      this.#forget(frameId);
    });
    worker.watchdog = setTimeout(() => {
      this.#refuse(frameId, `ORIENTATION_TIMEOUT: no worker after ${orientationMs}ms`);
      this.#killWorker(frameId, "orientation_timeout");
    }, orientationMs);
    this.#fillStandby();
    this.#ping();
  }

  /** The worker of a placement ended: the hub hears whether it had reported. */
  #workerExit(frameId, worker, { code = null, signal = null, reason = null } = {}) {
    this.#send("task.worker.exit", { placement_id: frameId, task_key: worker.taskKey, step: worker.step,
      code, signal, reported: worker.reported, ...(reason ? { reason } : {}) }, frameId);
  }

  #kill(child) {
    const pid = child?.pid;
    if (!pid) return;
    try {
      if (process.platform !== "win32") process.kill(-pid, "SIGKILL");
      else child.kill("SIGKILL");
    } catch {}
  }

  #killWorker(frameId, reason) {
    const worker = this.workers.get(frameId);
    if (!worker) return;
    this.#clearWatchdog(worker);
    // Forgotten first: the kill's own exit event has nothing left to report.
    this.#forget(frameId);
    this.#kill(worker.child);
    this.say(`[nerve] worker killed (${reason})`);
  }

  #forget(frameId) {
    const worker = this.workers.get(frameId);
    if (!worker) return;
    this.#clearWatchdog(worker);
    this.workers.delete(frameId);
    if (this.running.get(worker.taskKey ?? frameId) === frameId) this.running.delete(worker.taskKey ?? frameId);
    this.#ping();
    if (this.hostRecord) void this.hostRecord.beat();
    if (this.workers.size === 0) for (const done of this.drained.splice(0)) done();
  }

  #clearWatchdog(worker) {
    if (worker?.watchdog) clearTimeout(worker.watchdog);
    if (worker) worker.watchdog = null;
  }

  // ------------------------------------------------------------- standby

  /** Keep `warm` prepared workers waiting, so a poke starts no process. */
  #fillStandby() {
    if (!this.warm || this.stopped || this.draining) return;
    if (this.standbyFailures >= STANDBY_FAILURES || !this.exists(this.runner)) return;
    while (this.standby.length < this.warm) {
      const { command, args, env } = workerCommand({ runner: this.runner, payload: null,
        token: this.key, agentId: this.agentId, host: this.host, runnerOptions: this.runnerOptions });
      let child;
      try {
        child = this.spawn(command, args, { detached: process.platform !== "win32",
          stdio: ["ignore", "inherit", "inherit", "ipc"], env });
      } catch (error) {
        this.#standbyFailed(`could not start a standby worker: ${error.message}`);
        return;
      }
      const item = { child, ready: false, since: Date.now() };
      this.standby.push(item);
      const gone = (why) => {
        const index = this.standby.indexOf(item);
        if (index < 0) return;
        this.standby.splice(index, 1);
        if (this.stopped || this.draining || item.retired) return;
        if (!item.ready) this.#standbyFailed(`a standby worker ended before it was ready (${why})`);
        const again = setTimeout(() => this.#fillStandby(), 1_000);
        again.unref?.();
      };
      child.on("message", (message) => {
        if (message?.type !== "IPC_STANDBY_READY") return;
        item.ready = true;
        this.standbyFailures = 0;
      });
      child.on("error", (error) => gone(error.message));
      child.on("exit", (code, signal) => gone(`code=${code}, signal=${signal}`));
    }
  }

  #standbyFailed(text) {
    this.standbyFailures += 1;
    this.say(`[nerve] ${text}`);
    if (this.standbyFailures === STANDBY_FAILURES) {
      this.say("[nerve] standby workers keep failing; placements start their worker cold from now on");
    }
  }

  #recycleStandby() {
    for (const item of [...this.standby]) {
      if (Date.now() - item.since < STANDBY_MAX_AGE_MS) continue;
      item.retired = true;
      this.standby.splice(this.standby.indexOf(item), 1);
      this.#kill(item.child);
    }
    this.#fillStandby();
  }

  #dropStandby() {
    for (const item of this.standby.splice(0)) {
      item.retired = true;
      this.#kill(item.child);
    }
  }

  /** Free slots and what runs, so the hub never pokes a full computer. */
  #ping() {
    const active = [...this.workers.values()].filter((worker) => worker.lane === "work");
    this.#send("sys.ping", {
      worker_busy: this.full,
      slots: this.slots,
      active: active.map((worker) => worker.taskKey).filter(Boolean),
      active_count: active.length,
      // The lane of its own for supervise rounds, and whether a round runs.
      supervise_slots: SUPERVISE_SLOTS,
      supervising: this.count("supervise"),
      draining: this.draining,
      standby: this.standby.filter((item) => item.ready).length,
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

  /** A subscription sign-in the console started (login.mjs); only its updates go back. */
  async #login(type, payload) {
    const id = typeof payload.login_id === "string" ? payload.login_id.slice(0, 64) : "";
    if (!id) return;
    if (type === "login.code") {
      if (!this.logins.get(id)?.code(payload.code)) {
        this.#send("login.update", { login_id: id, state: "failed",
          message: "That code could not be used here. Start the sign-in again." });
      }
      return;
    }
    if (type === "login.cancel") {
      this.logins.get(id)?.cancel();
      return;
    }
    if (this.logins.has(id)) return;
    // One sign-in per runtime at a time: a new one replaces the old.
    for (const running of this.logins.values()) {
      if (running?.runtime === payload.runtime) running.cancel();
    }
    this.logins.set(id, null); // taken while the module loads, so a repeated frame starts nothing
    const start = this.startLogin ?? (await import("./login.mjs")).startLogin;
    const { resolveCommand } = await import("./runner.mjs");
    this.say(`[login] signing ${payload.runtime} in for the console`);
    const login = start({ runtime: payload.runtime, agentId: this.agentId, resolve: resolveCommand,
      send: (update) => this.#send("login.update", { login_id: id, runtime: payload.runtime, ...update }) });
    login.runtime = payload.runtime;
    this.logins.set(id, login);
    const state = await login.done;
    this.logins.delete(id);
    this.say(`[login] ${payload.runtime}: ${state}`);
    // The new login's models and quota reach the board now.
    if (state === "connected") void this.hostRecord?.refresh?.();
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
  if (typeof WebSocket === "undefined") {
    // Node 18 and 20 have no built-in WebSocket; without one no work can arrive.
    throw new Error(`Node ${process.versions.node} cannot hold the Loop's connection: install Node 22 or newer, then run \`npx veynod\` again`);
  }
  const make = createRunner ?? (await import("./runner.mjs")).createRunner;
  const runnerOptions = { repos: options.repos || [], runtime: options.runtime || "", exec: options.exec || "",
    ...(options.provider ? { provider: options.provider } : {}), ...(options.model ? { model: options.model } : {}),
    ...(options.deploy ? { deploy: options.deploy } : {}) };
  // One prepared worker per work slot and one for the supervise lane, unless
  // turned off (VEYNOD_WARM_WORKERS=0, or options.warm === 0).
  const slots = Math.max(1, Number(options.slots) || 1);
  const asked = options.warm ?? process.env.VEYNOD_WARM_WORKERS;
  const warm = asked === undefined || asked === "" ? slots + SUPERVISE_SLOTS : Math.max(0, Number(asked) || 0);
  const messenger = new Messenger({
    host: options.host, key: options.key, agentId: options.agent, slots: options.slots,
    runnerOptions, say, warm, ...(spawner ? { spawner } : {}),
    ...(options.onRunnerMissing ? { onRunnerMissing: options.onRunnerMissing } : {}),
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
    console.error("usage: veynod-messenger --key KEY [--host URL] [--agent ID] [--slots N] [--warm N]");
    process.exit(2);
  }
  // How its workers run (repos, runtime, exec), as the hub passes it to them.
  let runnerOptions = {};
  try {
    runnerOptions = JSON.parse(process.env.VEYNOD_RUNNER_OPTIONS || "{}") || {};
  } catch {
    runnerOptions = {};
  }
  const messenger = new Messenger({
    host, key, agentId: args.agent || process.env.VEYNOD_AGENT_ID,
    runner: args.runner, slots: args.slots, warm: args.warm, runnerOptions,
    say: (line) => console.error(line),
  });
  void messenger.start();
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      messenger.stop();
      process.exit(0);
    });
  }
}

export { ORIENTATION_TIMEOUT_MS, SUPERVISE_SLOTS };
