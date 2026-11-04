// veynod runner: how this computer runs one placement (full-managed Loop).
//
// It only connects out, through the nerve messenger (veynod-messenger.mjs),
// which says hello with what the machine can do (system, Claude Code / Codex
// with versions, models and quota, slots, repos), heartbeats every 30 s and
// holds the computer's socket. Each `task.poke` on that socket becomes one
// worker process running `runPlacement`: a task with a repository runs in a
// Kernel worktree lease and is submitted through submit_patch (gate + merge);
// work without one runs in a scratch directory. It never claims for the
// agent: if the wake fails three times the placement is reported failed and
// the scheduler moves it on.
//
// Runtimes go through OAR (@botiverse/oar, Node 24+) when it is installed, else
// through the runtime's own CLI (`claude -p`, `codex exec`); `--runtime custom
// --exec CMD` runs any command (tests, other agents). Every wake leaves
// receipts in ~/.veynod/runner-receipts.jsonl: delivered, accepted, started,
// then finished or failed.

import { execFileSync, spawn } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { arch as osArch, homedir, hostname, platform } from "node:os";
import { join, resolve, sep, win32 } from "node:path";

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

/**
 * The script an npm `.cmd` shim runs (cmd-shim writes `"%dp0%\node_modules\...\cli.js" %*`),
 * or null for any other `.cmd`.
 */
export function shimTarget(text, dir) {
  const match = String(text).match(/"%~?dp0%?\\([^"]+\.[cm]?js)"/i);
  return match ? win32.join(dir, match[1]) : null;
}

/**
 * How to start a command by name without a shell: {command, prefix}, or null
 * when it is not on PATH. On Windows npm installs `claude` and `codex` as `.cmd`
 * shims, which Node cannot start without cmd.exe, and cmd.exe would re-read a
 * prompt's quotes and metacharacters; so a shim's script runs with this Node.
 */
export function resolveCommand(name, { platform = process.platform, env = process.env,
  exists = existsSync, read = (path) => readFileSync(path, "utf8"), execPath = process.execPath } = {}) {
  if (platform !== "win32") return { command: name, prefix: [] };
  const dirs = String(env.PATH ?? env.Path ?? "").split(";").filter(Boolean);
  const exts = String(env.PATHEXT || ".COM;.EXE;.BAT;.CMD").split(";").map((ext) => ext.toLowerCase())
    .filter(Boolean);
  for (const dir of dirs) {
    for (const ext of exts) {
      const file = win32.join(dir, `${name}${ext}`);
      if (!exists(file)) continue;
      if (ext === ".exe" || ext === ".com") return { command: file, prefix: [] };
      if (ext === ".cmd" || ext === ".bat") {
        const script = shimTarget(read(file), dir);
        if (script) return { command: execPath, prefix: [script] };
      }
    }
  }
  return null;
}

/** Whether `name --version` runs here (Windows npm shims included). */
export function commandWorks(name, { run = execFileSync, resolve = resolveCommand } = {}) {
  const target = resolve(name);
  if (!target) return null;
  try {
    return String(run(target.command, [...target.prefix, "--version"],
      { encoding: "utf8", timeout: 10_000, stdio: ["ignore", "pipe", "ignore"] })).trim();
  } catch {
    return null;
  }
}

/**
 * The runtime to use: the one asked for when it is ready, else (with
 * `fallback`, for a hub whose saved choice may predate what is installed) the
 * one that is; with nothing asked, Claude Code, then Codex. {runtime, note} or
 * {runtime: null, reason}.
 */
export function chooseRuntime(wanted, runtimes, { fallback = false } = {}) {
  const ready = RUNTIME_IDS.filter((id) => runtimes.some((item) => item.id === id && item.available));
  if (wanted === "custom") return { runtime: "custom" };
  if (wanted && ready.includes(wanted)) return { runtime: wanted };
  if (wanted && !fallback) return { runtime: null, reason: `${RUNTIME_NAMES[wanted]} is not installed on this computer (or not on PATH).` };
  if (ready.length === 0) {
    return { runtime: null, reason: "No coding agent found on this computer: neither Claude Code nor Codex is on PATH." };
  }
  if (wanted) return { runtime: ready[0], note: `${RUNTIME_NAMES[wanted]} is not installed here; using ${RUNTIME_NAMES[ready[0]]}.` };
  return { runtime: ready[0] };
}

