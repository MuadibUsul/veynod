import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { runHub } from "../bin/up.mjs";
import { checkForUpdate, confirmRunning, installLauncher, newer, npmInstall, pickVersion, readState, startUpdates,
  writeState } from "../bin/update.mjs";

const scratch = () => mkdtempSync(join(tmpdir(), "veynod-update-"));

/** A fake installed version: its veynod.mjs prints what ran, or throws when `starts` is false. */
function fakeVersion(root, version, { starts = true } = {}) {
  const bin = join(root, version, "node_modules", "veynod", "bin");
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, "veynod.mjs"), starts
    ? `console.log(JSON.stringify({ ran: ${JSON.stringify(version)}, launcher: process.env.VEYNOD_LAUNCHER, args: process.argv.slice(2) }));\n`
    : `throw new Error("${version} cannot start");\n`);
}

/** Runs the launcher the way the service does; {code, out}. */
function launch(launcher, args = ["hub"]) {
  try {
    return { code: 0, out: JSON.parse(execFileSync(process.execPath, [launcher, ...args], { encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"] })) };
  } catch (error) {
    return { code: error.status ?? 1, out: null };
  }
}

const registry = (version) => async (url) => {
  assert.equal(url, "https://registry.npmjs.org/veynod/latest");
  return { ok: true, json: async () => ({ version }) };
};

test("versions compare as x.y.z numbers; anything else is never newer", () => {
  assert.equal(newer("0.10.0", "0.9.9"), true);
  assert.equal(newer("1.0.0", "1.0.0"), false);
  assert.equal(newer("0.7.0", "0.7.1"), false);
  assert.equal(newer("0.8.0-beta.1", "0.7.0"), false);
  assert.equal(newer(undefined, "0.7.0"), false);
});

test("the launcher gives a version three starts to confirm, then goes back to the one before", () => {
  const confirmed = { current: "1.0.0", confirmed: "1.0.0", previous: null, attempts: 0 };
  assert.deepEqual(pickVersion(confirmed), confirmed);
  let state = { current: "1.1.0", confirmed: "1.0.0", previous: "1.0.0", attempts: 0 };
  for (const attempt of [1, 2, 3]) {
    state = pickVersion(state);
    assert.equal(state.current, "1.1.0");
    assert.equal(state.attempts, attempt);
  }
  state = pickVersion(state);
  assert.deepEqual(state, { current: "1.0.0", confirmed: "1.0.0", previous: null, attempts: 0, rolled_back: "1.1.0" });
  // Nothing to go back to: it keeps trying the only version there is.
  assert.equal(pickVersion({ current: "1.0.0", confirmed: null, previous: null, attempts: 9 }).current, "1.0.0");
});

test("the real launcher starts the current version, and rolls back one that cannot start", () => {
  const root = scratch();
  fakeVersion(root, "1.0.0");
  const launcher = installLauncher("1.0.0", root);
  assert.equal(launcher, join(root, "veynod.mjs"));
  const first = launch(launcher);
  assert.equal(first.code, 0);
  assert.equal(first.out.ran, "1.0.0");
  // The launcher reports its real path; macOS's temp directory is behind a symlink.
  assert.equal(first.out.launcher, realpathSync(launcher), "the hub knows the launcher started it");
  assert.deepEqual(first.out.args, ["hub"]);
  confirmRunning("1.0.0", root);

  // An update to a version that cannot start: three failed starts, then 1.0.0 again.
  fakeVersion(root, "1.1.0", { starts: false });
  writeState(root, { ...readState(root), previous: "1.0.0", current: "1.1.0", attempts: 0 });
  for (let i = 0; i < 3; i += 1) assert.notEqual(launch(launcher).code, 0);
  const back = launch(launcher);
  assert.equal(back.code, 0);
  assert.equal(back.out.ran, "1.0.0");
  assert.equal(readState(root).rolled_back, "1.1.0");
});

test("running npx veynod again keeps the version before for a rollback", () => {
  const root = scratch();
  installLauncher("1.0.0", root);
  assert.equal(readState(root).previous, null);
  installLauncher("1.1.0", root);
  assert.equal(readState(root).previous, "1.0.0");
  // The same version again is not its own previous.
  installLauncher("1.1.0", root);
  assert.equal(readState(root).previous, "1.0.0");
  // A version the person asks for by name is not skipped as rolled back.
  writeState(root, { ...readState(root), rolled_back: "1.2.0" });
  installLauncher("1.2.0", root);
  assert.equal(readState(root).rolled_back, undefined);
  assert.equal(readState(root).current, "1.2.0");
});

test("a hub that comes up confirms its version and keeps only it and the one before", () => {
  const root = scratch();
  for (const version of ["0.9.0", "1.0.0", "1.1.0"]) fakeVersion(root, version);
  installLauncher("1.1.0", root);
  writeState(root, { ...readState(root), previous: "1.0.0", attempts: 2 });
  assert.equal(confirmRunning("1.0.0", root), false, "only the current version confirms");
  assert.equal(confirmRunning("1.1.0", root), true);
  assert.equal(readState(root).confirmed, "1.1.0");
  assert.equal(readState(root).attempts, 0);
  assert.deepEqual(readdirSync(root).sort(), ["1.0.0", "1.1.0", "state.json", "veynod.mjs"]);
});

test("an update check installs a newer version beside this one and points the launcher at it", async () => {
  const root = scratch();
  installLauncher("1.0.0", root);
  confirmRunning("1.0.0", root);
  const installed = [];
  const install = async (version, where) => { installed.push([version, where]); };
  const base = { version: "1.0.0", root, launched: join(root, "veynod.mjs"), install };

  assert.equal((await checkForUpdate({ ...base, config: { auto_update: false }, fetchImpl: registry("2.0.0") })).text,
    "auto-update is off");
  assert.equal((await checkForUpdate({ ...base, launched: undefined, fetchImpl: registry("2.0.0") })).text,
    "not running as the service");
  assert.equal((await checkForUpdate({ ...base, fetchImpl: async () => { throw new Error("offline"); } })).text,
    "npm could not be reached");
  assert.equal((await checkForUpdate({ ...base, fetchImpl: registry("1.0.0") })).text, "up to date (1.0.0)");
  assert.deepEqual(installed, []);

  const result = await checkForUpdate({ ...base, fetchImpl: registry("1.1.0") });
  assert.deepEqual(result, { installed: "1.1.0", text: "installed 1.1.0" });
  assert.deepEqual(installed, [["1.1.0", root]]);
  assert.deepEqual(readState(root), { current: "1.1.0", previous: "1.0.0", confirmed: "1.0.0", attempts: 0 });
});

test("a version that was rolled back is not installed again; a failed install changes nothing", async () => {
  const root = scratch();
  writeState(root, { current: "1.0.0", confirmed: "1.0.0", previous: null, attempts: 0, rolled_back: "1.1.0" });
  const base = { version: "1.0.0", root, launched: "launcher" };
  const skipped = await checkForUpdate({ ...base, fetchImpl: registry("1.1.0"), install: async () => assert.fail() });
  assert.match(skipped.text, /1\.1\.0 did not start here/);
  const failed = await checkForUpdate({ ...base, fetchImpl: registry("1.2.0"),
    install: async () => { throw new Error("npm ERR! network"); } });
  assert.deepEqual(failed, { text: "could not install 1.2.0: npm ERR! network" });
  assert.equal(readState(root).current, "1.0.0");
});

test("npm installs into a scratch directory, then the version moves into place whole", async () => {
  const root = scratch();
  const calls = [];
  const run = (command, args, options, done) => {
    calls.push({ command, args, options });
    mkdirSync(join(options.cwd, "node_modules", "veynod", "bin"), { recursive: true });
    writeFileSync(join(options.cwd, "node_modules", "veynod", "bin", "veynod.mjs"), "// 1.1.0");
    done(null);
  };
  const target = await npmInstall("1.1.0", root, { run });
  assert.equal(target, join(root, "1.1.0"));
  assert.equal(readFileSync(join(target, "node_modules", "veynod", "bin", "veynod.mjs"), "utf8"), "// 1.1.0");
  assert.equal(calls[0].command, "npm");
  assert.equal(calls[0].args.at(-1), '"veynod@1.1.0"', "the spec reaches the shell quoted");
  assert.equal(calls[0].options.shell, true, "npm is npm.cmd on Windows");
  assert.deepEqual(readdirSync(root), ["1.1.0"], "no scratch directory is left");

  const failing = (command, args, options, done) => done(new Error("npm ERR! 404"));
  await assert.rejects(npmInstall("1.2.0", root, { run: failing }), /404/);
  const empty = (command, args, options, done) => done(null);
  await assert.rejects(npmInstall("1.3.0", root, { run: empty }), /installed no veynod/);
  await assert.rejects(npmInstall("1.0.0 && echo hi", root, { run }), /not a version/);
  await assert.rejects(npmInstall("1.4.0", root, { run, spec: 'veynod@1.4.0" & calc "' }), /not a package spec/);
  await npmInstall("1.4.0", root, { run, spec: "C:\\Users\\RUNNER~1\\veynod-1.4.0.tgz" });
  assert.equal(calls.at(-1).args.at(-1), '"C:\\Users\\RUNNER~1\\veynod-1.4.0.tgz"', "a Windows path keeps single backslashes");
  assert.deepEqual(readdirSync(root).sort(), ["1.1.0", "1.4.0"]);
});

test("the hub's updater confirms, installs, and restarts only once no task runs", async () => {
  const root = scratch();
  installLauncher("1.0.0", root);
  const said = [];
  let running = 1;
  let restarts = 0;
  let checks = 0;
  const check = async () => {
    checks += 1;
    writeState(root, { ...readState(root), previous: "1.0.0", current: "1.1.0" });
    return { installed: "1.1.0", text: "installed 1.1.0" };
  };
  const stop = startUpdates({ version: "1.0.0", root, launched: "launcher", check, say: (text) => said.push(text),
    busy: () => running > 0, restart: () => { restarts += 1; }, idleEvery: 5, every: 60_000 });
  assert.equal(readState(root).confirmed, "1.0.0", "the version confirms as soon as the hub is up");
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(restarts, 0, "a running task is never cut short");
  assert.ok(said.includes("[update] 1.1.0 takes over once the running tasks finish"));
  running = 0;
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(restarts, 1);
  assert.equal(checks, 1, "no second install while one waits");
  stop();

  // Outside the service, or with updates off, it does nothing but confirm.
  assert.equal(typeof startUpdates({ version: "1.0.0", root, launched: undefined, check: async () => assert.fail() }), "function");
  const off = [];
  startUpdates({ version: "1.1.0", root, launched: "launcher", config: { auto_update: false },
    check: async () => assert.fail(), say: (text) => off.push(text) });
  assert.deepEqual(off, ["[update] auto-update is off (npx veynod --no-auto-update)"]);
});

test("the hub tells its updater whether any task is running", async () => {
  const home = scratch();
  const keyFile = join(home, "computer.key");
  writeFileSync(keyFile, "vk_test");
  const config = { host: "https://veynod.example", run_tasks: true, runtime: "claude", slots: 1,
    identities: [{ role: "computer", agent_id: "dou-pc", key_file: keyFile }] };
  const messenger = { running: new Map(), stop: () => {} };
  let busy;
  let stopped = false;
  let finish;
  const done = runHub({}, { home, config, version: "1.0.0", runComputer: async () => messenger,
    forever: () => new Promise((resolve) => { finish = resolve; }),
    onStarted: (hub) => {
      busy = hub.busy;
      assert.equal(hub.config, config);
      assert.equal(typeof hub.restart, "function", "the updater exits through the hub's own stop");
      return () => { stopped = true; };
    } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(busy(), false);
  messenger.running.set("task-1", "frame-1");
  assert.equal(busy(), true);
  finish(0);
  assert.equal(await done, 0);
  assert.equal(stopped, true, "the updater stops with the hub");
  assert.ok(!existsSync(join(home, ".veynod", "hub-dou-pc.lock")));
});
