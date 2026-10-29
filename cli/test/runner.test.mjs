import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { Worktrees, adapterFor, inventory, placementOf, promptFor, runRunner } from "../bin/runner.mjs";
import { parseArgs, servicePlan } from "../bin/veynod.mjs";

const git = (args, cwd) => execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();

/** A local "origin" with one commit on main, served from a directory. */
function origin() {
  const base = mkdtempSync(join(tmpdir(), "veynod-origin-"));
  const repo = join(base, "acme", "site.git");
  execFileSync("git", ["init", "--bare", "--quiet", "-b", "main", repo]);
  const seed = mkdtempSync(join(tmpdir(), "veynod-seed-"));
  git(["init", "--quiet", "-b", "main"], seed);
  git(["config", "user.email", "t@example.test"], seed);
  git(["config", "user.name", "t"], seed);
  writeFileSync(join(seed, "README.md"), "hello\n");
  git(["add", "."], seed);
  git(["commit", "--quiet", "-m", "first"], seed);
  git(["push", "--quiet", repo, "main"], seed);
  return { base: `file://${base}/`, repo };
}

test("a placement poke yields the task and its idempotency key", () => {
  assert.deepEqual(placementOf({ kind: "task.placed", task_key: "fix-login", note: "placement plc_01M630S0A35N82N1TFY7XGSQ88" }),
    { taskKey: "fix-login", placementId: "plc_01M630S0A35N82N1TFY7XGSQ88" });
  assert.deepEqual(placementOf({ kind: "task.placed", doc_key: "k", note: "placement plc_01M630S0A35N82N1TFY7XGSQ88" }).taskKey, "k");
  assert.equal(placementOf({ kind: "task.assigned", task_key: "x", note: "plc_01M630S0A35N82N1TFY7XGSQ88" }), null);
  assert.equal(placementOf({ kind: "task.placed", task_key: "x", note: "no id" }), null);
});

test("inventory reads OAR, keeps quota and drops the account email", async () => {
  const fakeOar = {
    claudeRuntime: {
      installation: async () => ({ kind: "available", version: "2.1.3" }),
      listModels: async () => ({ kind: "ok", models: [{ id: "sonnet" }, { id: "opus" }] }),
      accountUsage: async () => ({ kind: "available", plan: "max", email: "dou@example.com", rateLimited: false,
        windows: [{ label: "5h", usedRatio: 0.415, resetsAt: "2026-10-29T10:00:00Z" }] }),
    },
  };
  const importer = async (spec) => {
    if (spec.endsWith("/claude")) return fakeOar;
    throw new Error("not installed");
  };
  const run = (command) => {
    if (command === "codex") return "codex-cli 0.80.0\n";
    throw new Error("missing");
  };
  const found = await inventory({ importer, run });
  assert.deepEqual(found, [
    { id: "claude", available: true, via: "oar", version: "2.1.3", models: ["sonnet", "opus"],
      usage: { plan: "max", windows: [{ name: "5h", used_percent: 41.5, resets_at: "2026-10-29T10:00:00Z" }] } },
    { id: "codex", available: true, via: "cli", version: "codex-cli 0.80.0" },
  ]);
  assert.ok(!JSON.stringify(found).includes("dou@example.com"));
});

test("the prompt names the task, the branch and the hand-off", () => {
  const text = promptFor({ taskKey: "fix-login", placementId: "plc_X", branch: "veynod/fix-login", host: "https://veynod.com" });
  assert.match(text, /agent_claim_and_read\("fix-login"\)/);
  assert.match(text, /push only that branch/);
  assert.match(text, /release the task/);
});

test("worktrees: one per task, only its branch can be pushed, cleaned up after", () => {
  const { base, repo } = origin();
  const root = mkdtempSync(join(tmpdir(), "veynod-runner-"));
  const trees = new Worktrees({ root, repos: ["acme/site"], gitBase: base });
  assert.throws(() => trees.prepare("t1", "acme/other"), (error) => error.code === "repo_not_allowed");

  const one = trees.prepare("t1", "acme/site");
  const two = trees.prepare("t2", "acme/site");
  assert.notEqual(one.dir, two.dir);
  assert.equal(git(["rev-parse", "--abbrev-ref", "HEAD"], one.dir), "veynod/t1");
  assert.equal(git(["rev-parse", "--abbrev-ref", "HEAD"], two.dir), "veynod/t2");
  assert.equal(one.base, "main");
  assert.equal(readFileSync(join(one.dir, "README.md"), "utf8"), "hello\n");

  git(["config", "user.email", "t@example.test"], one.dir);
  git(["config", "user.name", "t"], one.dir);
  writeFileSync(join(one.dir, "change.txt"), "work\n");
  git(["add", "."], one.dir);
  git(["commit", "--quiet", "-m", "work"], one.dir);
  git(["push", "--quiet", "origin", "veynod/t1"], one.dir);
  assert.equal(git(["rev-parse", "--verify", "--quiet", "refs/heads/veynod/t1"], repo).length, 40);
  assert.throws(() => git(["push", "--quiet", "origin", "veynod/t1:main"], one.dir), /may only push veynod\/t1/);

  trees.remove("t1", "acme/site");
  assert.equal(existsSync(one.dir), false);
  assert.ok(!git(["worktree", "list"], trees.cache("acme/site")).includes(one.dir));
});

