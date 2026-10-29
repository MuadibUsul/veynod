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

/** The task a placement poke is about, with its placement id (the idempotency key). */
export function placementOf(poke) {
  if (!poke || poke.kind !== "task.placed") return null;
  const key = poke.task_key || poke.doc_key;
  const match = PLACEMENT.exec(poke.note || "");
  if (!key || !match) return null;
  return { taskKey: key, placementId: match[0] };
}

export function promptFor({ taskKey, placementId, branch, host }) {
  return [
    `Veynod placed task ${taskKey} on this computer (placement ${placementId}).`,
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

/** One way to run a prompt in a directory; resolves when the agent's turn ends. */
export function adapterFor(runtime, { exec = "", importer = (spec) => import(spec) } = {}) {
  if (runtime === "custom") {
    if (!exec) throw new Error("--runtime custom needs --exec CMD");
    return {
      id: "custom",
      run: ({ cwd, prompt, env }) => spawnRun(exec, [], { cwd, shell: true, env: { ...env, VEYNOD_PROMPT: prompt } }),
    };
  }
  if (!RUNTIME_IDS.includes(runtime)) throw new Error(`unknown runtime ${runtime} (claude, codex or custom)`);
  return {
    id: runtime,
    async run({ cwd, prompt, env }) {
      const oar = await oarRuntime(runtime, importer);
      if (oar) {
        try {
          const { promptAndWait } = await importer("@botiverse/oar/observe");
          const installation = await oar.installation();
          if (installation?.kind === "available") {
            const session = await oar.session(installation, { cwd, env });
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
      const args = runtime === "claude" ? ["-p", prompt] : ["exec", prompt];
      return spawnRun(runtime, args, { cwd, env });
    },
  };
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
  every = RUNNER_HEARTBEAT_MS,
  stateDir = process.env.VEYNOD_STATE_DIR || join(homedir(), ".veynod"),
} = {}) {
  const slots = Math.max(1, Number(options.slots) || 1);
  const repos = options.repos || [];
  const adapter = adapterFor(options.runtime || "claude", { exec: options.exec, importer });
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
  const hello = await callApi(fetchImpl, options.host, options.key, "/v1/runner/hello", {
    method: "POST", body: { host: hostReport({ runtimes, slots, repos, version }) },
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

  async function work({ taskKey, placementId }) {
    receipt({ task_key: taskKey, placement_id: placementId, stage: "accepted" });
    let repo = null;
    let tree = null;
    try {
      const task = await callApi(fetchImpl, options.host, options.key, `/v1/tasks/${encodeURIComponent(taskKey)}`);
      repo = task.repo || null;
      if (repo) tree = worktrees.prepare(taskKey, repo, new Set([...running.keys()].map(repoSlug)));
      const cwd = tree?.dir ?? options.cwd ?? process.cwd();
      const prompt = promptFor({ taskKey, placementId, branch: tree?.branch, host: options.host });
      const env = { VEYNOD_KEY: options.key, VEYNOD_HOST: options.host, VEYNOD_TASK_KEY: taskKey, VEYNOD_PLACEMENT_ID: placementId };
      let result = { ok: false, reason: "not run" };
      for (let attempt = 1; attempt <= WAKE_ATTEMPTS; attempt += 1) {
        receipt({ task_key: taskKey, placement_id: placementId, stage: "started", attempt, runtime: adapter.id });
        result = await adapter.run({ cwd, prompt, env });
        if (result.ok) break;
        if (attempt < WAKE_ATTEMPTS) await sleep(Math.min(30_000, 2_000 * attempt));
      }
      receipt({ task_key: taskKey, placement_id: placementId, stage: result.ok ? "finished" : "failed", reason: result.reason });
      say(`${taskKey}: ${result.ok ? "finished" : `failed (${result.reason})`}`);
    } catch (error) {
      receipt({ task_key: taskKey, placement_id: placementId, stage: "failed", reason: error.code || error.message });
      say(`${taskKey}: ${error.message}`);
    } finally {
      if (tree) {
        try {
          worktrees.remove(taskKey, repo);
        } catch {
          // cleaned on the next makeRoom
        }
      }
      running.delete(taskKey);
      void beat();
    }
  }

  try {
    return await follow({
      ...options,
      heartbeat: false,
      onPoke: async (poke) => {
        const placed = placementOf(poke);
        if (!placed || seen.has(placed.placementId)) return 0;
        seen.add(placed.placementId);
        receipt({ task_key: placed.taskKey, placement_id: placed.placementId, stage: "delivered" });
        if (running.size >= slots || running.has(placed.taskKey)) {
          receipt({ task_key: placed.taskKey, placement_id: placed.placementId, stage: "failed", reason: "no free slot" });
          return 0;
        }
        running.set(placed.taskKey, work(placed));
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
