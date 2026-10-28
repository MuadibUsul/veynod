// Resident wake queue. Sessions stay open, so an idle agent gets a new turn
// now and a busy one gets the prompt after its current turn. Every accepted
// input gets three receipts (delivered, accepted, started). A failed local
// wake falls back to a server task assignment after three attempts. Urgent
// wakes must pass the server lease/fencing guard first and leave a progress
// snapshot in ~/.veynod/wake-audit.jsonl.
import { appendFileSync, existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { claudeRuntime } from "@botiverse/oar/runtimes/claude";
import { codexRuntime } from "@botiverse/oar/runtimes/codex";
import { pending, receipt, update } from "./queue.mjs";

const RUNTIMES = { claude: claudeRuntime, codex: codexRuntime };
const sessions = new Map();
const inflight = new Set();
const watching = new Map();
const dir = process.env.VEYNOD_HOME || join(homedir(), ".veynod");
const auditFile = join(dir, "wake-audit.jsonl");
const lockFile = join(dir, "wake-daemon.lock");

// Two daemons would both read the same pending item and deliver it twice.
// One resident daemon per computer; a stale lock from a crash is taken over.
function acquireLock() {
  mkdirSync(dir, { recursive: true });
  try {
    const pid = Number(readFileSync(lockFile, "utf8").trim());
    if (pid && pid !== process.pid) {
      try {
        process.kill(pid, 0);
        return false;
      } catch (error) {
        if (error?.code === "EPERM") return false;
      }
    }
  } catch { /* no lock yet */ }
  writeFileSync(lockFile, String(process.pid));
  return true;
}
function releaseLock() {
  try {
    if (Number(readFileSync(lockFile, "utf8").trim()) === process.pid) unlinkSync(lockFile);
  } catch { /* already gone */ }
}
if (!acquireLock()) process.exit(0);

// The client starts this daemon as a child. When the window is quit (or the
// tray is closed) the parent is gone; stop instead of lingering with an open
// OAR session and a stale wake-daemon.lock.
const parentPid = Number(process.env.VEYNOD_PARENT_PID || 0);
if (parentPid > 0) {
  const parentTimer = setInterval(() => {
    try {
      process.kill(parentPid, 0);
    } catch (error) {
      if (error?.code === "EPERM") return;
      for (const timer of watching.values()) clearInterval(timer);
      releaseLock();
      process.exit(0);
    }
  }, 5000);
  parentTimer.unref();
}

function desktopConfig() {
  try { return JSON.parse(readFileSync(join(dir, "desktop.json"), "utf8")); }
  catch { return { agents: [], host: "https://veynod.com" }; }
}
function keyFor(agent) {
  if (!agent) return null;
  for (const name of [agent, agent.toLowerCase()]) {
    try {
      const key = readFileSync(join(dir, `${name}.key`), "utf8").trim();
      if (key) return key;
    } catch { /* try the next spelling */ }
  }
  return null;
}
function audit(value) {
  appendFileSync(auditFile, JSON.stringify({ at: new Date().toISOString(), ...value }) + "\n");
}
function readAudit(limit = 40) {
  if (!existsSync(auditFile)) return [];
  const rows = [];
  for (const line of readFileSync(auditFile, "utf8").trim().split("\n").slice(-limit)) {
    try { rows.push(JSON.parse(line)); } catch { /* ignore a torn line */ }
  }
  return rows;
}

async function sessionFor(item) {
  const key = `${item.runtime}:${item.cwd}`;
  if (sessions.has(key)) return sessions.get(key);
  const runtime = RUNTIMES[item.runtime];
  if (!runtime) throw new Error(`unsupported runtime ${item.runtime}`);
  const installation = await runtime.installation();
  if (installation?.kind !== "available") throw new Error("runtime unavailable");
  const session = await runtime.session(installation, { cwd: item.cwd });
  sessions.set(key, session);
  return session;
}

function statusOf(session) {
  return session.status?.()?.value?.kind ?? "unknown";
}

/// Urgent wakes are an interrupt: an episode of two people steering one agent
/// is exactly what the lease exists to prevent. Check the server, not the
/// local cache, and refuse when the lease is not held by this agent or the
/// caller's fencing token is stale.
async function guardUrgent(item, session) {
  if (!item.task_key) throw new Error("urgent wake refused: a task_key is required");
  if (!item.agent) throw new Error("urgent wake refused: agent is required");
  const key = keyFor(item.agent);
  if (!key) throw new Error(`urgent wake refused: no key for ${item.agent}`);
  const host = (desktopConfig().host || "https://veynod.com").replace(/\/$/, "");
  const response = await fetch(`${host}/v1/tasks/${encodeURIComponent(item.task_key)}`, {
    headers: { Authorization: `Bearer ${key}`, Accept: "application/json" },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`urgent wake refused: task read returned ${response.status}`);
  const task = await response.json();
  const holder = task.claimed_by || (task.assignee || "").replace(/^agent:/, "");
  if (!task.lease_active) throw new Error("urgent wake refused: no active lease");
  if (holder && holder.toLowerCase() !== item.agent.toLowerCase()) {
    throw new Error(`urgent wake refused: lease held by ${holder}`);
  }
  if (item.expected_fencing_token != null && task.fencing_token != null
      && Number(item.expected_fencing_token) !== Number(task.fencing_token)) {
    throw new Error(`urgent wake refused: fencing token ${item.expected_fencing_token} != ${task.fencing_token}`);
  }
  const snapshot = {
    session_id: session.id,
    status: statusOf(session),
    lease_epoch: task.lease_epoch ?? null,
    fencing_token: task.fencing_token ?? null,
    holder: holder || null,
    lease_expires_at: task.lease_expires_at ?? null,
    task_status: task.status ?? null,
  };
  audit({ kind: "wake.urgent", decision: "allowed", input_id: item.input_id,
    task_key: item.task_key, agent: item.agent, snapshot });
  return snapshot;
}

function trackStarted(item, session, statusBefore) {
  if (watching.has(item.input_id)) return;
  const startedAt = Date.now();
  let waitingForIdle = statusBefore === "running";
  const timer = setInterval(() => {
    try {
      const status = statusOf(session);
      if (waitingForIdle && status !== "running") waitingForIdle = false;
      if (!waitingForIdle && status === "running") {
        clearInterval(timer);
        watching.delete(item.input_id);
        update(item.input_id, { state: "started", started_at: new Date().toISOString() });
        receipt({ stage: "started", input_id: item.input_id, task_key: item.task_key,
          runtime: item.runtime, runtime_status: status, session_id: session.id });
        audit({ kind: "wake.started", input_id: item.input_id, task_key: item.task_key,
          agent: item.agent || null, session_id: session.id });
      } else if (Date.now() - startedAt > 30 * 60 * 1000) {
        clearInterval(timer);
        watching.delete(item.input_id);
      }
    } catch {
      clearInterval(timer);
      watching.delete(item.input_id);
    }
  }, 3000);
  watching.set(item.input_id, timer);
}

async function serverFallback(item, error) {
  const agent = item.agent;
  const key = keyFor(agent) || keyFor(desktopConfig().agents?.[0]);
  if (!key) {
    receipt({ stage: "fallback", input_id: item.input_id, task_key: item.task_key,
      failed: true, error: `local wake failed and no key is available to assign a task: ${error}` });
    return;
  }
  const host = (desktopConfig().host || "https://veynod.com").replace(/\/$/, "");
  const title = `本地唤起失败，已回退到任务指派：${item.task_key || item.input_id}`.slice(0, 256);
  const body = {
    title,
    description: [
      "本地 harness 唤起连续三次失败，客户端已按协议回退到服务端任务指派。",
      "",
      `runtime: ${item.runtime}`,
      `cwd: ${item.cwd}`,
      `input_id: ${item.input_id}`,
      `task_key: ${item.task_key || "(none)"}`,
      `urgent: ${Boolean(item.urgent)}`,
      `error: ${error}`,
      "",
      "原始唤起内容：",
      String(item.prompt || "").slice(0, 12000),
    ].join("\n"),
    status: "READY",
    priority: item.urgent ? 10 : 0,
    input_context: {
      source: "local_wake_fallback",
      input_id: item.input_id,
      runtime: item.runtime,
      cwd: item.cwd,
      task_key: item.task_key || null,
      error,
    },
    ...(agent ? { assignee: `agent:${agent}` } : {}),
  };
  try {
    const response = await fetch(`${host}/v1/tasks`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json",
        Accept: "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(20000),
    });
    const payload = await response.json().catch(() => ({}));
    const assigned = response.ok ? (payload.task_key || (payload.task || {}).task_key) : null;
    receipt({ stage: "fallback", input_id: item.input_id, task_key: item.task_key,
      fallback: response.ok ? "task_assigned" : "task_assign_failed",
      fallback_task: assigned, status: response.status,
      error: response.ok ? undefined : (payload.detail || payload.error || error) });
    if (response.ok) update(item.input_id, { fallback_task: assigned });
  } catch (fetchError) {
    receipt({ stage: "fallback", input_id: item.input_id, task_key: item.task_key,
      fallback: "task_assign_failed", error: String(fetchError) });
  }
}

async function processItem(item) {
  if (inflight.has(item.input_id)) return;
  inflight.add(item.input_id);
  try {
    const session = await sessionFor(item);
    const status = statusOf(session);
    let snapshot = null;
    if (item.urgent) {
      try {
        snapshot = await guardUrgent(item, session);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        update(item.input_id, { state: "refused", error: message,
          refused_at: new Date().toISOString() });
        receipt({ stage: "refused", input_id: item.input_id, task_key: item.task_key,
          urgent: true, error: message });
        return;
      }
    }
    const when = item.urgent ? "interrupt" : status === "running" ? "after_turn" : "now";
    const delivered = await session.deliver(item.prompt, {
      when,
      ...(item.urgent ? { maxWaitMs: 5000 } : {}),
      origin: "host",
      inputId: item.input_id,
    });
    if (delivered.landed === "rejected") throw new Error(delivered.reason || "delivery rejected");
    update(item.input_id, {
      state: delivered.landed === "queued" ? "queued" : "delivered",
      landed: delivered.landed,
      session_id: session.id,
      status_before: status,
      delivered_at: new Date().toISOString(),
    });
    receipt({ stage: "delivered", input_id: item.input_id, task_key: item.task_key,
      runtime: item.runtime, urgent: Boolean(item.urgent), landed: delivered.landed,
      session_id: session.id, status_before: status, snapshot });
    receipt({ stage: "accepted", input_id: item.input_id, task_key: item.task_key,
      landed: delivered.landed, session_id: session.id });
    trackStarted(item, session, status);
  } catch (error) {
    const attempts = Number(item.attempts || 0) + 1;
    const failed = attempts >= 3;
    update(item.input_id, {
      attempts,
      state: failed ? "fallback" : "pending",
      error: error instanceof Error ? error.message : String(error),
      ...(failed ? { fallback_at: new Date().toISOString() } : {}),
    });
    receipt({ stage: "failed", input_id: item.input_id, task_key: item.task_key,
      runtime: item.runtime, urgent: Boolean(item.urgent), failed, attempts,
      error: error instanceof Error ? error.message : String(error) });
    if (failed) await serverFallback(item, error instanceof Error ? error.message : String(error));
  } finally {
    inflight.delete(item.input_id);
  }
}

async function loop() {
  const items = pending();
  await Promise.all(items.map((item) => processItem(item)));
  setTimeout(loop, 1000);
}

process.on("exit", releaseLock);
process.on("SIGTERM", () => {
  for (const timer of watching.values()) clearInterval(timer);
  releaseLock();
  process.exit(0);
});

loop();