export const RUNTIME_NAMES = { claude: "Claude Code", codex: "Codex" };

/** What to type to install and sign in to a runtime (or both, with none named). */
export function installSteps(runtime, { platform = process.platform } = {}) {
  const steps = {
    codex: ["Codex:        npm install -g @openai/codex", "              then: codex login"],
    claude: ["Claude Code:  npm install -g @anthropic-ai/claude-code", "              then: claude   (sign in when it asks)"],
  };
  const lines = runtime ? [...steps[runtime]] : [...steps.claude, ...steps.codex];
  lines.push("The desktop apps do not count: the runner starts the command-line tool.");
  if (platform === "win32") lines.push("Open a new terminal after installing, so it is on PATH.");
  return lines;
}

/** What runtimes this computer has, without account emails or tokens. */
export async function inventory({ importer = (spec) => import(spec), run = execFileSync,
  resolve = resolveCommand } = {}) {
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
    const version = commandWorks(id, { run, resolve });
    if (version !== null) {
      found.push({ id, available: true, via: "cli", version: version.split("\n")[0].slice(0, 32) });
    } else {
      found.push({ id, available: false });
    }
  }
  return found;
}

const STEPS = new Set(["work", "review", "merge", "supervise"]);

/**
 * What a nerve poke asks of this computer: its placement id (the frame id and
 * the idempotency key), the step, and the task and diagram chunk it is about.
 */
export function placementOf(poke) {
  if (!poke || typeof poke !== "object") return null;
  const placementId = String(poke.placement_id || "");
  if (!PLACEMENT.test(placementId)) return null;
  const step = STEPS.has(poke.step) ? poke.step : "work";
  const key = poke.task_key || null;
  if (!key && step !== "supervise") return null;
  return {
    taskKey: step === "supervise" ? null : key,
    placementId,
    step,
    ...(poke.diagram_key && poke.chunk_key
      ? { diagramKey: String(poke.diagram_key), chunkKey: String(poke.chunk_key) } : {}),
  };
}

// Agents use Veynod's MCP tools; REST is the fallback for one that has none.
const NO_MCP = "Without Veynod's MCP tools, use the REST API instead: diagram actions are POST "
  + "/v1/diagrams/<key>/chunks/<chunk>/<gate|queue-check|merge>, loop actions are under /v1/runner "
  + "(agenda, placements/<id>/move, placements/<id>/withdraw).";

