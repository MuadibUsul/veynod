// veynod runner: this computer runs agents for a Veynod workspace (full-managed
// Loop, runner-1 and runner-4).
//
// It only connects out. On start it says hello with what the machine can do
// (system, Claude Code / Codex with versions, models and quota, slots, repos),
// then sends a heartbeat every 30 s with what it is running. The platform's
// scheduler places a task on it with a `task.placed` poke whose note carries the
// placement id; the runner wakes a local runtime in a fresh git worktree with a
// prompt to claim and do that task. It never claims for the agent: if the wake
// fails three times the task stays unclaimed and the scheduler moves it on.
//
// Runtimes go through OAR (@botiverse/oar, Node 24+) when it is installed, else
// through the runtime's own CLI (`claude -p`, `codex exec`); `--runtime custom
// --exec CMD` runs any command (tests, other agents). Every wake leaves
// receipts in ~/.veynod/runner-receipts.jsonl: delivered, accepted, started,
// then finished or failed.

import { execFileSync, spawn } from "node:child_process";
import { appendFileSync, existsSync, lstatSync, mkdirSync, readdirSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { arch as osArch, homedir, hostname, platform } from "node:os";
import { join } from "node:path";

export const RUNNER_HEARTBEAT_MS = 30_000;
// Leases are at least 60 s and heartbeat at a third of their TTL; renewing every
// two minutes keeps a default (900 s) lease alive with room to spare.
export const LEASE_KEEP_MS = 120_000;
// Placements a host may still work on; any other status means it moved on.
const OPEN_PLACEMENT = new Set(["offered", "claimed"]);
const WAKE_ATTEMPTS = 3;
const RUNTIME_IDS = ["claude", "codex"];
const PLACEMENT = /\bplc_[0-9A-Z]{10,}\b/;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ------------------------------------------------------------------ runtimes

async function oarRuntime(id, importer) {
  try {
    const mod = await importer(`@botiverse/oar/runtimes/${id}`);
    return mod[`${id}Runtime`] ?? null;
  } catch {
    return null;
  }
}

/** What runtimes this computer has, without account emails or tokens. */
export async function inventory({ importer = (spec) => import(spec), run = execFileSync } = {}) {
  const found = [];
  for (const id of RUNTIME_IDS) {
    const runtime = await oarRuntime(id, importer);
    if (runtime) {
      try {
        const installation = await runtime.installation();
        if (installation?.kind === "available") {
          const entry = { id, available: true, via: "oar" };
          if (installation.version) entry.version = String(installation.version);
          if (typeof runtime.listModels === "function") {
            const models = await runtime.listModels(installation).catch(() => null);
            if (models?.kind === "ok") entry.models = models.models.slice(0, 20).map((item) => item.id);
          }
          if (typeof runtime.accountUsage === "function") {
            const usage = await runtime.accountUsage(installation, { timeoutMs: 10_000 }).catch(() => null);
            if (usage?.kind === "available") {
              entry.usage = {
                ...(usage.plan ? { plan: usage.plan } : {}),
                windows: usage.windows.slice(0, 4).map((window) => ({
                  name: window.label,
                  used_percent: Math.round(window.usedRatio * 1000) / 10,
                  ...(window.resetsAt ? { resets_at: window.resetsAt } : {}),
                })),
              };
            }
          }
          found.push(entry);
          continue;
        }
      } catch {
        // fall through to the CLI probe
      }
    }
    try {
      const version = String(run(id, ["--version"], { encoding: "utf8", timeout: 10_000, stdio: ["ignore", "pipe", "ignore"] })).trim();
      found.push({ id, available: true, via: "cli", version: version.split("\n")[0].slice(0, 32) });
    } catch {
      found.push({ id, available: false });
    }
  }
  return found;
}

const STEP = /\bstep (work|review|merge|supervise)\b/;
const CHUNK = /\bdiagram (\S+) chunk (\S+)/;

/**
 * What a placement poke asks of this computer: its placement id (the
 * idempotency key), the step (work, review, merge, supervise), and the task
 * and diagram chunk it is about.
 */
export function placementOf(poke) {
  if (!poke || poke.kind !== "task.placed") return null;
  const match = PLACEMENT.exec(poke.note || "");
  if (!match) return null;
  const step = STEP.exec(poke.note)?.[1] ?? "work";
  const chunk = CHUNK.exec(poke.note);
  const key = poke.task_key || poke.doc_key || null;
  if (!key && step !== "supervise") return null;
  return {
    taskKey: step === "supervise" ? null : key,
    placementId: match[0],
    step,
    ...(chunk ? { diagramKey: chunk[1], chunkKey: chunk[2] } : {}),
  };
}

// Agents use Veynod's MCP tools; REST is the fallback for one that has none.
const NO_MCP = "Without Veynod's MCP tools, use the REST API instead: diagram actions are POST "
  + "/v1/diagrams/<key>/chunks/<chunk>/<gate|queue-check|merge>, loop actions are under /v1/runner "
  + "(agenda, placements/<id>/move, placements/<id>/withdraw).";

export function promptFor({ taskKey, placementId, branch, host, step = "work", diagramKey, chunkKey, agenda = [] }) {
  const where = branch
    ? `This directory is a fresh checkout of the default branch (local branch ${branch}).`
    : "Work in this directory.";
  const chunk = diagramKey ? ` It is chunk ${chunkKey} of diagram ${diagramKey}.` : "";
  if (step === "review") {
    return [
      `Veynod asks this computer to review task ${taskKey} (placement ${placementId}).${chunk}`,
      `1. Read it on Veynod (${host}) with get_audit_view("${taskKey}"): its acceptance, the brief, the pull request.`,
      `2. ${where} Check out the work under review and run the project's checks.`,
      diagramKey
        ? `3. If it passes: review_task("${taskKey}", approve) first, then the diagram gate: diagram(action="review", diagram_key="${diagramKey}", chunk_key="${chunkKey}", args={result: "pass", checks, evidence}). If not: review_task reject with what to fix, then the gate with result "fail" and the evidence.`
        : `3. Approve or reject with review_task("${taskKey}"), giving a result for every acceptance item and, when rejecting, exactly what to fix.`,
      "You did not build this; judge it only against its acceptance. Do not change the code yourself.",
      ...(diagramKey ? [NO_MCP] : []),
    ].join("\n");
  }
  if (step === "merge") {
    return [
      `Veynod asks this computer to merge chunk ${chunkKey} of diagram ${diagramKey} (task ${taskKey}, placement ${placementId}).`,
      `1. Read the chunk on Veynod (${host}): diagram(action="get", diagram_key="${diagramKey}") and the task's pull request.`,
      `2. ${where} If the chunk waits in the merge queue, rebase its branch on the default branch,`,
      `   run the chunk's commands, and report diagram(action="queue_check", diagram_key="${diagramKey}", chunk_key="${chunkKey}",`,
      "   args={result: \"pass\"} or rebase_conflict / test_failed with the evidence; stop there if it is not a pass).",
      "3. Merge the pull request into the default branch (squash), then record it:",
      `   diagram(action="merge", diagram_key="${diagramKey}", chunk_key="${chunkKey}", args={artifact: <merge commit>}).`,
      "You did not build this chunk. Never push anything but the merge itself.",
      NO_MCP,
    ].join("\n");
  }
  if (step === "supervise") {
    const lines = agenda.map((item) => `- ${JSON.stringify(item)}`);
    return [
      `Veynod asks this computer to look after the Loop (supervisor round, placement ${placementId}).`,
      `These things wait or went wrong on ${host}:`,
      ...(lines.length ? lines : ["- (nothing listed)"]),
      "For each: find out why (loop(action=\"agenda\"), loop(action=\"placements\"), list_tasks,",
      "get_task_tree, get_metrics, list_alerts, loop(action=\"hosts\")), then act:",
      "- a task or step stuck on a computer: loop(action=\"move\", placement_id, host_id optional);",
      "- a task that should not run as it is: loop(action=\"withdraw\", placement_id), then fix or split it;",
      "- split one that keeps failing into smaller tasks, cancel what is obsolete,",
      "  acknowledge an alert you resolved. Decide technical matters yourself.",
      "Only money, identity, legal, deleting data or outside commitments go to the owner, together,",
      "in one short message in plain words. Do not write code in this round.",
      NO_MCP,
    ].join("\n");
  }
  return [
    `Veynod placed task ${taskKey} on this computer (placement ${placementId}).${chunk}`,
    `1. Claim it on Veynod (${host}) with agent_claim_and_read("${taskKey}") and read what it asks.`,
    branch
      ? `2. Work in this directory. It is a fresh checkout on branch ${branch}; commit there and push only that branch.`
      : "2. Work in this directory.",
    "3. Run the project's checks, open a pull request if the task changes code,",
    "   then hand the task off for review with a brief (summary, key_metrics,",
    "   unverified_hypotheses, risk_flag) and owner_report in plain words.",
    "If you cannot finish, release the task with a reason so another computer can take it.",
  ].join("\n");
}

function spawnRun(command, args, { cwd, env, shell = false, signal }) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { cwd, env: { ...process.env, ...env }, shell, stdio: ["ignore", "inherit", "inherit"], windowsHide: true, signal });
    child.on("error", (error) => resolve({ ok: false, reason: error.message }));
    child.on("close", (code) => resolve(code === 0 ? { ok: true } : { ok: false, reason: `exited ${code}` }));
  });
}

