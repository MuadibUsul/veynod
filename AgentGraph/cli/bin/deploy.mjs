// The deploy computer: run one deploy placement (deploy gate v2).
//
// `veynod up --deploy --compose-dir DIR` puts a server's own computer on the
// board. It dials out over nerve like any computer and takes nothing but
// deploys. A deploy placement names the release (each service's image digest)
// and the plan (only the services whose digest changed). This module:
//
//   1. writes DIR/.veynod/deploy.env: VEYNOD_IMAGE_<SERVICE>=image@digest for
//      every service of the release (the compose file reads its images from it);
//   2. runs the local before-deploy hook, if this computer has one (migrations);
//   3. pulls and restarts only the changed compose services (`up -d --no-deps`);
//   4. waits for the health URL;
//   5. on failure, writes the previous images back and restarts the same
//      services (when the target rolls back), then reports how it ended.
//
// Commands run without a shell, with argument arrays: nothing the server sends
// is ever interpreted by a shell. Service names are validated by the server
// and again here.

import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join } from "node:path";

const NAME = /^[A-Za-z0-9_-]{1,64}$/;
const IMAGE = /^[A-Za-z0-9._\-/:]{1,300}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const OPEN = new Set(["offered", "claimed"]);
const LOG_LINES = 80;

/** VEYNOD_IMAGE_<SERVICE> for a service name: upper case, dashes as underscores. */
export function imageVar(service) {
  return `VEYNOD_IMAGE_${String(service).toUpperCase().replace(/-/g, "_")}`;
}

/** The env file a manifest becomes; a malformed entry is refused, never written. */
export function deployEnv(manifest = {}) {
  const lines = [];
  for (const [name, entry] of Object.entries(manifest).sort(([a], [b]) => a.localeCompare(b))) {
    if (!NAME.test(name) || !IMAGE.test(entry?.image ?? "") || !DIGEST.test(entry?.digest ?? "")) {
      throw new Error(`release entry ${name} is not an image and a sha256 digest`);
    }
    lines.push(`${imageVar(name)}=${entry.image}@${entry.digest}`);
  }
  return `${lines.join("\n")}\n`;
}

/** The compose services that run the given release services, in order, once each. */
export function composeServices(manifest = {}, services = []) {
  const out = [];
  for (const name of services) {
    const entry = manifest[name];
    if (!entry) continue;
    for (const compose of entry.compose?.length ? entry.compose : [name]) {
      if (!NAME.test(compose)) throw new Error(`compose service ${compose} is not a name`);
      if (!out.includes(compose)) out.push(compose);
    }
  }
  return out;
}

/** Where this computer deploys: from `veynod up --deploy` (saved in the config). */
export function deploySettings(given) {
  if (!given || typeof given !== "object" || typeof given.dir !== "string" || !isAbsolute(given.dir)) {
    return null;
  }
  return {
    dir: given.dir,
    file: typeof given.file === "string" && given.file ? given.file : "docker-compose.yml",
    before: typeof given.before === "string" && given.before ? given.before : "",
  };
}

/** Run a command without a shell; resolves with its exit code and last output lines. */
export function runCommand(command, args, { cwd, env = {}, timeoutMs = 15 * 60_000 } = {}) {
  return new Promise((resolve) => {
    let output = "";
    let child;
    try {
      child = spawn(command, args, { cwd, env: { ...process.env, ...env }, shell: false });
    } catch (error) {
      resolve({ code: 127, output: error.message });
      return;
    }
    const keep = (chunk) => {
      output = (output + chunk.toString()).slice(-20_000);
    };
    child.stdout?.on("data", keep);
    child.stderr?.on("data", keep);
    const timer = setTimeout(() => child.kill("SIGTERM"), timeoutMs);
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ code: 127, output: `${output}${error.message}` });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, output });
    });
  });
}

/** A hook is a command line split on spaces (no shell): `./scripts/migrate_all.sh`. */
export function hookArgs(line) {
  return String(line).trim().split(/\s+/).filter(Boolean);
}

async function waitHealthy(fetchImpl, url, timeoutS, sleep) {
  if (!url) return { ok: true, health: "not_checked" };
  const until = Date.now() + Math.max(5, Number(timeoutS) || 120) * 1000;
  let last = "no answer";
  while (Date.now() < until) {
    try {
      const response = await fetchImpl(url, { signal: AbortSignal.timeout(10_000) });
      if (response.ok) return { ok: true, health: "ok" };
      last = `HTTP ${response.status}`;
    } catch (error) {
      last = error.message;
    }
    await sleep(3000);
  }
  return { ok: false, health: "unhealthy", why: last };
}

function writeAtomic(path, content) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  writeFileSync(`${path}.tmp`, content, { mode: 0o600 });
  renameSync(`${path}.tmp`, path);
}

/**
 * Run the deploy placement `placementId` and report it. Never throws for a
 * deploy that fails: that is a report. Throws only when it cannot report.
 */
