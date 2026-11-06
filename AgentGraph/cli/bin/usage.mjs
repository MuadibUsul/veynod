// What a computer's agents can report about themselves: the models they offer
// and how much of the person's subscription is left, read from each agent's
// own command-line tool. It runs on any Node, with no adapter layer:
//   Codex        `codex app-server --listen stdio://`, JSON lines:
//                initialize, account/read, account/rateLimits/read, model/list
//   Claude Code  `claude -p --input-format stream-json --output-format
//                stream-json --safe-mode`, control requests: initialize
//                (models), get_usage (rate limits)
// Neither sends a prompt or reads credentials; account emails are not kept.
// Checked against Codex CLI 0.167 and Claude Code 2.1 on 2026-11-06.

import { spawn } from "node:child_process";

const HOUR = 60;
const WEEK = 7 * 24 * HOUR;

/**
 * A child process spoken to in JSON lines. `ask(payload, match)` writes one
 * line and resolves the first reply `match` accepts, or null once the process
 * has exited or the deadline passed. Null when the command is not installed.
 */
export function lineSession(name, args, { resolve, env = process.env, timeoutMs = 10_000, spawnImpl = spawn } = {}) {
  const target = resolve(name);
  if (!target) return null;
  let child;
  try {
    child = spawnImpl(target.command, [...target.prefix, ...args],
      { env, stdio: ["pipe", "pipe", "ignore"], windowsHide: true });
  } catch {
    return null;
  }
  const waiting = [];
  let closed = false;
  let buffer = "";
  const finish = () => {
    closed = true;
    for (const waiter of waiting.splice(0)) waiter.resolve(null);
  };
  child.on("error", finish);
  child.on("exit", finish);
  child.stdin?.on("error", finish);
  child.stdout?.on("data", (chunk) => {
    buffer += chunk;
    let end;
    while ((end = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      let message;
      try { message = JSON.parse(line); } catch { continue; }
      const at = waiting.findIndex((waiter) => waiter.match(message));
      if (at >= 0) waiting.splice(at, 1)[0].resolve(message);
    }
  });
  const timer = setTimeout(() => { finish(); child.kill(); }, timeoutMs);
  return {
    ask(payload, match) {
      if (closed) return Promise.resolve(null);
      return new Promise((resolveReply) => {
        waiting.push({ match, resolve: resolveReply });
        child.stdin.write(`${JSON.stringify(payload)}\n`);
      });
    },
    tell(payload) {
      if (!closed) child.stdin.write(`${JSON.stringify(payload)}\n`);
    },
    close() {
      clearTimeout(timer);
      finish();
      child.kill();
    },
  };
}

const percent = (value) => (typeof value === "number" && value >= 0 ? Math.min(100, Math.round(value * 10) / 10) : null);
const isoFromSeconds = (value) => (typeof value === "number" && value > 0 ? new Date(value * 1000).toISOString() : null);
const isoFromText = (value) => {
  const time = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? new Date(time).toISOString() : null;
};

function windowName(minutes) {
  if (typeof minutes !== "number" || minutes <= 0) return "usage limit";
  if (minutes % WEEK === 0) return `${minutes / WEEK} ${minutes === WEEK ? "week" : "weeks"}`;
  if (minutes % (24 * HOUR) === 0) return `${minutes / (24 * HOUR)} ${minutes === 24 * HOUR ? "day" : "days"}`;
  if (minutes % HOUR === 0) return `${minutes / HOUR} ${minutes === HOUR ? "hour" : "hours"}`;
  return `${minutes} minutes`;
}

/**
 * Codex's account/rateLimits/read result as the board's usage windows
 * ({name, used_percent, resets_at}). `rateLimits` is the main bucket;
 * `rateLimitsByLimitId` adds the others (a model with its own limit).
 */
export function codexUsageWindows(result) {
  const main = result?.rateLimits && typeof result.rateLimits === "object" ? result.rateLimits : null;
  const buckets = main ? [main] : [];
  for (const [limitId, bucket] of Object.entries(result?.rateLimitsByLimitId || {})) {
    if (!bucket || typeof bucket !== "object") continue;
    if (main && (main.limitId === limitId || main.limitId === bucket.limitId)) continue;
    buckets.push(bucket);
  }
  const windows = [];
  let plan;
  for (const bucket of buckets) {
    plan ??= typeof bucket.planType === "string" ? bucket.planType : undefined;
    const label = bucket.limitName || (bucket.limitId === "codex" ? "Codex" : bucket.limitId) || "Codex";
    for (const kind of ["primary", "secondary"]) {
      const window = bucket[kind];
      const used = percent(window?.usedPercent);
      if (used === null) continue;
      const resets = isoFromSeconds(window.resetsAt);
      windows.push({ name: `${label} · ${windowName(window.windowDurationMins)}`, used_percent: used,
        ...(resets ? { resets_at: resets } : {}) });
    }
  }
  return windows.length ? { ...(plan ? { plan } : {}), windows } : null;
}

/** Claude Code's get_usage reply as usage windows; null when it has none (an API key). */
export function claudeUsageWindows(reply) {
  if (reply?.rate_limits_available !== true || !reply.rate_limits) return null;
  const limits = reply.rate_limits;
  const windows = [];
  const add = (name, value) => {
    const used = percent(value?.utilization);
    if (used === null) return;
    const resets = isoFromText(value.resets_at);
    windows.push({ name, used_percent: used, ...(resets ? { resets_at: resets } : {}) });
  };
  add("Current session", limits.five_hour);
  add("Current week (all models)", limits.seven_day);
  add("Current week (OAuth apps)", limits.seven_day_oauth_apps);
  if (Array.isArray(limits.model_scoped)) {
    for (const entry of limits.model_scoped) {
      if (typeof entry?.display_name === "string" && entry.display_name.trim()) {
        add(`Current week (${entry.display_name.trim()})`, entry);
      }
    }
  } else {
    add("Current week (Opus)", limits.seven_day_opus);
    add("Current week (Sonnet)", limits.seven_day_sonnet);
  }
  const plan = typeof reply.subscription_type === "string" ? reply.subscription_type : undefined;
  return windows.length ? { ...(plan ? { plan } : {}), windows } : null;
}

/**
 * Codex's models and subscription usage: {models?, usage?}, or null when it
 * cannot say. Its app-server answers within a second once up, but a first
 * start can take several (it reaches the network), hence the long budget: the
 * read runs in the background every ten minutes, so waiting costs nothing.
 */
export async function readCodex({ resolve, env, timeoutMs = 20_000, spawnImpl } = {}) {
  const session = lineSession("codex", ["app-server", "--listen", "stdio://"], { resolve, env, timeoutMs, spawnImpl });
  if (!session) return null;
  let id = 0;
  const request = (method, params = {}) => {
    const n = ++id;
    return session.ask({ id: n, method, params }, (message) => message?.id === n);
  };
  try {
    const hello = await request("initialize", { clientInfo: { name: "veynod", version: "cli" },
      capabilities: { experimentalApi: true } });
    if (!hello?.result) return null;
    session.tell({ method: "initialized", params: {} });
    const found = {};
    const listed = await request("model/list");
    const models = (listed?.result?.data || []).filter((model) => !model?.hidden && typeof model?.id === "string")
      .map((model) => model.id);
    if (models.length) found.models = models;
    // Only a ChatGPT login has subscription windows; an API key has none to report.
    const account = (await request("account/read"))?.result;
    if (account?.account?.type === "chatgpt") {
      const usage = codexUsageWindows((await request("account/rateLimits/read"))?.result);
      if (usage) found.usage = usage;
    }
    return found;
  } finally {
    session.close();
  }
}

/** Claude Code's models and subscription usage: {models?, usage?}, or null when it cannot say. */
export async function readClaude({ resolve, env, timeoutMs = 30_000, spawnImpl } = {}) {
  const session = lineSession("claude", ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
    "--verbose", "--no-session-persistence", "--safe-mode"], { resolve, env, timeoutMs, spawnImpl });
  if (!session) return null;
  let id = 0;
  const control = (request) => {
    const requestId = `veynod-${++id}`;
    return session.ask({ type: "control_request", request_id: requestId, request },
      (message) => message?.type === "control_response" && message.response?.request_id === requestId)
      .then((message) => (message?.response?.subtype === "success" ? message.response.response : null));
  };
  try {
    const hello = await control({ subtype: "initialize" });
    if (!hello) return null;
    const found = {};
    const models = (hello.models || []).map((model) => model?.value).filter((value) => typeof value === "string");
    if (models.length) found.models = models;
    const usage = claudeUsageWindows(await control({ subtype: "get_usage", skip_behaviors: true }));
    if (usage) found.usage = usage;
    return found;
  } finally {
    session.close();
  }
}

/** What one runtime reports beyond its version. */
export function readAgent(id, options) {
  if (id === "codex") return readCodex(options);
  if (id === "claude") return readClaude(options);
  return Promise.resolve(null);
}
