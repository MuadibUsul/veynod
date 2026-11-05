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
import { chmodSync, cpSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { basename, dirname, join } from "node:path";

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

// ------------------------------------------------------------------ stable copy

/**
 * Where `up` keeps its own copy of the CLI, so the background service never
 * points into npm's npx cache (npm cleans it at will, and a service whose file
 * is gone just restarts a missing script). One directory per version: a hub
 * still running the old version keeps its files until the service restarts.
 */
export function stableDir(version, home = defaultHome()) {
  return join(veynodDir(home), "cli", String(version));
}

/**
 * Copies npx's install of this package to ~/.veynod/cli/<version>/node_modules/
 * and returns the copy's veynod.mjs. Nothing to install first: `npx veynod`
 * alone sets up a service that lasts.
 *
 * The whole npx node_modules goes, not only veynod and @botiverse/oar: npm
 * hoists OAR's own dependencies (the ACP SDK, pi, undici, which…) beside it.
 * Without them OAR failed to load in the service, which then ran Codex through
 * its CLI without the computer's key (mcp_pinning false, claims refused). That
 * directory is npx's own for this package: veynod's dependency tree, nothing else.
 */
export function installStableCopy(script, { version, home = defaultHome(), fs = { cpSync, mkdirSync } } = {}) {
  const packageDir = dirname(dirname(script)); // …/node_modules/veynod
  const modules = dirname(packageDir); // …/node_modules
  const target = join(stableDir(version, home), "node_modules");
  fs.mkdirSync(target, { recursive: true, mode: 0o700 });
  fs.cpSync(modules, target, { recursive: true, force: true });
  return join(target, basename(packageDir), "bin", basename(script));
}

/** Removes the copies of other versions once the service runs the new one. */
export function pruneStableCopies(version, home = defaultHome(), fs = { existsSync, readdirSync, rmSync }) {
  const root = join(veynodDir(home), "cli");
  if (!fs.existsSync(root)) return [];
  const removed = [];
  for (const name of fs.readdirSync(root)) {
    if (name === String(version)) continue;
    fs.rmSync(join(root, name), { recursive: true, force: true });
    removed.push(name);
  }
  return removed;
}

// ------------------------------------------------------------------ plan

/**
 * What `up` will do on this computer, without doing it: the identities to
 * connect, the clients to wire, the service to install and what the doctor
 * will check. `--dry-run` prints it; the tests read it.
 */
/**
 * Identities the hub starts: the computer's own. A `-review` identity from an
 * older setup stays parked in the config and is never started: the Kernel
 * gate reviews code, and a person reviews what no other computer can.
 */
export function hubWorkers(config = {}) {
  const identities = config.identities || [];
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

/**
 * Running `npx veynod` again (say, to upgrade) keeps what this computer chose
 * last time: whether it runs board tasks, with which runtime, slots and repos.
 * A flag on the command line still wins. `decided` says whether there is
 * anything left to ask.
 */
export function withEarlierChoices(options, previous) {
  const explicit = options.explicit || new Set();
  if (!previous || previous.host !== options.host || options.deploy) {
    return { ...options, decided: explicit.has("runTasks") || Boolean(options.deploy) };
  }
  const keep = (name, current, earlier) => (explicit.has(name) || earlier === undefined || earlier === null
    ? current : earlier);
  const runtime = keep("runtime", options.runtime, previous.runtime || undefined);
  return {
    ...options,
    runTasks: keep("runTasks", options.runTasks, previous.run_tasks === undefined ? undefined : Boolean(previous.run_tasks)),
    runtime,
    exec: options.exec || (runtime === "custom" ? previous.exec || "" : options.exec),
    slots: keep("slots", options.slots, previous.slots || undefined),
    supervisor: keep("supervisor", options.supervisor, previous.supervisor === undefined ? undefined : Boolean(previous.supervisor)),
    // --repo on a re-run adds a repository; it never drops the ones kept before.
    repos: [...new Set([...(previous.repos || []), ...(options.repos || [])])],
    decided: true,
  };
}

/**
 * Keeps only saved identities whose key the board still accepts. A key that
 * was revoked or expired answers 401/403: that identity goes back to the plan's
 * missing list, so this run asks for a new key instead of saying "already
 * connected" while the doctor says the key is refused. A network error keeps
 * the identity (no new approval just because the board was unreachable).
 */
export async function dropRefusedKeys(reused, plan, { host, fetchImpl = fetch, readKeyFile = readKey } = {}) {
  const refused = [];
  const accepted = async (identity) => {
    try {
      const { status } = await getJson(fetchImpl, `${host}/v1/whoami`, readKeyFile(identity));
      return status !== 401 && status !== 403;
    } catch {
      return true;
    }
  };
  const identities = [];
  const missing = [...reused.missing];
  for (const identity of reused.identities) {
    if (await accepted(identity)) {
      identities.push(identity);
      continue;
    }
    refused.push(identity.agent_id);
    const wanted = plan.identities.find((item) => item.role === identity.role);
    if (wanted && !missing.includes(wanted)) missing.push(wanted);
  }
  // A parked identity whose key is refused is of no use: forget it.
  const parked = [];
  for (const identity of reused.parked) {
    if (await accepted(identity)) parked.push(identity);
    else refused.push(identity.agent_id);
  }
  return { identities, missing, parked, refused };
}

export function upPlan(options, { home = defaultHome(), clients = [], name = hostname() } = {}) {
  // Names are agent ids too (a letter, then letters, digits and dashes, at most 40 long).
  const base = agentName(options.name || name);
  const identities = [{ role: "computer", label: base, wants_role: "EXECUTOR",
    why: "this computer's agent: your MCP clients and the hub use it" }];
  return {
    host: options.host,
    run_tasks: Boolean(options.runTasks),
    slots: Number(options.slots || 1),
    workers: 1,
    identities,
    clients,
    hub: {
      mode: options.runTasks ? "runner" : "listen",
      runtime: options.runtime,
      slots: options.slots,
      repos: options.repos,
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
export async function runHub(options, { listen, runComputer, version, say = () => {}, home = defaultHome(),
  config = loadConfig(home), forever = () => new Promise(() => {}) } = {}) {
  if (!config?.identities?.length) {
    throw new Error("no identities on this computer yet: run `npx veynod` first");
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
    const loops = [];
    for (const identity of workers) {
      releases.push(acquireLock(identity.agent_id, { home }));
      const key = readKey(identity);
      const tag = (text) => say(`[${identity.agent_id}] ${text}`);
      if (config.run_tasks) {
        // Work arrives only over the nerve socket the messenger holds.
        const messenger = await runComputer({ host, key, agent: identity.agent_id,
          runtime: config.runtime || "", exec: config.exec || "", slots: config.slots || 1,
          repos: config.repos || [], ...(config.deploy ? { deploy: config.deploy } : {}) },
        { version, say: tag });
        releases.push(() => messenger.stop());
        loops.push(forever());
      } else {
        loops.push(listen({ host, key, exec: config.exec || "", heartbeat: true }, tag));
      }
    }
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
    add("config", false, true, "This computer is not connected.", "Run `npx veynod`.");
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
        "Run `npx veynod` again to reconnect.");
      continue;
    }
    if (identity.role === "computer") computerKey = key;
    try {
      const who = await getJson(fetchImpl, `${config.host}/v1/whoami`, key);
      add(`key:${identity.agent_id}`, who.status === 200, required,
        who.status === 200 ? `${identity.agent_id} is accepted by the board.`
          : `The board refused ${identity.agent_id} (HTTP ${who.status}).`,
        who.status === 200 ? null : "The key was revoked or expired: run `npx veynod` again.");
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
  if (config.run_tasks && config.deploy) {
    // A deploy computer: docker and its compose directory, no agent runtime.
    const docker = onPath(run, "docker");
    add("docker", docker, true, docker ? "docker is installed." : "docker is missing.",
      docker ? null : "Install Docker with the compose plugin (`docker compose version`).");
    const dir = config.deploy.dir;
    const there = Boolean(dir) && existsSync(join(dir, config.deploy.file || "docker-compose.yml"));
    add("compose", there, true, there ? `Deploys run in ${dir}.`
      : `No compose file ${config.deploy.file || "docker-compose.yml"} in ${dir || "(no directory)"}.`,
    there ? null : "Run `veynod up --deploy --compose-dir DIR --compose-file FILE` with the right path.");
  } else if (config.run_tasks) {
    add("reviewer", true, false,
      "The Kernel gate reviews code built here; a person reviews what no other computer can.");
    // No saved runtime means the hub uses whichever is here.
    const wanted = config.runtime ? [config.runtime] : ["claude", "codex"];
    const runtime = config.runtime === "custom" ? "custom" : wanted.find((id) => onPath(run, id));
    const ready = runtime === "custom" ? Boolean(config.exec) : Boolean(runtime);
    const named = wanted.map((id) => RUNTIME_NAMES[id] ?? id).join(" or ");
    add("runtime", ready, true,
      ready ? `${RUNTIME_NAMES[runtime] ?? runtime} is installed.` : `${named} is not installed or not on PATH.`,
      ready ? null : `Install ${named} (the command-line tool, not the desktop app) and sign in; `
        + "run `npx veynod` again to see the commands.");
    if ((config.repos || []).length) {
      const git = onPath(run, "git");
      add("git", git, true, git ? "git is installed." : "git is missing.", git ? null : "Install git.");
      const gh = onPath(run, "gh");
      add("gh", gh, false, gh ? "gh is installed (pull requests)." : "gh is missing: agents cannot open pull requests.",
        gh ? null : "Install the GitHub CLI and run `gh auth login`.");
    }
  }
  if (typeof serviceInstalled === "function") {
    // Without it nothing reaches this computer: assignments wait unseen.
    const installed = serviceInstalled();
    add("service", installed, true, installed
      ? (config.run_tasks ? "The hub runs as a background service." : "The background service keeps this computer online.")
      : "No background service: nothing reaches this computer while it is not running.",
    installed ? null : "Run `npx veynod` again (it installs it).");
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