test("worktrees: over the disk cap the oldest finished one goes first", () => {
  const { base } = origin();
  const root = mkdtempSync(join(tmpdir(), "veynod-runner-"));
  const trees = new Worktrees({ root, repos: ["acme/site"], gitBase: base, maxBytes: 1 });
  const first = trees.prepare("old", "acme/site");
  const second = trees.prepare("new", "acme/site");
  assert.equal(existsSync(first.dir), false);
  assert.equal(existsSync(second.dir), true);
  assert.throws(() => trees.prepare("third", "acme/site", new Set(["new"])), (error) => error.code === "disk_full");
});

test("custom runtime runs the command with the prompt in the environment", async () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-custom-"));
  const adapter = adapterFor("custom", { exec: 'node -e "require(\'fs\').writeFileSync(\'out.txt\', process.env.VEYNOD_PROMPT)"' });
  const result = await adapter.run({ cwd: dir, prompt: "do it", env: {} });
  assert.equal(result.ok, true);
  assert.equal(readFileSync(join(dir, "out.txt"), "utf8"), "do it");
  assert.throws(() => adapterFor("custom"), /needs --exec/);
  assert.throws(() => adapterFor("gemini"), /unknown runtime/);
});

test("runner arguments and the runner service plan", () => {
  const options = parseArgs(["runner", "--runtime", "codex", "--slots", "2", "--repo", "acme/site,acme/api"]);
  assert.equal(options.runtime, "codex");
  assert.equal(options.slots, 2);
  assert.deepEqual(options.repos, ["acme/site", "acme/api"]);
  assert.throws(() => parseArgs(["runner", "--slots", "0"]), /--slots/);
  assert.throws(() => parseArgs(["runner", "--repo", "not a repo"]), /owner\/name/);

  const linux = servicePlan("install", { os: "linux", home: "/home/u", node: "/usr/bin/node", script: "/opt/veynod.mjs",
    host: "https://veynod.com", keyFile: "/home/u/.veynod/runner.key", mode: "runner",
    runner: { runtime: "codex", slots: 2, repos: ["acme/site"], diskMb: 2048 } });
  assert.equal(linux.files[0].path, "/home/u/.config/systemd/user/veynod-runner.service");
  assert.match(linux.files[0].content, /ExecStart="\/usr\/bin\/node" "\/opt\/veynod.mjs" "runner" .*"--runtime" "codex" "--slots" "2" "--repo" "acme\/site" "--disk-mb" "2048"/);
  assert.match(linux.files[0].content, /Restart=always/);
  const mac = servicePlan("install", { os: "darwin", home: "/Users/u", host: "h", keyFile: "k", mode: "runner" });
  assert.match(mac.files[0].path, /com\.veynod\.runner\.plist$/);
  const win = servicePlan("install", { os: "win32", home: "C:\\Users\\u", host: "h", keyFile: "k", mode: "runner" });
  assert.ok(win.commands[0].includes("VeynodRunner"));
  assert.match(win.files[0].content, /shell.Run .*, 0, True/);
  // The listen service keeps its own names.
  const listen = servicePlan("install", { os: "linux", home: "/home/u", host: "h", keyFile: "k" });
  assert.match(listen.files[0].path, /veynod-listen\.service$/);
});

test("end to end: hello, a placed task runs in its worktree, heartbeats report it", async () => {
  const { base } = origin();
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const calls = [];
  let hello = null;
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      calls.push({ path: req.url, body: body ? JSON.parse(body) : null, auth: req.headers.authorization });
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/v1/runner/hello") {
        hello = JSON.parse(body);
        res.end(JSON.stringify({ host_id: "hst_1", agent_id: "pi-code" }));
      } else if (req.url === "/v1/runner/heartbeat") {
        res.end(JSON.stringify({ host_id: "hst_1" }));
      } else if (req.url === "/v1/tasks/fix-login") {
        res.end(JSON.stringify({ task_key: "fix-login", repo: "acme/site" }));
      } else {
        res.statusCode = 404;
        res.end("{}");
      }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const host = `http://127.0.0.1:${server.address().port}`;

  // The stream: one placement, then the same poke again (must be ignored).
  const follow = async (options) => {
    const poke = { kind: "task.placed", task_key: "fix-login", note: "placement plc_01M630S0A35N82N1TFY7XGSQ88" };
    await options.onPoke(poke);
    await options.onPoke(poke);
    return 0;
  };
  const exec = 'node -e "require(\'fs\').writeFileSync(\'done.txt\', process.env.VEYNOD_TASK_KEY + \' \' + process.cwd())"';
  const code = await runRunner({ host, key: "ag_test", runtime: "custom", exec, slots: 1, repos: ["acme/site"],
    gitBase: base }, { follow, version: "0.4.0", every: 50, stateDir: state });
  server.close();
  assert.equal(code, 0);

  assert.equal(hello.host.max_concurrency, 1);
  assert.deepEqual(hello.host.repos, ["acme/site"]);
  assert.deepEqual(hello.host.runtimes, [{ id: "custom", available: true, via: "exec" }]);
  assert.ok(calls.every((call) => call.auth === "Bearer ag_test"));
  const beats = calls.filter((call) => call.path === "/v1/runner/heartbeat").map((call) => call.body.running);
  assert.ok(beats.some((running) => running.includes("fix-login")), JSON.stringify(beats));
  assert.deepEqual(beats.at(-1), []);

  const receipts = readFileSync(join(state, "runner-receipts.jsonl"), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(receipts.map((item) => item.stage), ["delivered", "accepted", "started", "finished"]);
  assert.ok(receipts.every((item) => item.placement_id === "plc_01M630S0A35N82N1TFY7XGSQ88"));
  // The worktree it ran in is gone afterwards.
  assert.equal(existsSync(join(state, "runner", "work", "fix-login")), false);
});