/**
 * The only MCP server a runner session gets: Veynod with the runner's own key.
 * The session must claim as the host's agent (the placement assigns the task to
 * it), so whatever Veynod key `init` wrote into the user's configuration must
 * not be the one it uses.
 */
export function veynodMcpServer({ host, key }) {
  return { name: "veynod", type: "http", url: `${host.replace(/\/$/, "")}/mcp/`,
    headers: { Authorization: `Bearer ${key}` } };
}

/** The same server in Claude Code's --mcp-config file format. */
export function claudeMcpConfig(pin) {
  const { name, type, url, headers } = veynodMcpServer(pin);
  return { mcpServers: { [name]: { type, url, headers } } };
}

/**
 * One way to run a prompt in a directory; resolves when the agent's turn ends.
 * `pin` ({host, key}) pins the session's MCP to Veynod with that key: through OAR
 * with `mcpServers` and `userConfiguration: "exclude"`, through Claude Code's CLI
 * with `--mcp-config <file> --strict-mcp-config` and an explicit permission mode
 * (nobody answers a prompt in an unattended run). Codex's CLI has no verified
 * equivalent, so a Codex run without OAR is reported as not pinned.
 */
export function adapterFor(runtime, { exec = "", importer = (spec) => import(spec), spawner = spawnRun,
  permissionMode = "bypassPermissions" } = {}) {
  if (runtime === "custom") {
    if (!exec) throw new Error("--runtime custom needs --exec CMD");
    return {
      id: "custom",
      run: ({ cwd, prompt, env }) => spawner(exec, [], { cwd, shell: true, env: { ...env, VEYNOD_PROMPT: prompt } }),
    };
  }
  if (!RUNTIME_IDS.includes(runtime)) throw new Error(`unknown runtime ${runtime} (claude, codex or custom)`);
  return {
    id: runtime,
    async run({ cwd, prompt, env, pin, configDir }) {
      const oar = await oarRuntime(runtime, importer);
      if (oar) {
        try {
          const { promptAndWait } = await importer("@botiverse/oar/observe");
          const installation = await oar.installation();
          if (installation?.kind === "available") {
            const options = { cwd, env };
            if (pin) Object.assign(options, { mcpServers: [veynodMcpServer(pin)], userConfiguration: "exclude" });
            const session = await oar.session(installation, options);
            try {
              const result = await promptAndWait(session, prompt);
              if (result.kind === "rejected") return { ok: false, reason: result.reason };
              return { ok: result.kind === "ended", reason: result.kind === "ended" ? undefined : `interrupted by ${result.by}` };
            } finally {
              await session.dispose().catch(() => {});
            }
          }
        } catch (error) {
          return { ok: false, reason: error instanceof Error ? error.message : String(error) };
        }
      }
      if (runtime !== "claude") return spawner(runtime, ["exec", prompt], { cwd, env });
      const args = ["-p", prompt, "--permission-mode", permissionMode];
      let file = null;
      if (pin) {
        mkdirSync(configDir, { recursive: true, mode: 0o700 });
        file = join(configDir, `mcp-${process.pid}-${Date.now()}.json`);
        writeFileSync(file, JSON.stringify(claudeMcpConfig(pin)), { mode: 0o600 });
        args.push("--mcp-config", file, "--strict-mcp-config");
      }
      try {
        return await spawner("claude", args, { cwd, env });
      } finally {
        if (file) rmSync(file, { force: true });
      }
    },
  };
}