export function promptFor({ taskKey, placementId, host, step = "work", diagramKey, chunkKey, agenda = [] }) {
  // Code never reaches these prompts: a task with a repository is a Kernel
  // task (kernelPrompt), reviewed by verification_gate and merged by
  // merge_coordinator. These are the steps for work without a repository.
  const chunk = diagramKey ? ` It is chunk ${chunkKey} of diagram ${diagramKey}.` : "";
  if (step === "review") {
    return [
      `Veynod asks this computer to review task ${taskKey} (placement ${placementId}).${chunk}`,
      `1. Read it on Veynod (${host}) with get_audit_view("${taskKey}"): its acceptance, the brief and what it delivered.`,
      "2. Check what it delivered against every acceptance item.",
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
      `1. Read the chunk on Veynod (${host}): diagram(action="get", diagram_key="${diagramKey}") and what its task delivered.`,
      "2. If the chunk waits in the merge queue, run its checks and report",
      `   diagram(action="queue_check", diagram_key="${diagramKey}", chunk_key="${chunkKey}", args={result: "pass"})`,
      "   or test_failed with the evidence; stop there if it is not a pass.",
      `3. Record the delivery: diagram(action="merge", diagram_key="${diagramKey}", chunk_key="${chunkKey}", args={artifact: <document or link>}).`,
      "You did not build this chunk. Do not change what it delivered.",
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
    "2. Work in this directory.",
    "3. Run the checks the task names, then hand the task off for review with a brief (summary, key_metrics,",
    "   unverified_hypotheses, risk_flag) and owner_report in plain words.",
    "If you cannot finish, release the task with a reason so another computer can take it.",
  ].join("\n");
}

export function isInside(child, parent) {
  if (!child || !parent) return false;
  const root = resolve(parent);
  const path = resolve(child);
  return path === root || path.startsWith(root.endsWith(sep) ? root : root + sep);
}

/**
 * Where an agent may write. Kernel mode refuses the host shared checkout and
 * refuses to start without a worktree lease and a write-set lock.
 */
export function codingWorkspace({ lease, scratch, hostCheckout }) {
  const writeSet = Array.isArray(lease?.write_set) ? lease.write_set.filter(Boolean) : [];
  const held = lease?.leased === true || lease?.lease === "held";
  if (!lease?.kernel_task_id || !held || writeSet.length === 0) {
    throw Object.assign(new Error(
      "Kernel worktree lease and write-set lock are required; refusing to edit the host checkout",
    ), { code: "kernel_lease_required" });
  }
  if (hostCheckout && scratch && isInside(scratch, hostCheckout)) {
    throw Object.assign(new Error(
      "refusing to mutate the host shared checkout; the Kernel worktree pool owns this task",
    ), { code: "shared_checkout_blocked" });
  }
  return { dir: scratch, branch: null, kernel: true, writeSet };
}

/**
 * slots=1 does not keep a reviewer process. The gate replaces it. A revision
 * turn runs only after the gate fails; escalation stops for a person.
 */
export function reviewAction({ step, gate }) {
  if (step === "merge") {
    if (gate?.merged) {
      return { spawn: null, outcome: "finished", note: "merge_coordinator merged" };
    }
    return { spawn: null, outcome: "failed", note: "merge_coordinator has not merged" };
  }
  if (gate?.needs_human || gate?.gate === "escalated") {
    return { spawn: null, outcome: "failed", note: "needs_human" };
  }
  if (gate?.merged || gate?.gate === "pass") {
    return { spawn: null, outcome: "finished", note: "verification_gate passed" };
  }
  if (gate?.gate === "fail" || gate?.status === "gate_failed") {
    return { spawn: "revision", outcome: null, note: "verification_gate failed" };
  }
  return { spawn: null, outcome: "failed", note: "gate_pending" };
}

export function kernelPrompt({ taskKey, placementId, host, writeSet, revision = false, evidence = "" }) {
  const locks = (writeSet || []).join(", ") || "(none)";
  if (revision) {
    return [
      `The Kernel verification_gate failed for task ${taskKey} (placement ${placementId}).`,
      evidence ? `Evidence: ${evidence}` : "Read the gate evidence on the task before changing the diff.",
      `The write-set lock is ${locks}. Revise only inside it.`,
      `Submit again with POST ${host}/v1/tasks/${taskKey}/kernel/submit`,
      `body {git_diff, touched_symbols, local_test_passed: true}. That is submit_patch.`,
      "verification_gate (typecheck, unit tests, AST lock) and merge_coordinator run on the Kernel.",
      "Do not edit the host shared checkout, do not push a branch, and do not open a pull request.",
    ].join("\n");
  }
  return [
    `Veynod placed task ${taskKey} on this computer (placement ${placementId}).`,
    "Delivery is the Veynod Kernel. Do not edit the host shared checkout and do not push a branch.",
    `1. Claim it on Veynod (${host}) with agent_claim_and_read("${taskKey}") and read what it asks.`,
    `2. A worktree lease and write-set lock are already held: ${locks}.`,
    "   Read code through the Kernel. The six primitives are pull_task_bundle, submit_patch,",
    "   request_task_split, escalate_contract_defect, inject_dependency_task, query_symbol_signature.",
    "3. Produce a unified diff that stays inside the write set.",
    `4. Submit only with POST ${host}/v1/tasks/${taskKey}/kernel/submit`,
    "   {git_diff, touched_symbols, local_test_passed: true}. The product API calls submit_patch.",
    "   verification_gate then merge_coordinator record the result on this same task.",
    "If the contract is wrong, escalate_contract_defect instead of editing outside the write set.",
    "If you cannot finish, release the task with a reason so another computer can take it.",
  ].join("\n");
}

export function publicLease(lease, taskKey) {
  if (!lease?.kernel_task_id) return null;
  const writeSet = Array.isArray(lease.write_set) ? lease.write_set.slice(0, 8) : [];
  return {
    task_key: taskKey || lease.task_key || "",
    kernel_task_id: String(lease.kernel_task_id),
    worktree: lease.worktree || (lease.leased ? "leased" : "none"),
    lease: lease.lease || (lease.leased ? "held" : "none"),
    ...(lease.gate ? { gate: lease.gate } : {}),
    ...(writeSet.length ? { write_set: writeSet } : {}),
  };
}

/** Poll until the gate is no longer pending, or the wait elapses. */
/**
 * The only cwd a kernel coding task may use: a worktree_pool lease directory
 * (`.../lease/<task-hash>`), never the shared repository root.
 */
export function resolveLeaseCwd(lease, { reposDir = null, hostCheckout = null, exists = existsSync } = {}) {
  const rel = String(lease?.worktree_rel || "");
  const anchor = String(lease?.worktree_anchor || "");
  const given = String(lease?.worktree_path || "");
  const candidates = [];
  if (given) candidates.push(given);
  if (reposDir && anchor && given.startsWith(anchor)) {
    candidates.push(join(reposDir, given.slice(anchor.length)));
  }
  if (reposDir && rel) candidates.push(join(reposDir, rel));
  let found = null;
  for (const candidate of candidates) {
    if (candidate && exists(candidate)) {
      found = resolve(candidate);
      break;
    }
  }
  if (!found) {
    throw Object.assign(new Error(
      `Kernel worktree_pool path is not on this host (${given || "missing"}). Refusing to edit the shared checkout.`,
    ), { code: "worktree_missing" });
  }
  const parts = found.split(sep);
  if (!parts.includes("lease") || parts.at(-1) === "lease") {
    throw Object.assign(new Error(`cwd is not a worktree_pool lease directory: ${found}`), { code: "worktree_not_isolated" });
  }
  if (rel && !rel.includes("lease/")) {
    throw Object.assign(new Error(`worktree_rel is not a lease path: ${rel}`), { code: "worktree_not_isolated" });
  }
  if (hostCheckout && resolve(hostCheckout) === found) {
    throw Object.assign(new Error(`refusing bare edit of the host checkout: ${found}`), { code: "shared_checkout_blocked" });
  }
  return found;
}

/** Greppable lines for submission_service → verification_gate → merge_coordinator. */
export function pipelineLines({ submission, gate }) {
  const lines = [];
  if (submission?.submission_id) {
    lines.push(`submission_service id=${submission.submission_id} status=${submission.status || "accepted"}`);
  }
  const results = gate?.gate_results || [];
  if (results.length) {
    lines.push(`verification_gate ${results.map((item) => `${item.stage}=${item.passed === true ? "PASS" : "FAIL"}`).join(" ")}`);
  } else if (gate?.gate) {
    lines.push(`verification_gate ${gate.gate}`);
  }
  if (gate?.merged || gate?.merge_sha) {
    lines.push(`merge_coordinator sha=${gate.merge_sha || ""} mode=${gate.merge_mode || "fast-forward"}`);
  }
  return lines;
}

export function combinedMcpConfig({ product, kernelUrl, kernelToken }) {
  const mcpServers = {};
  if (product) Object.assign(mcpServers, claudeMcpConfig(product).mcpServers);
  if (kernelUrl) {
    const server = {
      type: "http",
      url: `${String(kernelUrl).replace(/\/$/, "")}/mcp`,
    };
    if (kernelToken) server.headers = { Authorization: `Bearer ${kernelToken}` };
    // The Kernel MCP server publishes the six syscalls (pull_task_bundle,
    // submit_patch, request_task_split, escalate_contract_defect,
    // inject_dependency_task, query_symbol_signature).
    mcpServers["veynod-kernel"] = server;
  }
  return { mcpServers };
}

/**
 * Submit the worktree's diff. The agent may already have submitted its own
 * (the prompt tells it how): a gate still working on that one answers 409
 * submission_in_progress, which is not a failure of this placement. Reading
 * it as one reported "failed", moved the task and started the work again.
 */
export async function submitKernelDiff(fetchImpl, options, taskKey, diff) {
  try {
    return await callApi(fetchImpl, options.host, options.key,
      `/v1/tasks/${encodeURIComponent(taskKey)}/kernel/submit`, {
        method: "POST",
        body: { git_diff: diff, touched_symbols: touchedSymbolsFromDiff(diff), local_test_passed: true },
      });
  } catch (error) {
    if (error.code === "submission_in_progress") return { status: "in_progress", already_submitted: true };
    throw error;
  }
}

function cachedDiff(cwd) {
  execFileSync("git", ["add", "-A"], { cwd, stdio: ["ignore", "pipe", "pipe"] });
  return String(execFileSync("git", ["diff", "--cached", "--binary", "HEAD"], { cwd, encoding: "utf8" }));
}

/**
 * Symbols the diff actually touched, as `path::Class::method` (or the file when
 * the change is module-level).  Sending the lease write set instead made the
 * Kernel's submission-time AST check vacuous: it compared the lease with
 * itself.  Parsing the hunks keeps that layer meaningful; the gate still
 * re-derives the symbols independently.
 */
const NL = String.fromCharCode(10);

export function touchedSymbolsFromDiff(diff) {
  const out = new Set();
  let file = "";
  let context = "";
  for (const line of String(diff || "").split(NL)) {
    const header = /^\+\+\+ b\/(.+)$/.exec(line);
    if (header) {
      file = header[1].trim();
      context = "";
      out.add(file);
      continue;
    }
    if (!file) continue;
    const def = /^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)/.exec(line);
    if (def) {
      const name = def[1];
      const isClass = /class\s/.test(line);
      const symbol = context && !isClass ? `${file}::${context}::${name}` : `${file}::${name}`;
      out.add(symbol);
      if (isClass) context = name;
      continue;
    }
    const indent = /^([-+ ])(\s*)(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)/.exec(line);
    if (indent && indent[3]) out.add(`${file}::${indent[3]}`);
  }
  return [...out];
}

