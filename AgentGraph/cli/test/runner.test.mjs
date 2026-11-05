import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { adapterFor, chooseRuntime, claudeMcpConfig, commandWorks, createRunner, installSteps, inventory,
    pinsIdentity, pipelineLines, placementOf, promptFor, resolveCommand, resolveLeaseCwd,
    shimTarget, veynodMcpServer }
  from "../bin/runner.mjs";
import { parseArgs, servicePlan } from "../bin/veynod.mjs";

/** What the messenger does with each poke: one `work` per placement, after a hello. */
async function drive(options, pokes, deps = {}) {
  const runner = await createRunner(options, deps);
  await runner.hello();
  for (const poke of pokes) {
    const placed = placementOf(poke);
    const slot = placed.taskKey ?? placed.placementId;
    runner.running.set(slot, placed.placementId);
    await runner.beat();
    try {
      await runner.work(placed);
    } finally {
      runner.running.delete(slot);
    }
  }
  await runner.beat();
  return runner;
}

const git = (args, cwd) => execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();


test("a nerve poke yields the task, the step and its idempotency key", () => {
  const id = "plc_01M630S0A35N82N1TFY7XGSQ88";
  assert.deepEqual(placementOf({ task_key: "fix-login", placement_id: id }),
    { taskKey: "fix-login", placementId: id, step: "work" });
  assert.deepEqual(placementOf({ task_key: "DG-x-api", placement_id: id, step: "review", diagram_key: "x", chunk_key: "api" }),
    { taskKey: "DG-x-api", placementId: id, step: "review", diagramKey: "x", chunkKey: "api" });
  assert.deepEqual(placementOf({ placement_id: id, step: "supervise" }),
    { taskKey: null, placementId: id, step: "supervise" });
  assert.equal(placementOf({ task_key: "x", placement_id: "no id" }), null);
  assert.equal(placementOf({ placement_id: id }), null, "work names its task");
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
  // commandWorks resolves the binary first and only then asks `run` for its
  // version, so the probe must see the bare name, not a host path.
  const resolve = (name) => ({ command: name, prefix: [] });
  const found = await inventory({ importer, run, resolve });
  assert.deepEqual(found, [
    { id: "claude", available: true, via: "oar", version: "2.1.3", models: ["sonnet", "opus"],
      usage: { plan: "max", windows: [{ name: "5h", used_percent: 41.5, resets_at: "2026-10-29T10:00:00Z" }] } },
    { id: "codex", available: true, via: "cli", version: "codex-cli 0.80.0" },
  ]);
  assert.ok(!JSON.stringify(found).includes("dou@example.com"));
});

