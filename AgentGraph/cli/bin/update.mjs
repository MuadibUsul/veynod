// The background service keeps itself current, the same way on every OS.
//
// The service starts ~/.veynod/cli/veynod.mjs, a small launcher that loads the
// version named in ~/.veynod/cli/state.json. An update installs the new
// version beside the old one, points `current` at it and lets the hub exit;
// the service manager starts it again (systemd Restart=always, launchd
// KeepAlive, the Windows wscript loop), now on the new version. A hub that
// comes up confirms its version. One that fails to start three times in a row
// is rolled back to the previous version by the launcher, before it loads, and
// that version is not installed again; a newer one is.
//
// The hub restarts only while no task runs on it, so an update never cuts a
// session short. Updates come from the npm registry through npm itself, so npm
// checks the package's integrity. `npx veynod --no-auto-update` turns them off.

import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const UPDATE_EVERY_MS = 6 * 60 * 60 * 1000;
// How often a hub with a newer version waiting looks for a moment with no task running.
export const IDLE_CHECK_MS = 60 * 1000;
const VERSION = /^\d+\.\d+\.\d+$/;
// What may reach the shell npm runs in: a package spec or a tarball path, with
// nothing sh or cmd.exe treats as special inside double quotes.
const SPEC = /^[\w@./:\\~ -]+$/;

/** Whether version `a` is newer than `b` (plain x.y.z). */
export function newer(a, b) {
  if (!VERSION.test(a || "") || !VERSION.test(b || "")) return false;
  const left = a.split(".").map(Number);
  const right = b.split(".").map(Number);
  for (let i = 0; i < 3; i += 1) {
    if (left.at(i) !== right.at(i)) return left.at(i) > right.at(i);
  }
  return false;
}

/**
 * Which version the launcher starts, and the state it leaves: a version that
 * has not confirmed it started counts one more attempt; after three it gives
 * way to the previous version, which ran here before and counts as confirmed.
 * Self-contained: the launcher embeds it as text.
 */
export function pickVersion(state) {
  const next = { ...state };
  if (next.current && next.current !== next.confirmed) {
    next.attempts = (next.attempts || 0) + 1;
    if (next.attempts > 3 && next.previous && next.previous !== next.current) {
      next.rolled_back = next.current;
      next.current = next.previous;
      next.confirmed = next.previous;
      next.previous = null;
      next.attempts = 0;
    }
  }
  return next;
}

/** The launcher's source: pickVersion, then load that version's CLI as if it were run directly. */
export function launcherSource() {
  return [
    "// veynod launcher: starts the version named in state.json (see update.mjs).",
    "// Written by `npx veynod`; replaced when you run it again.",
    'import { readFileSync, writeFileSync } from "node:fs";',
    'import { dirname, join } from "node:path";',
    'import { fileURLToPath, pathToFileURL } from "node:url";',
    `const pickVersion = ${pickVersion.toString()};`,
    "const root = dirname(fileURLToPath(import.meta.url));",
    'const file = join(root, "state.json");',
    'const state = pickVersion(JSON.parse(readFileSync(file, "utf8")));',
    'writeFileSync(file, `${JSON.stringify(state, null, 2)}\\n`);',
    'const script = join(root, state.current, "node_modules", "veynod", "bin", "veynod.mjs");',
    "process.env.VEYNOD_LAUNCHER = fileURLToPath(import.meta.url);",
    "await import(pathToFileURL(script).href);",
    "",
  ].join("\n");
}

export const statePath = (root) => join(root, "state.json");

export function readState(root) {
  try {
    return JSON.parse(readFileSync(statePath(root), "utf8"));
  } catch {
    return {};
  }
}

export function writeState(root, state) {
  mkdirSync(root, { recursive: true, mode: 0o700 });
  writeFileSync(statePath(root), `${JSON.stringify(state, null, 2)}\n`);
}

/**
 * Writes the launcher and makes `version` (already copied to root/<version>)
 * the one it starts. Returns the launcher's path, which the service runs.
 * The version the person ran is theirs to choose, so it is never skipped as a
 * rolled-back one; it still has to confirm it starts, or the launcher goes
 * back to the version that ran before.
 */
export function installLauncher(version, root) {
  mkdirSync(root, { recursive: true, mode: 0o700 });
  const launcher = join(root, "veynod.mjs");
  writeFileSync(launcher, launcherSource());
  const before = readState(root);
  const previous = before.current && before.current !== version ? before.current : before.previous ?? null;
  const next = { current: version, previous: previous === version ? null : previous,
    confirmed: before.confirmed ?? null, attempts: 0 };
  if (before.rolled_back && before.rolled_back !== version) next.rolled_back = before.rolled_back;
  writeState(root, next);
  return launcher;
}

/** Removes the installed versions not in `keep`; the launcher and its state stay. */
export function pruneVersions(root, keep) {
  const removed = [];
  for (const name of existsSync(root) ? readdirSync(root) : []) {
    if (VERSION.test(name) && !keep.includes(name)) {
      rmSync(join(root, name), { recursive: true, force: true });
      removed.push(name);
    }
  }
  return removed;
}

