// veynod up / hub / doctor: one command puts this computer on the board
// (docs/WAKE_AND_DISPATCH.md §14, board task up-hub-p1-up-hub).
//
// `up` connects the computer's identities through the console's device flow,
// keeps their keys in ~/.veynod/keys (0600), wires the MCP clients with the
// computer identity's key, installs one hub service and runs the doctor.
// `hub` is that service: one process that serves every identity in the config,
// one inbox stream per key (a token may hold only one), as a runner when the
// computer runs board tasks and as a listener otherwise. `doctor` says what is
// missing, in plain words, with the fix.

import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { dirname, join } from "node:path";

import { RUNTIME_NAMES, commandWorks } from "./runner.mjs";

export const CONFIG_VERSION = 1;
const NODE_FOR_OAR = 24;

// ------------------------------------------------------------------ files

// Tests and CI set HOME to a throwaway tree; node's homedir() ignores HOME on
// Windows (it prefers USERPROFILE), so honour VEYNOD_HOME/HOME explicitly.
export function defaultHome() {
  return process.env.VEYNOD_HOME || process.env.HOME || homedir();
}

export function veynodDir(home = defaultHome()) {
  return join(home, ".veynod");
}

export function configPath(home = defaultHome()) {
  return join(veynodDir(home), "config.json");
}

const safeName = (value) => String(value).replace(/[^A-Za-z0-9_.-]+/g, "_").slice(0, 64) || "agent";

export function agentName(value) {
  const clean = String(value).toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/-+/g, "-")
    .replace(/^[^a-z]+/, "").slice(0, 33).replace(/-+$/, "");
  return clean.length >= 3 ? clean : `computer${clean ? `-${clean}` : ""}`;
}

export function keyPath(agentId, home = defaultHome()) {
  return join(veynodDir(home), "keys", `${safeName(agentId)}.key`);
}

export function loadConfig(home = defaultHome()) {
  try {
    return JSON.parse(readFileSync(configPath(home), "utf8"));
  } catch {
    return null;
  }
}

function writePrivate(path, content) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  writeFileSync(`${path}.tmp`, content, { mode: 0o600 });
  renameSync(`${path}.tmp`, path);
  chmodSync(path, 0o600);
}

export function saveConfig(config, home = defaultHome()) {
  writePrivate(configPath(home), `${JSON.stringify({ version: CONFIG_VERSION, ...config }, null, 2)}\n`);
  return configPath(home);
}

export function storeKey(agentId, key, home = defaultHome()) {
  const path = keyPath(agentId, home);
  writePrivate(path, `${key}\n`);
  return path;
}

export function readKey(identity) {
  return readFileSync(identity.key_file, "utf8").trim();
}

// ------------------------------------------------------------------ plan

/**
 * What `up` will do on this computer, without doing it: the identities to
 * connect, the clients to wire, the service to install and what the doctor
 * will check. `--dry-run` prints it; the tests read it.
 */
/** A permanent -review twin only when slots > 1 or the legacy dual path is asked for. */
export function reviewTwinWanted(options = {}) {
  if (!options.runTasks && !options.run_tasks) return false;
  if (options.legacyReview || options.legacy_review || options.delivery === "legacy") return true;
  return Number(options.slots || 1) > 1;
}

/**
 * Identities that get a worker process. slots=1 on the kernel path is exactly
 * one worker: the -review identity, even if an old config still has it, is not
 * started and therefore cannot spawn a second Claude.
 */
export function hubWorkers(config = {}) {
  const identities = config.identities || [];
  if (reviewTwinWanted(config)) return identities;
  const workers = identities.filter((item) => item.role !== "review");
  return workers.length ? workers : identities.slice(0, 1);
}

/**
 * Which saved identities `up` reuses. One the plan wants is kept while its key
 * file is there. One it no longer wants (the -review seat after going from
 * --slots 2 to 1) is parked in the config instead of forgotten: the hub does
 * not start it (hubWorkers), and asking for it again reuses its key rather than
 * minting another `-review-2`, `-review-3`… identity on the board.
 */
export function reuseIdentities(plan, previous, { host, fresh = false, exists = existsSync } = {}) {
  const usable = !fresh && previous?.host === host
    ? (previous.identities || []).filter((item) => item?.key_file && exists(item.key_file)) : [];
  const identities = [];
  const missing = [];
  for (const wanted of plan.identities) {
    const kept = usable.find((item) => item.role === wanted.role);
    if (kept) identities.push(kept);
    else missing.push(wanted);
  }
  const parked = usable.filter((item) => !plan.identities.some((wanted) => wanted.role === item.role));
  return { identities, missing, parked };
}