test("each step gets its own prompt", () => {
  const review = promptFor({ taskKey: "DG-x-api", placementId: "plc_X", host: "h", step: "review", diagramKey: "x", chunkKey: "api" });
  assert.match(review, /get_audit_view\("DG-x-api"\)/);
  assert.match(review, /review_task\("DG-x-api", approve\) first, then the diagram gate/);
  assert.match(review, /diagram\(action="review", diagram_key="x", chunk_key="api"/);
  assert.match(review, /use the REST API instead/);
  assert.match(review, /Do not change the code yourself/);
  assert.doesNotMatch(review, /pull request/);
  const plain = promptFor({ taskKey: "t1", placementId: "plc_X", host: "h", step: "review" });
  assert.match(plain, /review_task\("t1"\)/);
  assert.doesNotMatch(plain, /diagrams/);
  const merge = promptFor({ taskKey: "DG-x-api", placementId: "plc_X", host: "h", step: "merge", diagramKey: "x", chunkKey: "api" });
  assert.match(merge, /diagram\(action="queue_check", diagram_key="x", chunk_key="api"/);
  assert.match(merge, /diagram\(action="merge", diagram_key="x", chunk_key="api", args=\{artifact: <document or link>\}\)/);
  assert.doesNotMatch(merge, /pull request|rebase|push/);
  const round = promptFor({ placementId: "plc_X", host: "h", step: "supervise", agenda: [{ kind: "no_host", task_key: "t9" }] });
  assert.match(round, /supervisor round/);
  assert.match(round, /"task_key":"t9"/);
  assert.match(round, /Do not write code in this round/);
  assert.match(round, /loop\(action="move", placement_id/);
  assert.match(round, /loop\(action="withdraw", placement_id\)/);
});

test("the prompt names the task and the hand-off, never a branch or a pull request", () => {
  const text = promptFor({ taskKey: "fix-login", placementId: "plc_X", host: "https://veynod.com" });
  assert.match(text, /agent_claim_and_read\("fix-login"\)/);
  assert.match(text, /release the task/);
  assert.doesNotMatch(text, /branch|pull request/);
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

function leaseCheckout() {
  const root = mkdtempSync(join(tmpdir(), "veynod-pool-"));
  const repo = join(root, "repo");
  const lease = join(root, ".veynod-worktrees", "lease", "abcd1234abcd1234");
  execFileSync("git", ["init", "-q", "-b", "main", repo]);
  execFileSync("git", ["config", "user.email", "t@example.com"], { cwd: repo });
  execFileSync("git", ["config", "user.name", "t"], { cwd: repo });
  writeFileSync(join(repo, "README"), "seed\n");
  execFileSync("git", ["add", "README"], { cwd: repo });
  execFileSync("git", ["commit", "-q", "-m", "seed"], { cwd: repo });
  execFileSync("git", ["worktree", "add", "--detach", "--quiet", lease, "HEAD"], { cwd: repo });
  return { repo, lease };
}

test("resolveLeaseCwd refuses the shared repository root", () => {
  const root = mkdtempSync(join(tmpdir(), "veynod-root-"));
  assert.throws(() => resolveLeaseCwd(
    { worktree_path: root, worktree_rel: "repo" },
    { exists: () => true },
  ), (error) => error.code === "worktree_not_isolated");
});

test("pipeline log names submission_service, verification_gate and merge_coordinator", () => {
  const lines = pipelineLines({
    submission: { submission_id: "sub-1", status: "QUEUED" },
    gate: {
      gate_results: [
        { stage: "STATIC_TYPECHECK", passed: true },
        { stage: "UNIT_TEST_SUITE", passed: true },
      ],
      merged: true,
      merge_sha: "abc123",
      merge_mode: "fast-forward",
    },
  });
  assert.deepEqual(lines, [
    "submission_service id=sub-1 status=QUEUED",
    "verification_gate STATIC_TYPECHECK=PASS UNIT_TEST_SUITE=PASS",
    "merge_coordinator sha=abc123 mode=fast-forward",
  ]);
});

test("end to end: hello, a placed task runs in its worktree, heartbeats report it", async () => {
  const { lease } = leaseCheckout();
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const calls = [];
  const said = [];
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
      } else if (req.url.endsWith("/report")) {
        res.end(JSON.stringify({ status: "offered" }));
      } else if (req.url === "/v1/runner/placements/plc_01M630S0A35N82N1TFY7XGSQ88") {
        res.end(JSON.stringify({ placement_id: "plc_01M630S0A35N82N1TFY7XGSQ88", status: "offered" }));
      } else if (req.url === "/v1/tasks/fix-login") {
        res.end(JSON.stringify({ task_key: "fix-login", repo: "acme/site" }));
      } else if (req.url === "/v1/tasks/fix-login/kernel/lease" && req.method === "POST") {
        res.end(JSON.stringify({
          leased: true, lease: "held", kernel_task_id: "11111111-1111-1111-1111-111111111111",
          write_set: ["done.txt"], worktree: "leased", worktree_path: lease,
          worktree_rel: ".veynod-worktrees/lease/abcd1234abcd1234",
        }));
      } else if (req.url === "/v1/tasks/fix-login/kernel/submit" && req.method === "POST") {
        res.end(JSON.stringify({ submission_id: "sub-1", status: "QUEUED", accepted: true }));
      } else if (req.url === "/v1/tasks/fix-login/kernel/delivery") {
        res.end(JSON.stringify({
          gate: "pass", merged: true, merge_sha: "abc123def456", merge_mode: "fast-forward",
          submission_id: "sub-1",
          gate_results: [
            { stage: "STATIC_TYPECHECK", passed: true },
            { stage: "UNIT_TEST_SUITE", passed: true },
          ],
        }));
      } else {
        res.statusCode = 404;
        res.end("{}");
      }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const host = `http://127.0.0.1:${server.address().port}`;

  const poke = { task_key: "fix-login", placement_id: "plc_01M630S0A35N82N1TFY7XGSQ88" };
  const exec = 'node -e "require(\'fs\').writeFileSync(\'done.txt\', process.env.VEYNOD_TASK_KEY + \' \' + process.cwd())"';
  await drive({ host, key: "ag_test", runtime: "custom", exec, slots: 1, repos: ["acme/site"] },
    [poke], { version: "0.4.0", stateDir: state, say: (line) => said.push(line) });
  server.close();

  assert.equal(hello.host.max_concurrency, 1);
  assert.deepEqual(hello.host.repos, ["acme/site"]);
  assert.deepEqual(hello.host.runtimes, [{ id: "custom", available: true, via: "exec" }]);
  assert.ok(calls.every((call) => call.auth === "Bearer ag_test"));
  const beats = calls.filter((call) => call.path === "/v1/runner/heartbeat").map((call) => call.body.running);
  assert.ok(beats.some((running) => running.includes("fix-login")), JSON.stringify(beats));
  assert.deepEqual(beats.at(-1), []);

  const reports = calls.filter((call) => call.path === "/v1/runner/placements/plc_01M630S0A35N82N1TFY7XGSQ88/report");
  assert.equal(reports.length, 1);
  assert.equal(reports[0].body.outcome, "finished");
  assert.match(reports[0].body.note, /merge_coordinator abc123def456/);

  const receipts = readFileSync(join(state, "runner-receipts.jsonl"), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(receipts.map((item) => item.stage), ["accepted", "cwd", "started", "pipeline", "pipeline", "pipeline", "finished"]);
  assert.ok(receipts.every((item) => item.placement_id === "plc_01M630S0A35N82N1TFY7XGSQ88"));
  const cwdLine = said.find((line) => line.includes("kernel worktree cwd="));
  assert.ok(cwdLine && cwdLine.includes(`${lease}`) && cwdLine.includes("pool=worktree_pool"), cwdLine);
  assert.ok(said.some((line) => line.startsWith("submission_service id=sub-1")));
  assert.ok(said.some((line) => line.includes("verification_gate STATIC_TYPECHECK=PASS")));
  assert.ok(said.some((line) => line.includes("merge_coordinator sha=abc123def456 mode=fast-forward")));
  assert.equal(readFileSync(join(lease, "done.txt"), "utf8").startsWith("fix-login"), true);
  // The shared checkout and the old runner worktree are not where the agent wrote.
  assert.equal(existsSync(join(state, "runner", "work", "fix-login")), false);
  const submit = calls.find((call) => call.path === "/v1/tasks/fix-login/kernel/submit");
  assert.match(submit.body.git_diff, /done\.txt/);
});


test("a supervise round reads its agenda, runs, and reports a failure", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const calls = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      calls.push({ path: req.url, body: body ? JSON.parse(body) : null });
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/v1/runner/hello") res.end(JSON.stringify({ host_id: "hst_2", agent_id: "boss" }));
      else if (req.url === "/v1/runner/placements/plc_01M630S0A35N82N1TFY7XGSQ99") {
        res.end(JSON.stringify({ status: "offered", detail: { agenda: [{ kind: "stopped", task_key: "t7" }] } }));
      } else res.end("{}");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const host = `http://127.0.0.1:${server.address().port}`;
  const poke = { placement_id: "plc_01M630S0A35N82N1TFY7XGSQ99", step: "supervise" };
  // The runtime writes the prompt it got, then fails.
  const exec = 'node -e "require(\'fs\').writeFileSync(require(\'path\').join(process.env.VEYNOD_STATE, \'prompt.txt\'), process.env.VEYNOD_PROMPT); process.exit(3)"';
  process.env.VEYNOD_STATE = state;
  await drive({ host, key: "ag_boss", runtime: "custom", exec, slots: 1 }, [poke],
    { version: "0.4.0", stateDir: state, sleepMs: 0 });
  server.close();
  assert.match(readFileSync(join(state, "prompt.txt"), "utf8"), /"task_key":"t7"/);
  const report = calls.find((call) => call.path.endsWith("/report"));
  assert.equal(report.body.outcome, "failed");
  assert.match(report.body.note, /exited 3/);
});

// ------------------------------------------------------------------ phase 0 (up-hub-p0-cli)

const noOar = async (spec) => { throw new Error(`no ${spec}`); };

/** A fake Veynod for the runner: answers hello, heartbeats, placements, tasks and renews. */
async function fakeVeynod({ placements = {}, tasks = {} } = {}) {
  const calls = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      calls.push({ method: req.method, path: req.url, body: body ? JSON.parse(body) : null });
      res.setHeader("Content-Type", "application/json");
      const placement = /^\/v1\/runner\/placements\/(plc_[0-9A-Z]+)$/.exec(req.url);
      const task = /^\/v1\/tasks\/([^/]+)$/.exec(req.url);
      if (req.url === "/v1/runner/hello") res.end(JSON.stringify({ host_id: "hst_9", agent_id: "box" }));
      else if (placement) res.end(JSON.stringify({ placement_id: placement[1], status: placements[placement[1]] ?? "offered" }));
      else if (task && req.method === "GET") res.end(JSON.stringify({ task_key: task[1], repo: tasks[task[1]] ?? null }));
      else res.end("{}");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { calls, host: `http://127.0.0.1:${server.address().port}`, close: () => server.close() };
}

const PLC_A = "plc_01M630S0A35N82N1TFY7XGSQAA";
const PLC_B = "plc_01M630S0A35N82N1TFY7XGSQBB";

test("claude without OAR runs with Veynod's MCP pinned to the runner key and a permission mode", async () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-pin-"));
  let seen = null;
  const spawner = async (command, args) => {
    const file = args[args.indexOf("--mcp-config") + 1];
    seen = { command, args, config: JSON.parse(readFileSync(file, "utf8")), file };
    return { ok: true };
  };
  const adapter = adapterFor("claude", { importer: noOar, spawner });
  const result = await adapter.run({ cwd: dir, prompt: "do it", env: {}, pin: { host: "https://veynod.example/", key: "ag_box" },
    configDir: join(dir, "run") });
  assert.equal(result.ok, true);
  assert.equal(seen.command, "claude");
  assert.deepEqual(seen.args.slice(0, 4), ["-p", "do it", "--permission-mode", "bypassPermissions"]);
  assert.ok(seen.args.includes("--strict-mcp-config"));
  assert.deepEqual(seen.config, claudeMcpConfig({ host: "https://veynod.example", key: "ag_box" }));
  assert.equal(seen.config.mcpServers.veynod.url, "https://veynod.example/mcp/");
  assert.equal(seen.config.mcpServers.veynod.headers.Authorization, "Bearer ag_box");
  assert.equal(existsSync(seen.file), false, "the config file with the key is removed after the run");
});

test("OAR sessions get only Veynod's MCP, with the runner key", async () => {
  let options = null;
  const importer = async (spec) => {
    if (spec === "@botiverse/oar/runtimes/claude") {
      return { claudeRuntime: {
        installation: async () => ({ kind: "available" }),
        session: async (_installation, opts) => { options = opts; return { dispose: async () => {} }; },
      } };
    }
    if (spec === "@botiverse/oar/observe") return { promptAndWait: async () => ({ kind: "ended" }) };
    throw new Error(spec);
  };
  const adapter = adapterFor("claude", { importer });
  const result = await adapter.run({ cwd: "/tmp", prompt: "p", env: { A: "1" }, pin: { host: "https://h", key: "ag_k" } });
  assert.equal(result.ok, true);
  assert.deepEqual(options.mcpServers, [veynodMcpServer({ host: "https://h", key: "ag_k" })]);
  assert.equal(options.userConfiguration, "exclude");
  assert.deepEqual(options.env, { A: "1" });
});

test("codex without OAR is reported as not pinned", async () => {
  assert.equal(await pinsIdentity("claude", { importer: noOar }), true);
  assert.equal(await pinsIdentity("custom", { importer: noOar }), true);
  assert.equal(await pinsIdentity("codex", { importer: noOar }), false);
  const withOar = async (spec) => (spec.endsWith("/codex") ? { codexRuntime: {} } : noOar(spec));
  assert.equal(await pinsIdentity("codex", { importer: withOar }), true);
});

test("a placement the platform moved on is not worked and not reported", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const fake = await fakeVeynod({ placements: { [PLC_A]: "moved" } });
  const marker = join(state, "ran.txt");
  const exec = `node -e "require('fs').writeFileSync('${marker}', 'x')"`;
  await drive({ host: fake.host, key: "ag_box", runtime: "custom", exec, slots: 1 },
    [{ task_key: "old", placement_id: PLC_A }], { version: "t", stateDir: state, sleepMs: 0 });
  fake.close();
  assert.equal(existsSync(marker), false);
  assert.equal(fake.calls.filter((call) => call.path.endsWith("/report")).length, 0);
  const stages = readFileSync(join(state, "runner-receipts.jsonl"), "utf8").trim().split("\n")
    .map((line) => JSON.parse(line).stage);
  assert.deepEqual(stages, ["skipped"]);
});

test("work without a repository runs in its own scratch directory, removed afterwards", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const fake = await fakeVeynod();
  const out = join(state, "cwd.txt");
  // Windows paths use backslashes, which the child's single-quoted JS string
  // would eat as escapes — double them so the written path stays intact.
  const exec = `node -e "require('fs').writeFileSync('${out.replaceAll("\\", "\\\\")}', process.cwd())"`;
  await drive({ host: fake.host, key: "ag_box", runtime: "custom", exec, slots: 1 },
    [{ task_key: "write-spec", placement_id: PLC_A }], { version: "t", stateDir: state, sleepMs: 0 });
  fake.close();
  const cwd = readFileSync(out, "utf8");
  assert.ok(cwd.startsWith(join(state, "runner", "scratch")), cwd);
  assert.equal(existsSync(cwd), false);
  assert.deepEqual(fake.calls.filter((call) => call.path.endsWith("/report")).map((call) => call.body), [{ outcome: "finished" }]);
});

test("the lease is kept alive while the runtime works", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const fake = await fakeVeynod();
  const exec = 'node -e "setTimeout(() => {}, 400)"';
  await drive({ host: fake.host, key: "ag_box", runtime: "custom", exec, slots: 1 },
    [{ task_key: "long-job", placement_id: PLC_A }],
    { version: "t", leaseEvery: 50, stateDir: state, sleepMs: 0 });
  fake.close();
  const renews = fake.calls.filter((call) => call.method === "POST" && call.path === "/v1/tasks/long-job/renew");
  assert.ok(renews.length >= 2, `renewed ${renews.length} times`);
  assert.equal(fake.calls.filter((call) => call.path === `/v1/runner/placements/${PLC_B}/report`).length, 0);
});