/** The running hub says it started: the launcher keeps this version from now on. */
export function confirmRunning(version, root) {
  const state = readState(root);
  if (state.current !== version) return false;
  if (state.confirmed !== version || state.attempts) writeState(root, { ...state, confirmed: version, attempts: 0 });
  // Keep the version running and the one before it (for a rollback); drop the rest.
  pruneVersions(root, [state.current, state.previous].filter(Boolean));
  return true;
}

/** npm's newest published veynod, or null when the registry cannot be reached. */
export async function latestVersion(fetchImpl = fetch) {
  try {
    const response = await fetchImpl("https://registry.npmjs.org/veynod/latest", { signal: AbortSignal.timeout(15_000) });
    if (!response.ok) return null;
    const version = (await response.json())?.version;
    return VERSION.test(version || "") ? version : null;
  } catch {
    return null;
  }
}

/**
 * Installs `spec` (veynod@<version>, or a tarball) as root/<version> with npm:
 * into a scratch directory first, then moved into place in one rename, so the
 * launcher never finds half a version. npm is npm.cmd on Windows, which only a
 * shell starts; the version and the spec are checked and the spec is quoted,
 * so nothing from outside reaches that shell.
 */
export function npmInstall(version, root, { spec = `veynod@${version}`, run = execFile } = {}) {
  if (!VERSION.test(version)) return Promise.reject(new Error(`not a version: ${version}`));
  if (!SPEC.test(spec)) return Promise.reject(new Error(`not a package spec: ${spec}`));
  const scratch = join(root, `.install-${version}`);
  rmSync(scratch, { recursive: true, force: true });
  mkdirSync(scratch, { recursive: true });
  writeFileSync(join(scratch, "package.json"), "{}\n");
  return new Promise((resolve, reject) => {
    run("npm", ["install", "--no-audit", "--no-fund", "--loglevel=error", `"${spec}"`],
      { cwd: scratch, shell: true, timeout: 10 * 60_000, windowsHide: true }, (error) => {
        try {
          if (error) throw error;
          if (!existsSync(join(scratch, "node_modules", "veynod", "bin", "veynod.mjs"))) {
            throw new Error("npm installed no veynod");
          }
          const target = join(root, version);
          rmSync(target, { recursive: true, force: true });
          mkdirSync(target, { recursive: true });
          renameSync(join(scratch, "node_modules"), join(target, "node_modules"));
          resolve(target);
        } catch (failure) {
          reject(failure);
        } finally {
          rmSync(scratch, { recursive: true, force: true });
        }
      });
  });
}

/**
 * One update check: when npm has a newer veynod, install it beside this one
 * and point the launcher at it. Only for a hub the launcher started (the
 * service), and not when the owner turned updates off. Returns
 * {installed, text}: the version now waiting for a restart, and what happened
 * in words for the log.
 */
export async function checkForUpdate({ version, root, config = {}, launched = process.env.VEYNOD_LAUNCHER,
  fetchImpl = fetch, install = npmInstall } = {}) {
  if (config?.auto_update === false) return { text: "auto-update is off" };
  if (!launched) return { text: "not running as the service" };
  const latest = await latestVersion(fetchImpl);
  if (!latest) return { text: "npm could not be reached" };
  if (!newer(latest, version)) return { text: `up to date (${version})` };
  const state = readState(root);
  if (state.rolled_back === latest) return { text: `${latest} did not start here and was rolled back; waiting for a newer one` };
  try {
    await install(latest, root);
  } catch (error) {
    return { text: `could not install ${latest}: ${error.message}` };
  }
  writeState(root, { ...readState(root), previous: version, current: latest, attempts: 0 });
  return { installed: latest, text: `installed ${latest}` };
}

/**
 * The hub's updater: confirms this version started, then checks now and every
 * six hours. A newly installed version takes over at the first moment no task
 * runs here (`busy()`): the hub exits and its service starts the new one.
 * Returns stop().
 */
export function startUpdates({ version, root, config, say = () => {}, busy = () => false,
  restart = () => process.kill(process.pid, "SIGTERM"), check = checkForUpdate,
  every = UPDATE_EVERY_MS, idleEvery = IDLE_CHECK_MS, launched = process.env.VEYNOD_LAUNCHER } = {}) {
  if (!launched) return () => {};
  confirmRunning(version, root);
  if (config?.auto_update === false) {
    say("[update] auto-update is off (npx veynod --no-auto-update)");
    return () => {};
  }
  let waiting = null;
  let idleTimer = null;
  let checking = false;
  const restartWhenIdle = () => {
    if (busy()) return;
    clearInterval(idleTimer);
    say(`[update] restarting on ${waiting}`);
    restart();
  };
  const run = async () => {
    if (checking || waiting) return;
    checking = true;
    try {
      const result = await check({ version, root, config, launched });
      say(`[update] ${result.text}`);
      if (result.installed) {
        waiting = result.installed;
        if (busy()) say(`[update] ${waiting} takes over once the running tasks finish`);
        idleTimer = setInterval(restartWhenIdle, idleEvery);
        idleTimer.unref?.();
        restartWhenIdle();
      }
    } catch (error) {
      say(`[update] check failed: ${error.message}`);
    } finally {
      checking = false;
    }
  };
  const timer = setInterval(() => void run(), every);
  timer.unref?.();
  void run();
  return () => {
    clearInterval(timer);
    clearInterval(idleTimer);
  };
}