export function upPlan(options, { home = defaultHome(), clients = [], name = hostname() } = {}) {
  // Names are agent ids too (a letter, then letters, digits and dashes, at most
  // 40 long), with room for the `-review` suffix when slots > 1.
  const base = agentName(options.name || name);
  const identities = [{ role: "computer", label: base, wants_role: "EXECUTOR",
    why: "this computer's agent: your MCP clients and the hub use it" }];
  const reviewTwin = reviewTwinWanted(options);
  if (reviewTwin) {
    identities.push({ role: "review", label: `${base}-review`, wants_role: "AUDITOR",
      why: "reviews what this computer built, so the Loop can finish a task here" });
  }
  const workers = reviewTwin ? identities : identities.filter((item) => item.role !== "review");
  return {
    host: options.host,
    run_tasks: Boolean(options.runTasks),
    slots: Number(options.slots || 1),
    delivery: options.delivery || "kernel",
    review_twin: reviewTwin,
    workers: workers.length,
    identities,
    clients,
    hub: {
      mode: options.runTasks ? "runner" : "listen",
      runtime: options.runtime,
      slots: options.slots,
      repos: options.repos,
      delivery: options.delivery || "kernel",
    },
    files: [configPath(home), ...identities.map((item) => keyPath(item.label, home))],
  };
}

// ------------------------------------------------------------------ hub