export async function runDeploy(options, placementId, {
  fetchImpl = fetch, run = runCommand, sleep = (ms) => new Promise((done) => setTimeout(done, ms)),
  say = () => {},
} = {}) {
  const api = async (path, body) => {
    const response = await fetchImpl(`${options.host}${path}`, {
      method: body ? "POST" : "GET",
      headers: { Authorization: `Bearer ${options.key}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(20_000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data?.error?.message || `${path} answered ${response.status}`);
    return data;
  };
  const log = [];
  const note = (line) => {
    log.push(String(line).slice(0, 400));
    say(`deploy ${placementId}: ${line}`);
  };
  const reportPath = `/v1/runner/placements/${encodeURIComponent(placementId)}/deploy-report`;
  const report = (status, extra = {}) => api(reportPath, { status, log: log.slice(-LOG_LINES), ...extra });

  const placement = await api(`/v1/runner/placements/${encodeURIComponent(placementId)}`);
  if (placement.step !== "deploy" || !OPEN.has(placement.status)) {
    say(`deploy ${placementId}: skipped, placement is ${placement.status}`);
    return { status: "skipped" };
  }
  const settings = deploySettings(options.deploy);
  if (!settings) {
    note("this computer has no deploy directory: run `veynod up --deploy --compose-dir DIR`");
    await report("failed", { note: "no deploy directory on the deploy computer" });
    return { status: "failed" };
  }
  const detail = placement.detail || {};
  const release = detail.release?.manifest || {};
  const previous = detail.previous?.manifest || {};
  const changed = (detail.plan?.changed || []).filter((name) => NAME.test(name));
  const envFile = join(settings.dir, ".veynod", "deploy.env");
  const compose = (args) => run("docker", ["compose", "-f", settings.file, "--env-file", envFile, ...args],
    { cwd: settings.dir });
  await report("running", { note: `deploying ${detail.release?.commit ?? "?"}: ${changed.join(", ") || "nothing"}` });

  let services;
  let previousEnv = null;
  try {
    services = composeServices(release, changed);
    writeAtomic(envFile, deployEnv({ ...previous, ...release }));
    // New services keep their image; the changed ones go back to what ran before.
    previousEnv = Object.keys(previous).length ? deployEnv({ ...release, ...previous }) : null;
  } catch (error) {
    note(error.message);
    await report("failed", { note: error.message });
    return { status: "failed" };
  }

  const step = async (label, command, args, extra = {}) => {
    note(`${label}: ${[command, ...args].join(" ")}`);
    const result = await run(command, args, { cwd: settings.dir, ...extra });
    for (const line of String(result.output || "").trim().split("\n").slice(-5)) if (line) note(`  ${line}`);
    return result.code === 0;
  };

  let failure = null;
  if (settings.before) {
    const [command, ...args] = hookArgs(settings.before);
    if (!await step("before deploy", command, args, { env: { VEYNOD_DEPLOY_ENV: envFile } })) {
      failure = "the before-deploy hook failed";
    }
  }
  if (!failure && services.length) {
    if (!await step("pull", "docker", ["compose", "-f", settings.file, "--env-file", envFile, "pull", ...services])) {
      failure = "docker compose pull failed";
    } else if (!await step("restart", "docker", ["compose", "-f", settings.file, "--env-file", envFile,
      "up", "-d", "--no-deps", ...services])) {
      failure = "docker compose up failed";
    }
  }
  let health = "not_checked";
  if (!failure) {
    const checked = await waitHealthy(fetchImpl, detail.health?.url, detail.health?.timeout_s, sleep);
    health = checked.health;
    if (!checked.ok) failure = `health check failed (${checked.why})`;
    else note(`health: ${health}`);
  }
  const states = Object.fromEntries(services.map((name) => [name, failure ? "failed" : "restarted"]));
  if (!failure) {
    await report("deployed", { note: `${services.length} service(s) restarted`, services: states, health });
    return { status: "deployed" };
  }
  note(failure);
  if (detail.auto_rollback && previousEnv) {
    note("rolling back to the previous images");
    writeAtomic(envFile, previousEnv);
    const back = composeServices(previous, changed);
    const restarted = back.length === 0 || await compose(["up", "-d", "--no-deps", ...back])
      .then((result) => result.code === 0);
    const checked = restarted
      ? await waitHealthy(fetchImpl, detail.health?.url, detail.health?.timeout_s, sleep)
      : { ok: false, health: "unhealthy" };
    if (restarted && checked.ok) {
      await report("rolled_back", { note: `${failure}; back on ${detail.previous?.commit ?? "the previous release"}`,
        services: Object.fromEntries(back.map((name) => [name, "rolled_back"])), health: checked.health });
      return { status: "rolled_back" };
    }
    note("the rollback did not come up healthy either");
    await report("failed", { note: `${failure}; rollback failed too`, services: states, health: checked.health });
    return { status: "failed" };
  }
  await report("failed", { note: failure, services: states, health });
  return { status: "failed" };
}

/** Read the env file this computer deployed last (for `veynod doctor`). */
export function currentImages(settings) {
  try {
    return readFileSync(join(settings.dir, ".veynod", "deploy.env"), "utf8");
  } catch {
    return "";
  }
}