/** Whether a run on this adapter claims as the runner's own agent. */
export async function pinsIdentity(runtime, { importer = (spec) => import(spec) } = {}) {
  if (runtime === "custom" || runtime === "claude") return true;
  return Boolean(await oarRuntime(runtime, importer));
}

// ------------------------------------------------------------------ worktrees

const repoSlug = (repo) => repo.replace(/[^A-Za-z0-9_.-]+/g, "__");

const PRE_PUSH = `#!/bin/sh
# Veynod runner: a task's worktree pushes only its own branch.
branch=$(git rev-parse --abbrev-ref HEAD)
while read -r local_ref local_sha remote_ref remote_sha; do
  if [ "$remote_ref" != "refs/heads/$branch" ]; then
    echo "veynod runner: this worktree may only push $branch (refused $remote_ref)" >&2
    exit 1
  fi
done
exit 0
`;

function directoryBytes(path) {
  let total = 0;
  let stack = [path];
  while (stack.length) {
    const current = stack.pop();
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = join(current, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else {
        try {
          total += lstatSync(full).size;
        } catch {
          // vanished
        }
      }
    }
  }
  return total;
}

/**
 * Per-task git worktrees from a cached bare clone of each repository. Pushes
 * are limited to the task's branch by a pre-push hook; finished worktrees are
 * removed, and the oldest finished ones go first when the disk cap is reached.
 */