export async function settleGate(read, { waitMs = 0, intervalMs = 250, sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) } = {}) {
  const started = Date.now();
  let gate = await read();
  const pending = (item) => item && (item.gate === "pending" || item.status === "gate" || item.gate === "absent" && item.submission_id);
  while (pending(gate) && Date.now() - started < waitMs) {
    await sleep(intervalMs);
    gate = await read();
  }
  return gate;
}

function spawnRun(command, args, { cwd, env, shell = false, signal }) {
  // Resolved here so a Windows npm shim starts without a shell; custom --exec keeps its shell.
  const target = shell ? { command, prefix: [] } : resolveCommand(command);
  if (!target) return Promise.resolve({ ok: false, reason: `${command} is not on PATH` });
  return new Promise((resolve) => {
    const child = spawn(target.command, [...target.prefix, ...args], { cwd, env: { ...process.env, ...env }, shell, stdio: ["ignore", "inherit", "inherit"], windowsHide: true, signal });
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
    async run({ cwd, prompt, env, pin, configDir, mcpConfig }) {
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
      const mounted = mcpConfig || (pin ? claudeMcpConfig(pin) : null);
      if (mounted) {
        mkdirSync(configDir, { recursive: true, mode: 0o700 });
        file = join(configDir, `mcp-${process.pid}-${Date.now()}.json`);
        writeFileSync(file, JSON.stringify(mounted), { mode: 0o600 });
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

const repoSlug = (repo) => repo.replace(/[^A-Za-z0-9_.-]+/g, "__");

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
 * What runs placements on this computer: inventory, the runtime adapter, the
 * host record (hello + heartbeat) and `work(placement)`.
 *
 * It has no delivery loop of its own. The nerve messenger (veynod-messenger.mjs)
 * owns the socket, the receipts and the slots; each poke becomes one worker
 * process that runs `runPlacement`, which calls `work` once.
 */
export async function createRunner(options, {
  version,
  say = () => {},
  fetchImpl = fetch,
  importer = (spec) => import(spec),
  spawner,
  leaseEvery = LEASE_KEEP_MS,
  sleepMs = 2_000,
  stateDir = process.env.VEYNOD_STATE_DIR || join(homedir(), ".veynod"),
  takeInventory = () => inventory({ importer }),
} = {}) {
  const slots = Math.max(1, Number(options.slots) || 1);
  const repos = options.repos || [];
  let runtimes = options.runtime === "custom"
    ? [{ id: "custom", available: true, via: "exec" }]
    : await takeInventory();
  // A saved choice can predate what is installed: use what is here, and say so.
  const picked = chooseRuntime(options.runtime || "", runtimes, { fallback: true });
  if (picked.note) say(`warning: ${picked.note}`);
  if (!picked.runtime) {
    say(`warning: ${picked.reason} The board shows this computer without a runtime until one is installed:`);
    for (const line of installSteps(null)) say(`  ${line}`);
  }
  const makeAdapter = (runtime) => adapterFor(runtime, { exec: options.exec, importer,
    ...(spawner ? { spawner } : {}), ...(options.permissionMode ? { permissionMode: options.permissionMode } : {}) });
  let adapter = makeAdapter(picked.runtime || options.runtime || "claude");
  const pin = { host: options.host, key: options.key };
  const configDir = join(stateDir, "run");
  const receipts = join(stateDir, "runner-receipts.jsonl");
  mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  const receipt = (fields) => appendFileSync(receipts, `${JSON.stringify({ at: new Date().toISOString(), ...fields })}\n`);

  const hostCheckout = options.hostCheckout || process.env.VEYNOD_HOST_CHECKOUT || null;
  const reposDir = options.reposDir || process.env.VEYNOD_REPOS_DIR || process.env.REPOS_DIR || "";
  const kernelUrl = options.kernelUrl || process.env.VEYNOD_KERNEL_URL || process.env.KERNEL_URL || "";
  const kernelToken = process.env.KERNEL_RUNTIME_TOKEN || process.env.VEYNOD_AGENT_TOKEN || "";
  const leaseBook = new Map();
  const reportFields = () => ({
    mcp_pinning: mcpPinning,
    runtime: adapter.id,
    delivery: "kernel",
    leases: [...leaseBook.values()],
  });
  const mcpPinning = await pinsIdentity(adapter.id, { importer });
  if (!mcpPinning) say(`warning: ${adapter.id} without OAR cannot pin Veynod's MCP to this runner's key; install OAR (Node 24+)`);
  // The host record: only the process that owns the computer's socket says
  // hello. A worker that did would overwrite the record with its own options.
  const hello = async () => {
    const answer = await callApi(fetchImpl, options.host, options.key, "/v1/runner/hello", {
      // Inside `host`: the request schema is strict, and servers that do not read the
      // report yet drop unknown host fields instead of refusing the hello.
      method: "POST", body: { host: { ...hostReport({ runtimes, slots, repos, version }),
        capabilities_report: reportFields() } },
    });
    say(`runner ${answer.host_id} (${answer.agent_id ?? "unnamed"}) ready: ${slots} slot(s), ${runtimes.filter((item) => item.available).map((item) => item.id).join(", ") || "no runtime"}`);
    return answer;
  };

  // Placements running on this computer, for the heartbeat. The messenger owns
  // this map (it starts and ends the workers); `work` never touches it, so a
  // worker process cannot rewrite the host record with its own view.
  const running = options.running instanceof Map ? options.running : new Map();
  let lastInventory = Date.now();
  const beat = async () => {
    try {
      if (options.runtime !== "custom" && Date.now() - lastInventory > 10 * 60_000) {
        runtimes = await takeInventory();
        lastInventory = Date.now();
        // Installed (or removed) while the hub runs: the next task uses what is here now.
        const now = chooseRuntime(options.runtime || "", runtimes, { fallback: true });
        if (now.runtime && now.runtime !== adapter.id) {
          adapter = makeAdapter(now.runtime);
          say(`runtime is now ${now.runtime}`);
        }
      }
      await callApi(fetchImpl, options.host, options.key, "/v1/runner/heartbeat", {
        method: "POST", body: { running: [...running.keys()], runtimes, capabilities_report: reportFields() },
      });
    } catch (error) {
      say(`heartbeat: ${error.message}`);
    }
  };
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

  async function readGate(taskKey) {
    return callApi(fetchImpl, options.host, options.key,
      `/v1/tasks/${encodeURIComponent(taskKey)}/kernel/delivery`);
  }

  async function work({ taskKey, placementId, step, diagramKey, chunkKey }) {
    const slot = taskKey ?? placementId;
    let repo = null;
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
      let cwd;
      let prompt;
      let skipAgent = false;
      let task = null;
      if (step !== "supervise" && taskKey) {
        task = await callApi(fetchImpl, options.host, options.key, `/v1/tasks/${encodeURIComponent(taskKey)}`);
        repo = task.repo || null;
      }
      // Coding tasks go through the Kernel. A task with no repository never
      // touches a checkout, so it keeps an empty scratch directory.
      const kernelCoding = step !== "supervise" && Boolean(repo);
      if (kernelCoding && (step === "review" || step === "merge")) {
        // No second Claude. The gate and merge coordinator already ran,
        // unless the gate failed and this same worker must revise once.
        const gate = await readGate(taskKey);
        const action = reviewAction({ step, gate });
        for (const line of pipelineLines({ gate })) say(line);
        if (action.spawn !== "revision") {
          skipAgent = true;
          result = { ok: action.outcome === "finished", reason: action.note };
          receipt({ task_key: taskKey, placement_id: placementId, step, stage: result.ok ? "finished" : "failed",
            reason: action.note, review_twin: false });
          say(`${slot} (${step}): ${action.note} review_twin=absent`);
        }
      }
      if (kernelCoding && !skipAgent) {
        const lease = await callApi(fetchImpl, options.host, options.key,
          `/v1/tasks/${encodeURIComponent(taskKey)}/kernel/lease`, { method: "POST", body: {} });
        cwd = resolveLeaseCwd(lease, { reposDir, hostCheckout });
        const published = publicLease({ ...lease, task_key: taskKey }, taskKey);
        if (published) leaseBook.set(slot, published);
        say(`kernel worktree cwd=${cwd} pool=worktree_pool lease=${lease.lease || "held"} task=${lease.kernel_task_id}`);
        receipt({ task_key: taskKey, placement_id: placementId, step, stage: "cwd", cwd,
          pool: "worktree_pool", kernel_task_id: lease.kernel_task_id, review_twin: false });
        prompt = kernelPrompt({
          taskKey, placementId, host: options.host, writeSet: lease.write_set,
          revision: step === "review",
        });
      } else if (skipAgent) {
        cwd = null;
      } else if (step === "supervise") {
        agenda = placement.detail?.agenda ?? [];
      }
      if (!cwd && !skipAgent) {
        // Work without a repository gets an empty directory of its own, never the
        // service's working directory. Kernel coding tasks already have cwd.
        scratch = join(stateDir, "runner", "scratch", repoSlug(`${step}-${slot}`));
        rmSync(scratch, { recursive: true, force: true });
        mkdirSync(scratch, { recursive: true });
        cwd = scratch;
        prompt = promptFor({ taskKey, placementId, host: options.host,
          step, diagramKey, chunkKey, agenda });
      }
      const env = { VEYNOD_KEY: options.key, VEYNOD_HOST: options.host, VEYNOD_TASK_KEY: taskKey ?? "",
        VEYNOD_PLACEMENT_ID: placementId, VEYNOD_STEP: step, VEYNOD_CWD: cwd };
      const mcpConfig = kernelCoding ? combinedMcpConfig({
        product: pin, kernelUrl, kernelToken,
      }) : null;
      if (step === "work" && taskKey && !skipAgent) stopLease = keepLease(taskKey);
      if (!skipAgent) for (let attempt = 1; attempt <= WAKE_ATTEMPTS; attempt += 1) {
        receipt({ task_key: taskKey, placement_id: placementId, step, stage: "started", attempt, runtime: adapter.id, cwd });
        result = await adapter.run({ cwd, prompt, env, pin, configDir, mcpConfig });
        if (result.ok) break;
        if (attempt < WAKE_ATTEMPTS) await sleep(Math.min(30_000, sleepMs * attempt));
      }
      if (kernelCoding && taskKey && !skipAgent) {
        let diff = "";
        try {
          diff = cachedDiff(cwd);
        } catch (error) {
          result = { ok: false, reason: error.message };
        }
        if (result.ok && !String(diff).trim()) {
          result = { ok: false, reason: "no_kernel_submission" };
        } else if (result.ok) {
          const submitted = await submitKernelDiff(fetchImpl, options, taskKey, diff);
          const gate = await settleGate(() => readGate(taskKey), {
            waitMs: Number(options.gateWaitMs ?? process.env.VEYNOD_GATE_WAIT_MS ?? 0),
          });
          const lines = pipelineLines({ submission: submitted, gate });
          for (const line of lines) {
            say(line);
            receipt({ task_key: taskKey, placement_id: placementId, step, stage: "pipeline", line });
          }
          if (gate?.needs_human || gate?.gate === "escalated") {
            result = { ok: false, reason: "needs_human" };
          } else if (gate?.gate === "fail" || gate?.status === "gate_failed") {
            result = { ok: false, reason: "verification_gate failed" };
          } else if (gate?.merged || gate?.gate === "pass" || submitted?.submission_id
              || submitted?.already_submitted) {
            result = { ok: true, reason: gate?.merge_sha ? `merge_coordinator ${gate.merge_sha}` : "submitted_to_kernel_gate" };
          }
        }
      }
      receipt({ task_key: taskKey, placement_id: placementId, step, stage: result.ok ? "finished" : "failed", reason: result.reason, cwd });
      say(`${slot} (${step}): ${result.ok ? "finished" : `failed (${result.reason})`}`);
    } catch (error) {
      result = { ok: false, reason: error.code || error.message };
      receipt({ task_key: taskKey, placement_id: placementId, step, stage: "failed", reason: result.reason });
      say(`${slot} (${step}): ${error.message}`);
    } finally {
      stopLease();
      leaseBook.delete(slot);
      if (scratch) rmSync(scratch, { recursive: true, force: true });
    }
    // Tell the platform how the step ended, so a failure moves on at once.
    await report(placementId, result.ok ? "finished" : "failed", result.reason);
  }

  return {
    hello,
    beat,
    work,
    report,
    running,
    adapter: () => adapter,
  };
}

// ------------------------------------------------------------------ one task
/**
 * Run exactly one placement and exit (the messenger's worker process).
 *
 * The messenger owns the socket, both receipts and the host record; this
 * process does the work for one placement and reports how it ended.
 */
export async function runPlacement(options, poke, deps = {}) {
  const runner = await createRunner(options, deps);
  // The second receipt: this process is up and about to work the placement.
  if (process.send) process.send({ type: "IPC_ORIENTATION_READY", task_key: poke.task_key });
  const placed = placementOf(poke);
  if (!placed) throw new Error("the poke names no placement");
  await runner.work(placed);
}

/** `runner.mjs --placement ID [--task KEY] [--step STEP] [--agent AGENT]` */
export function onePokeFromArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (!flag.startsWith("--")) continue;
    args[flag.slice(2)] = argv[index + 1]?.startsWith("--") ? true : argv[++index];
  }
  if (!args.placement) {
    throw new Error("usage: runner.mjs --placement ID [--task KEY] [--step STEP]");
  }
  return {
    task_key: args.task && args.task !== true ? args.task : null,
    placement_id: args.placement,
    step: args.step || "work",
    agent_id: args.agent,
  };
}

/** The poke the messenger handed this worker: the full frame payload, else the arguments. */
export function workerPoke(argv, env = process.env) {
  const fromArgs = onePokeFromArgs(argv);
  try {
    const frame = JSON.parse(env.VEYNOD_TASK_POKE || "null");
    if (frame && frame.placement_id === fromArgs.placement_id) return { ...fromArgs, ...frame };
  } catch {
    // fall back to the arguments
  }
  return fromArgs;
}

/** The messenger's runner options for this worker (repos, runtime, delivery). */
export function workerRunnerOptions(env = process.env) {
  let given = {};
  try {
    given = JSON.parse(env.VEYNOD_RUNNER_OPTIONS || "{}") || {};
  } catch {
    given = {};
  }
  const out = {};
  if (Array.isArray(given.repos)) out.repos = given.repos.map(String);
  for (const field of ["runtime", "exec", "delivery", "gitBase", "reposDir"]) {
    if (typeof given[field] === "string" && given[field]) out[field] = given[field];
  }
  return out;
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"))) {
  const poke = workerPoke(process.argv.slice(2));
  const host = process.env.VEYNOD_HOST || "http://127.0.0.1:8000";
  const key = process.env.VEYNOD_AGENT_TOKEN || process.env.VEYNOD_KEY;
  if (!key) {
    process.stderr.write("runner.mjs needs VEYNOD_AGENT_TOKEN\n");
    process.exit(2);
  }
  runPlacement({ ...workerRunnerOptions(), host, key, agent: process.env.VEYNOD_AGENT_ID, slots: 1 }, poke)
    .then(() => process.exit(0))
    .catch((error) => {
      process.stderr.write(`runner failed: ${error.message}\n`);
      process.exit(1);
    });
}