test("hello says whether the runner pins its MCP identity", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const fake = await fakeVeynod();
  await drive({ host: fake.host, key: "ag_box", runtime: "custom", exec: "true", slots: 1 }, [],
    { version: "t", stateDir: state });
  fake.close();
  const hello = fake.calls.find((call) => call.path === "/v1/runner/hello");
  assert.deepEqual(hello.body.host.capabilities_report, {
    mcp_pinning: true, runtime: "custom", delivery: "kernel", leases: [],
  });
  assert.deepEqual(Object.keys(hello.body), ["host"], "nothing outside host: the hello schema is strict");
});

// ------------------------------------------------------------------ runtimes (0.5.2)

const CMD_SHIM = `@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\\node.exe" (
  SET "_prog=%dp0%\\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*
`;

test("an npm .cmd shim on Windows runs its script with Node, no shell", () => {
  assert.equal(shimTarget(CMD_SHIM, "C:\\Users\\me\\AppData\\Roaming\\npm"),
    "C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\bin\\codex.js");
  assert.equal(shimTarget("@echo off\r\nsomething.exe %*\r\n", "C:\\x"), null, "only npm's shims are read");
  const files = {
    "C:\\Users\\me\\AppData\\Roaming\\npm\\codex.cmd": CMD_SHIM,
    "C:\\Users\\me\\.local\\bin\\claude.exe": "",
  };
  const fs = { platform: "win32", execPath: "C:\\node\\node.exe",
    env: { Path: "C:\\Windows;C:\\Users\\me\\.local\\bin;C:\\Users\\me\\AppData\\Roaming\\npm", PATHEXT: ".COM;.EXE;.BAT;.CMD" },
    exists: (file) => file in files, read: (file) => files[file] };
  assert.deepEqual(resolveCommand("codex", fs), { command: "C:\\node\\node.exe",
    prefix: ["C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\bin\\codex.js"] });
  assert.deepEqual(resolveCommand("claude", fs), { command: "C:\\Users\\me\\.local\\bin\\claude.exe", prefix: [] });
  assert.equal(resolveCommand("gemini", fs), null);
  assert.deepEqual(resolveCommand("codex", { platform: "linux" }), { command: "codex", prefix: [] });

  const seen = [];
  const version = commandWorks("codex", { resolve: (name) => resolveCommand(name, fs),
    run: (command, args) => { seen.push([command, ...args]); return "codex-cli 0.141.0\n"; } });
  assert.equal(version, "codex-cli 0.141.0");
  assert.deepEqual(seen, [["C:\\node\\node.exe",
    "C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\bin\\codex.js", "--version"]]);
});