export class Worktrees {
  constructor({ root, repos = [], maxBytes = 5 * 1024 ** 3, gitBase = "https://github.com/", git = execFileSync }) {
    this.root = root;
    this.allowed = new Set(repos.map((repo) => repo.toLowerCase()));
    this.maxBytes = maxBytes;
    this.gitBase = gitBase.endsWith("/") ? gitBase : `${gitBase}/`;
    this.git = (args, cwd) => String(git("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })).trim();
  }

  cache(repo) {
    return join(this.root, "repos", `${repoSlug(repo)}.git`);
  }

  workdir(taskKey) {
    return join(this.root, "work", repoSlug(taskKey));
  }

  allows(repo) {
    return this.allowed.has(String(repo).toLowerCase());
  }

  /** A fresh worktree on branch veynod/<task> from the repo's default branch. */
  prepare(taskKey, repo, busy = new Set()) {
    if (!this.allows(repo)) {
      throw Object.assign(new Error(`repository ${repo} is not on this runner's list`), { code: "repo_not_allowed" });
    }
    this.makeRoom(busy);
    const cache = this.cache(repo);
    mkdirSync(join(this.root, "repos"), { recursive: true });
    mkdirSync(join(this.root, "work"), { recursive: true });
    if (!existsSync(cache)) {
      this.git(["clone", "--bare", "--quiet", `${this.gitBase}${repo}.git`, cache]);
      writeFileSync(join(cache, "hooks", "pre-push"), PRE_PUSH);
      chmodSync(join(cache, "hooks", "pre-push"), 0o755);
    }
    this.git(["fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"], cache);
    const head = this.git(["symbolic-ref", "--short", "HEAD"], cache);
    const branch = `veynod/${taskKey}`;
    const dir = this.workdir(taskKey);
    if (existsSync(dir)) this.remove(taskKey, repo);
    this.git(["worktree", "add", "--quiet", "-B", branch, dir, `refs/remotes/origin/${head}`], cache);
    return { dir, branch, base: head };
  }

  remove(taskKey, repo) {
    const dir = this.workdir(taskKey);
    if (repo && existsSync(this.cache(repo))) {
      try {
        this.git(["worktree", "remove", "--force", dir], this.cache(repo));
      } catch {
        // not registered: fall back to deleting the directory
      }
    }
    rmSync(dir, { recursive: true, force: true });
    for (const name of existsSync(join(this.root, "repos")) ? readdirSync(join(this.root, "repos")) : []) {
      try {
        this.git(["worktree", "prune"], join(this.root, "repos", name));
      } catch {
        // a broken cache is recloned next time
      }
    }
  }

  /** Over the cap: drop finished worktrees, oldest first; refuse if still over. */
  makeRoom(busy) {
    const work = join(this.root, "work");
    if (!existsSync(work) || directoryBytes(work) <= this.maxBytes) return;
    const idle = readdirSync(work)
      .filter((name) => !busy.has(name))
      .map((name) => ({ name, at: lstatSync(join(work, name)).mtimeMs }))
      .sort((a, b) => a.at - b.at);
    for (const { name } of idle) {
      rmSync(join(work, name), { recursive: true, force: true });
      if (directoryBytes(work) <= this.maxBytes) break;
    }
    for (const name of existsSync(join(this.root, "repos")) ? readdirSync(join(this.root, "repos")) : []) {
      try {
        this.git(["worktree", "prune"], join(this.root, "repos", name));
      } catch {
        // ignore
      }
    }
    if (directoryBytes(work) > this.maxBytes) {
      throw Object.assign(new Error("the runner's disk cap is reached by running tasks"), { code: "disk_full" });
    }
  }
}

// ------------------------------------------------------------------ host

const OS = { linux: "linux", darwin: "darwin", win32: "windows", freebsd: "freebsd" };

export function hostReport({ runtimes, slots, repos, version }) {
  return {
    os: OS[platform()] ?? "other",
    arch: osArch(),
    hostname: hostname(),
    runner_version: version,
    runtimes,
    max_concurrency: slots,
    repos,
  };
}

async function callApi(fetchImpl, host, key, path, { method = "GET", body } = {}) {
  const response = await fetchImpl(`${host}${path}`, {
    method,
    headers: { Authorization: `Bearer ${key}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(20_000),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw Object.assign(new Error(data?.error?.message || `${path} answered ${response.status}`),
      { status: response.status, code: data?.error?.code });
  }
  return data;
}

/**
 * The runner loop. `follow(options, say)` is the CLI's stream follower; it calls
 * options.onPoke for every poke. Resolves when the follower ends.
 */
export async function runRunner(options, {
  follow,
  version,
  say = () => {},
  fetchImpl = fetch,
  importer = (spec) => import(spec),
  spawner,
  every = RUNNER_HEARTBEAT_MS,
  leaseEvery = LEASE_KEEP_MS,
  sleepMs = 2_000,
  stateDir = process.env.VEYNOD_STATE_DIR || join(homedir(), ".veynod"),
} = {}) {
  const slots = Math.max(1, Number(options.slots) || 1);
  const repos = options.repos || [];
  const adapter = adapterFor(options.runtime || "claude", { exec: options.exec, importer,
    ...(spawner ? { spawner } : {}), ...(options.permissionMode ? { permissionMode: options.permissionMode } : {}) });
  const pin = { host: options.host, key: options.key };
  const configDir = join(stateDir, "run");
  const worktrees = new Worktrees({
    root: join(stateDir, "runner"),
    repos,
    maxBytes: (Number(options.diskMb) || 5120) * 1024 * 1024,
    gitBase: options.gitBase || "https://github.com/",
  });
  const receipts = join(stateDir, "runner-receipts.jsonl");
  mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  const receipt = (fields) => appendFileSync(receipts, `${JSON.stringify({ at: new Date().toISOString(), ...fields })}\n`);

  let runtimes = options.runtime === "custom"
    ? [{ id: "custom", available: true, via: "exec" }]
    : await inventory({ importer });
  const mcpPinning = await pinsIdentity(adapter.id, { importer });
  if (!mcpPinning) say(`warning: ${adapter.id} without OAR cannot pin Veynod's MCP to this runner's key; install OAR (Node 24+)`);
  const hello = await callApi(fetchImpl, options.host, options.key, "/v1/runner/hello", {
    // Inside `host`: the request schema is strict, and servers that do not read the
    // report yet drop unknown host fields instead of refusing the hello.
    method: "POST", body: { host: { ...hostReport({ runtimes, slots, repos, version }),
      capabilities_report: { mcp_pinning: mcpPinning, runtime: adapter.id } } },
  });
  say(`runner ${hello.host_id} (${hello.agent_id ?? "unnamed"}) ready: ${slots} slot(s), ${runtimes.filter((item) => item.available).map((item) => item.id).join(", ") || "no runtime"}`);

  const running = new Map();
  const seen = new Set();
  let lastInventory = Date.now();
  const beat = async () => {
    try {
      if (options.runtime !== "custom" && Date.now() - lastInventory > 10 * 60_000) {
        runtimes = await inventory({ importer });
        lastInventory = Date.now();
      }
      await callApi(fetchImpl, options.host, options.key, "/v1/runner/heartbeat", {
        method: "POST", body: { running: [...running.keys()], runtimes },
      });
    } catch (error) {
      say(`heartbeat: ${error.message}`);
    }
  };
  const timer = setInterval(() => void beat(), every);
  timer.unref?.();

  const report = async (placementId, outcome, note) => {
    try {
      await callApi(fetchImpl, options.host, options.key,
        `/v1/runner/placements/${encodeURIComponent(placementId)}/report`, {
          method: "POST", body: { outcome, ...(note ? { note: String(note).slice(0, 500) } : {}) },
        });
    } catch (error) {
      if (error.status !== 404) say(`report ${placementId}: ${error.message}`);
    }
  };

  /**
   * Keeps the task's lease alive while the runtime works on it (the model is not
   * trusted to renew its own lease during a long tool run). Before the session
   * claims, and after it hands off, the renew is refused; that is expected.
   */
  const keepLease = (taskKey) => {
    const renew = () => callApi(fetchImpl, options.host, options.key,
      `/v1/tasks/${encodeURIComponent(taskKey)}/renew`, { method: "POST", body: {} }).catch(() => {});
    const timer = setInterval(() => void renew(), leaseEvery);
    timer.unref?.();
    return () => clearInterval(timer);
  };

  async function work({ taskKey, placementId, step, diagramKey, chunkKey }) {
    const slot = taskKey ?? placementId;
    let repo = null;
    let tree = null;
    let scratch = null;
    let stopLease = () => {};
    let result = { ok: false, reason: "not run" };
    try {
      // A replayed or late poke can name a placement the platform already moved on.
      const placement = await callApi(fetchImpl, options.host, options.key,
        `/v1/runner/placements/${encodeURIComponent(placementId)}`);
      if (!OPEN_PLACEMENT.has(placement.status)) {
        receipt({ task_key: taskKey, placement_id: placementId, step, stage: "skipped",
          reason: `placement is ${placement.status ?? "unknown"}` });
        say(`${slot} (${step}): skipped, placement is ${placement.status ?? "unknown"}`);
        return;
      }
      receipt({ task_key: taskKey, placement_id: placementId, step, stage: "accepted" });
      let agenda = [];
      if (step === "supervise") {
        agenda = placement.detail?.agenda ?? [];
      } else {
        const task = await callApi(fetchImpl, options.host, options.key, `/v1/tasks/${encodeURIComponent(taskKey)}`);
        repo = task.repo || null;
        // Reviews and merges get their own checkout so they never touch the builder's.
        const treeKey = step === "work" ? taskKey : `${step}-${taskKey}`;
        if (repo) tree = worktrees.prepare(treeKey, repo, new Set([...running.keys()].map(repoSlug)));
        if (tree) tree.key = treeKey;
      }
      // Work without a repository gets an empty directory of its own, never the
      // service's working directory.
      if (!tree) {
        scratch = join(stateDir, "runner", "scratch", repoSlug(`${step}-${slot}`));
        rmSync(scratch, { recursive: true, force: true });
        mkdirSync(scratch, { recursive: true });
      }
      const cwd = tree?.dir ?? scratch;
      const prompt = promptFor({ taskKey, placementId, branch: tree?.branch, host: options.host,
        step, diagramKey, chunkKey, agenda });
      const env = { VEYNOD_KEY: options.key, VEYNOD_HOST: options.host, VEYNOD_TASK_KEY: taskKey ?? "",
        VEYNOD_PLACEMENT_ID: placementId, VEYNOD_STEP: step };
      if (step === "work" && taskKey) stopLease = keepLease(taskKey);
      for (let attempt = 1; attempt <= WAKE_ATTEMPTS; attempt += 1) {
        receipt({ task_key: taskKey, placement_id: placementId, step, stage: "started", attempt, runtime: adapter.id });
        result = await adapter.run({ cwd, prompt, env, pin, configDir });
        if (result.ok) break;
        if (attempt < WAKE_ATTEMPTS) await sleep(Math.min(30_000, sleepMs * attempt));
      }
      receipt({ task_key: taskKey, placement_id: placementId, step, stage: result.ok ? "finished" : "failed", reason: result.reason });
      say(`${slot} (${step}): ${result.ok ? "finished" : `failed (${result.reason})`}`);
    } catch (error) {
      result = { ok: false, reason: error.code || error.message };
      receipt({ task_key: taskKey, placement_id: placementId, step, stage: "failed", reason: result.reason });
      say(`${slot} (${step}): ${error.message}`);
    } finally {
      stopLease();
      if (tree) {
        try {
          worktrees.remove(tree.key, repo);
        } catch {
          // cleaned on the next makeRoom
        }
      }
      if (scratch) rmSync(scratch, { recursive: true, force: true });
      running.delete(slot);
      void beat();
    }
    // Tell the platform how the step ended, so a failure moves on at once.
    await report(placementId, result.ok ? "finished" : "failed", result.reason);
  }

  try {
    return await follow({
      ...options,
      heartbeat: false,
      onPoke: async (poke) => {
        const placed = placementOf(poke);
        if (!placed || seen.has(placed.placementId)) return 0;
        seen.add(placed.placementId);
        const slot = placed.taskKey ?? placed.placementId;
        receipt({ task_key: placed.taskKey, placement_id: placed.placementId, step: placed.step, stage: "delivered" });
        if (running.size >= slots || running.has(slot)) {
          // Say so at once: the platform places it elsewhere instead of waiting
          // for the claim to time out on this computer.
          receipt({ task_key: placed.taskKey, placement_id: placed.placementId, step: placed.step, stage: "failed", reason: "no free slot" });
          await report(placed.placementId, "failed", "no_free_slot");
          return 0;
        }
        running.set(slot, work(placed));
        void beat();
        return 0;
      },
    }, say);
  } finally {
    clearInterval(timer);
    await Promise.allSettled([...running.values()]);
    await beat(); // leave the board knowing this computer runs nothing
  }
}