/** One process per key: a second hub on the same key would fight for its one stream. */
export function acquireLock(agentId, { home = defaultHome(), pid = process.pid, alive = isAlive } = {}) {
  const path = join(veynodDir(home), `hub-${safeName(agentId)}.lock`);
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  try {
    const held = Number(readFileSync(path, "utf8").trim());
    if (held && held !== pid && alive(held)) {
      throw Object.assign(new Error(`another hub (pid ${held}) already serves ${agentId}`), { code: "hub_running" });
    }
  } catch (error) {
    if (error.code === "hub_running") throw error;
  }
  writeFileSync(path, String(pid), { mode: 0o600 });
  return () => {
    try {
      if (Number(readFileSync(path, "utf8").trim()) === pid) unlinkSync(path);
    } catch {
      // already gone
    }
  };
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

/**
 * The hub: every identity in the config, one stream each, in this process.
 * With run_tasks each identity runs as a runner (the scheduler places work and
 * reviews on it); otherwise each one listens, so pokes reach --exec hooks.
 */
export async function runHub(options, { follow, listen, runRunner, version, say = () => {}, home = defaultHome(),
  config = loadConfig(home) } = {}) {
  if (!config?.identities?.length) {
    throw new Error("no identities on this computer yet: run `npx veynod up` first");
  }
  const host = options.host && options.host !== "https://veynod.com" ? options.host : config.host;
  const releases = [];
  // A service stop (SIGTERM) lets go of the locks, so a reused pid never blocks a restart.
  const stop = () => {
    for (const release of releases) release();
    process.exit(0);
  };
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
  try {
    const workers = hubWorkers(config);
    const slots = Number(config.slots || 1);
    if (config.run_tasks && !reviewTwinWanted(config)) {
      say(`slots=${slots} workers=${workers.length} review_twin=absent`);
    }
    const loops = workers.map((identity) => {
      releases.push(acquireLock(identity.agent_id, { home }));
      const key = readKey(identity);
      const stateDir = join(veynodDir(home), "hub", safeName(identity.agent_id));
      const tag = (text) => say(`[${identity.agent_id}] ${text}`);
      if (config.run_tasks) {
        return runRunner({ host, key, runtime: config.runtime || "", exec: config.exec || "",
          slots: config.slots || 1, repos: config.repos || [], diskMb: config.disk_mb || 5120,
          gitBase: config.git_base || "https://github.com/",
          delivery: config.delivery || "kernel" },
        { follow, version, say: tag, stateDir });
      }
      return listen({ host, key, exec: config.exec || "", heartbeat: true }, tag);
    });
    const results = await Promise.all(loops);
    return results.some((code) => code) ? 1 : 0;
  } finally {
    process.removeListener("SIGTERM", stop);
    process.removeListener("SIGINT", stop);
    for (const release of releases) release();
  }
}

// ------------------------------------------------------------------ doctor

async function getJson(fetchImpl, url, key) {
  const response = await fetchImpl(url, { headers: { Authorization: `Bearer ${key}` },
    signal: AbortSignal.timeout(15_000) });
  const data = await response.json().catch(() => ({}));
  return { status: response.status, data };
}

// Resolved like the runner does, so a Windows npm shim (claude.cmd) counts.
const onPath = (run, command) => commandWorks(command, { run }) !== null;

/**
 * What is missing on this computer and on the board, one line each:
 * {id, ok, required, text, fix}. `up` exits non-zero while a required item is red.
 */
export async function doctor({ home = defaultHome(), config = loadConfig(home), fetchImpl = fetch,
  run = execFileSync, nodeVersion = process.versions.node, serviceInstalled } = {}) {
  const checks = [];
  const add = (id, ok, required, text, fix = null) => checks.push({ id, ok: Boolean(ok), required, text, fix });
  if (!config) {
    add("config", false, true, "This computer is not connected.", "Run `npx veynod up`.");
    return checks;
  }
  add("config", true, true, `Connected to ${config.host}.`);
  let computerKey = null;
  // A parked identity (not started by the hub) cannot make the doctor red.
  const started = new Set(hubWorkers(config).map((item) => item.agent_id));
  for (const identity of config.identities || []) {
    const required = started.has(identity.agent_id);
    let key = null;
    try {
      key = readKey(identity);
    } catch {
      add(`key:${identity.agent_id}`, false, required, `The key for ${identity.agent_id} is missing.`,
        "Run `npx veynod up` again to reconnect.");
      continue;
    }
    if (identity.role === "computer") computerKey = key;
    try {
      const who = await getJson(fetchImpl, `${config.host}/v1/whoami`, key);
      add(`key:${identity.agent_id}`, who.status === 200, required,
        who.status === 200 ? `${identity.agent_id} is accepted by the board.`
          : `The board refused ${identity.agent_id} (HTTP ${who.status}).`,
        who.status === 200 ? null : "The key was revoked or expired: run `npx veynod up` again.");
    } catch (error) {
      add(`key:${identity.agent_id}`, false, required, `Veynod at ${config.host} did not answer (${error.message}).`,
        "Check the network or --host.");
    }
  }
  const major = Number(String(nodeVersion).split(".")[0]);
  add("node", major >= NODE_FOR_OAR, false,
    major >= NODE_FOR_OAR ? `Node ${nodeVersion}: OAR can drive the runtimes.`
      : `Node ${nodeVersion}: below ${NODE_FOR_OAR}, so OAR is unavailable and the runtime's CLI is used.`,
    major >= NODE_FOR_OAR ? null : `Install Node ${NODE_FOR_OAR} or newer for OAR (quota-aware, pinned sessions).`);
  if (config.run_tasks) {
    const slots = Number(config.slots || 1);
    const kernelReviews = (config.delivery || "kernel") !== "legacy" && !config.legacy_review && slots <= 1;
    const roles = new Set((config.identities || []).map((item) => item.role));
    if (kernelReviews) {
      add("reviewer", true, true,
        "slots=1: one worker. Kernel verification_gate and merge_coordinator review this computer's work. review_twin=absent.");
    } else {
      add("reviewer", roles.has("review"), true,
        roles.has("review") ? "A second identity reviews what this computer builds."
          : "No reviewer identity: a task built here cannot be reviewed here.",
        roles.has("review") ? null : "Run `npx veynod up --run-tasks --slots 2` or `--legacy-review` to add one.");
    }
    // No saved runtime means the hub uses whichever is here.
    const wanted = config.runtime ? [config.runtime] : ["claude", "codex"];
    const runtime = config.runtime === "custom" ? "custom" : wanted.find((id) => onPath(run, id));
    const ready = runtime === "custom" ? Boolean(config.exec) : Boolean(runtime);
    const named = wanted.map((id) => RUNTIME_NAMES[id] ?? id).join(" or ");
    add("runtime", ready, true,
      ready ? `${RUNTIME_NAMES[runtime] ?? runtime} is installed.` : `${named} is not installed or not on PATH.`,
      ready ? null : `Install ${named} (the command-line tool, not the desktop app) and sign in; `
        + "run `veynod up` again to see the commands.");
    if ((config.repos || []).length) {
      const git = onPath(run, "git");
      add("git", git, true, git ? "git is installed." : "git is missing.", git ? null : "Install git.");
      const gh = onPath(run, "gh");
      add("gh", gh, false, gh ? "gh is installed (pull requests)." : "gh is missing: agents cannot open pull requests.",
        gh ? null : "Install the GitHub CLI and run `gh auth login`.");
    }
    if (typeof serviceInstalled === "function") {
      const installed = serviceInstalled();
      add("service", installed, true, installed ? "The hub runs as a background service."
        : "The hub is not installed as a service.", installed ? null : "Run `npx veynod up` again (it installs it).");
    }
  }
  if (computerKey) {
    try {
      const board = await getJson(fetchImpl, `${config.host}/v1/runner/doctor`, computerKey);
      if (board.status === 200) {
        for (const item of board.data.checks || []) {
          add(`board:${item.id}`, item.ok, Boolean(item.required) && config.run_tasks, item.text, item.fix || null);
        }
      } else if (board.status === 404) {
        add("board", true, false, "This Veynod server has no board doctor yet (older release).");
      }
    } catch {
      // the key check above already reported the network
    }
  }
  return checks;
}

export function doctorOk(checks) {
  return checks.every((item) => item.ok || !item.required);
}