test("inventory without OAR finds a runtime through its shim", async () => {
  const found = await inventory({ importer: noOar,
    resolve: (name) => (name === "codex" ? { command: "node", prefix: ["codex.js"] } : null),
    run: (command, args) => `${command} ${args.join(" ")}` });
  assert.deepEqual(found, [{ id: "claude", available: false },
    { id: "codex", available: true, via: "cli", version: "node codex.js --version" }]);
});

test("the runtime is the one this computer has", () => {
  const codexOnly = [{ id: "claude", available: false }, { id: "codex", available: true }];
  const both = [{ id: "claude", available: true }, { id: "codex", available: true }];
  const none = [{ id: "claude", available: false }, { id: "codex", available: false }];
  assert.deepEqual(chooseRuntime("", codexOnly), { runtime: "codex" });
  assert.deepEqual(chooseRuntime("", both), { runtime: "claude" });
  assert.deepEqual(chooseRuntime("codex", both), { runtime: "codex" });
  assert.equal(chooseRuntime("claude", codexOnly).runtime, null, "asked for by name: up stops");
  assert.match(chooseRuntime("claude", codexOnly).reason, /Claude Code is not installed/);
  assert.deepEqual(chooseRuntime("claude", codexOnly, { fallback: true }),
    { runtime: "codex", note: "Claude Code is not installed here; using Codex." });
  assert.equal(chooseRuntime("", none).runtime, null);
  assert.match(chooseRuntime("", none, { fallback: true }).reason, /neither Claude Code nor Codex/);
  assert.deepEqual(chooseRuntime("custom", none), { runtime: "custom" });
});

test("with no runtime, the install and sign-in steps for this system", () => {
  const windows = installSteps(null, { platform: "win32" });
  assert.ok(windows.some((line) => line.includes("npm install -g @openai/codex")));
  assert.ok(windows.some((line) => line.includes("npm install -g @anthropic-ai/claude-code")));
  assert.ok(windows.some((line) => line.includes("codex login")));
  assert.ok(windows.some((line) => /desktop apps do not count/.test(line)));
  assert.ok(windows.some((line) => /new terminal/.test(line)));
  const mac = installSteps("codex", { platform: "darwin" });
  assert.ok(!mac.some((line) => /Claude Code/.test(line)) && !mac.some((line) => /new terminal/.test(line)));
});

test("a hub uses the runtime that is installed, whatever was saved", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-state-"));
  for (const saved of ["", "claude"]) {
    const fake = await fakeVeynod();
    const said = [];
    await drive({ host: fake.host, key: "ag_box", runtime: saved, slots: 1 }, [],
      { version: "t", stateDir: state, importer: noOar,
        say: (line) => said.push(line),
        takeInventory: async () => [{ id: "claude", available: false }, { id: "codex", available: true, via: "cli" }] });
    fake.close();
    const hello = fake.calls.find((call) => call.path === "/v1/runner/hello");
    assert.equal(hello.body.host.capabilities_report.runtime, "codex", `saved ${saved || "nothing"}`);
    assert.equal(said.some((line) => line.includes("Claude Code is not installed here; using Codex.")),
      saved === "claude");
  }
});

test("a gate still working on the agent's own submission is not a failed placement", async () => {
  const { submitKernelDiff } = await import("../bin/runner.mjs");
  const fetchImpl = async () => ({ ok: false, status: 409,
    json: async () => ({ error: { code: "submission_in_progress", message: "wait" } }) });
  const result = await submitKernelDiff(fetchImpl, { host: "https://h", key: "k" }, "T-1", "diff");
  assert.equal(result.already_submitted, true);
  const refused = async () => ({ ok: false, status: 409,
    json: async () => ({ error: { code: "lease_not_active", message: "gone" } }) });
  await assert.rejects(submitKernelDiff(refused, { host: "https://h", key: "k" }, "T-1", "diff"),
    (error) => error.code === "lease_not_active");
});
