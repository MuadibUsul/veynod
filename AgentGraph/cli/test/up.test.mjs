import assert from "node:assert/strict";
import { mkdtempSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { acquireLock, agentName, doctor, doctorOk, hubWorkers, keyPath, loadConfig, reuseIdentities, saveConfig, storeKey, upPlan } from "../bin/up.mjs";
import { connect, parseArgs, sealForTest, servicePlan, settledDoctor } from "../bin/veynod.mjs";

test("up plans one identity per computer, whatever the slots", () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-up-"));
  const quiet = upPlan(parseArgs(["up", "--name", "Box 1"]), { home, clients: ["cursor"] });
  assert.deepEqual(quiet.identities.map((item) => [item.role, item.label, item.wants_role]),
    [["computer", "box-1", "EXECUTOR"]]);
  assert.equal(quiet.run_tasks, false, "no unattended runs without --run-tasks");
  assert.equal(quiet.hub.mode, "listen");

  const busy = upPlan(parseArgs(["up", "--run-tasks", "--slots", "1", "--name", "box", "--repo", "acme/site"]), { home });
  assert.deepEqual(busy.identities.map((item) => [item.role, item.label, item.wants_role]),
    [["computer", "box", "EXECUTOR"]]);
  assert.equal(busy.workers, 1);
  assert.equal(busy.hub.mode, "runner");
  assert.deepEqual(busy.hub.repos, ["acme/site"]);
  assert.deepEqual(busy.files, [join(home, ".veynod", "config.json"), keyPath("box", home)]);

  // More slots are more workers on the same identity, never a second "-review" one.
  const wider = upPlan(parseArgs(["up", "--run-tasks", "--slots", "3", "--name", "box"]), { home });
  assert.deepEqual(wider.identities.map((item) => item.role), ["computer"]);
  assert.equal(wider.hub.slots, 3);
});

test("a stored -review identity from an older setup is never started", () => {
  const workers = hubWorkers({
    run_tasks: true,
    slots: 4,
    identities: [
      { role: "computer", agent_id: "grok-bot-vm-169236189" },
      { role: "review", agent_id: "grok-bot-vm-169236189-review" },
    ],
  });
  assert.deepEqual(workers.map((item) => item.agent_id), ["grok-bot-vm-169236189"]);
});

test("identity names are valid agent ids with room for -review", () => {
  assert.equal(agentName("Box.Lab"), "box-lab");
  assert.equal(agentName("_9srv"), "srv");
  assert.equal(agentName("7"), "computer");
  assert.equal(agentName("x".repeat(80)).length, 33);
  for (const name of ["MacBook-Pro.local", "ip-10-0-0-1", "box"]) {
    assert.match(`${agentName(name)}-review`, /^[a-z][a-z0-9-]{2,39}$/);
  }
});

test("keys and the config are private files", () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-up-"));
  const file = storeKey("box", "agt_secret", home);
  assert.equal(statSync(file).mode & 0o777, 0o600);
  saveConfig({ host: "https://h", identities: [{ role: "computer", agent_id: "box", key_file: file }] }, home);
  assert.equal(statSync(join(home, ".veynod", "config.json")).mode & 0o777, 0o600);
  assert.equal(loadConfig(home).identities[0].agent_id, "box");
  assert.equal(loadConfig(home).version, 1);
});

test("one hub per key: a second process on the same key is refused", () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-up-"));
  const release = acquireLock("box", { home, pid: 111, alive: () => true });
  assert.throws(() => acquireLock("box", { home, pid: 222, alive: () => true }), /another hub \(pid 111\)/);
  // A lock left by a process that died is taken over.
  acquireLock("box", { home, pid: 333, alive: () => false });
  release();
});

test("the hub is one service, started without a key on its command line", () => {
  const plan = servicePlan("install", { os: "linux", home: "/home/u", node: "/usr/bin/node",
    script: "/opt/veynod.mjs", host: "https://veynod.com", mode: "hub" });
  assert.equal(plan.files[0].path, "/home/u/.config/systemd/user/veynod-hub.service");
  assert.match(plan.files[0].content, /ExecStart="\/usr\/bin\/node" "\/opt\/veynod.mjs" "hub"/);
  assert.doesNotMatch(plan.files[0].content, /--key/);
});

test("doctor: not connected, then what the computer and the board are missing", async () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-up-"));
  const none = await doctor({ home });
  assert.equal(doctorOk(none), false);
  assert.match(none[0].fix, /npx veynod up/);

  const server = createServer((req, res) => {
    res.setHeader("Content-Type", "application/json");
    if (req.url === "/v1/whoami") {
      res.statusCode = req.headers.authorization === "Bearer agt_box" ? 200 : 401;
      res.end("{}");
    } else if (req.url === "/v1/runner/doctor") {
      res.end(JSON.stringify({ ok: false, checks: [
        { id: "automation", ok: true, required: true, text: "Automation is full." },
        { id: "reviewer", ok: false, required: true, text: "No online computer can review.", fix: "Approve -review." },
      ] }));
    } else {
      res.statusCode = 404;
      res.end("{}");
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const host = `http://127.0.0.1:${server.address().port}`;
  const box = storeKey("box", "agt_box", home);
  const config = { host, run_tasks: true, runtime: "custom", exec: "true", repos: [],
    identities: [{ role: "computer", agent_id: "box", key_file: box }] };
  const checks = await doctor({ home, config, nodeVersion: "22.1.0", run: () => "" });
  server.close();
  const byId = Object.fromEntries(checks.map((item) => [item.id, item]));
  assert.equal(byId["key:box"].ok, true);
  assert.equal(byId.node.ok, false);
  assert.equal(byId.node.required, false, "old Node falls back to the runtime's CLI");
  assert.equal(byId.reviewer.ok, true, "the Kernel gate and a person review; no review twin");
  assert.match(byId.reviewer.text, /Kernel gate/);
  assert.equal(byId["board:reviewer"].ok, false);
  assert.equal(byId["board:reviewer"].fix, "Approve -review.");
  assert.equal(doctorOk(checks), false);

  // With the review identity and a board that has everything, the doctor is green.
  const review = storeKey("box-review", "agt_box", home);
  config.identities.push({ role: "review", agent_id: "box-review", key_file: review });
  writeFileSync(join(home, "unused"), "");
  const green = await doctor({ home, config, nodeVersion: "24.0.0", run: () => "",
    fetchImpl: async (url) => ({ status: 200, json: async () => (url.endsWith("/doctor")
      ? { checks: [{ id: "reviewer", ok: true, required: true, text: "1 computer(s) can review." }] } : {}) }) });
  assert.equal(doctorOk(green), true, JSON.stringify(green));
});

async function deviceFlow({ legacy = false } = {}) {
  const seen = [];
  let publicRaw = null;
  let asked = [];
  const server = createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      const body = raw ? JSON.parse(raw) : {};
      seen.push({ url: req.url, body });
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/v1/auth/cli/sessions") {
        if (legacy && body.identities) {
          res.statusCode = 422;
          res.end(JSON.stringify({ error: { code: "validation_error", message: "extra fields" } }));
          return;
        }
        publicRaw = Buffer.from(body.public_key, "base64");
        asked = body.identities || [];
        res.statusCode = 201;
        res.end(JSON.stringify({ device_code: "d".repeat(43), user_code: "ABCD-EFGH", expires_in: 60, interval: 0.01 }));
      } else {
        // The owner approves everything the terminal asked for.
        res.end(JSON.stringify({ status: "approved", agent_signature: "box", sealed_key: sealForTest("agt_box", publicRaw),
          identities: asked.slice(1).map((item) => ({ name: item.name, agent_signature: item.name,
            role_scope: item.role_scope, sealed_key: sealForTest(`agt_${item.name}`, publicRaw) })),
          workspace: { name: "acme" }, mcp_path: "/mcp/" }));
      }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { server, seen, host: `http://127.0.0.1:${server.address().port}` };
}

test("one approval connects the computer and its reviewer", async () => {
  const { server, seen, host } = await deviceFlow();
  const result = await connect({ host, open: false }, { label: "box", identities: [
    { name: "box", role_scope: "EXECUTOR" }, { name: "box-review", role_scope: "AUDITOR", why: "reviews" }] });
  server.close();
  assert.equal(seen[0].body.identities.length, 2, "the terminal says what it wants");
  assert.equal(result.key, "agt_box");
  assert.equal(result.legacy, false);
  assert.deepEqual(result.extra, [{ name: "box-review", agent_id: "box-review", role_scope: "AUDITOR", key: "agt_box-review" }]);
});

test("an older server mints one identity per code", async () => {
  const { server, seen, host } = await deviceFlow({ legacy: true });
  const result = await connect({ host, open: false }, { label: "box", identities: [
    { name: "box", role_scope: "EXECUTOR" }, { name: "box-review", role_scope: "AUDITOR" }] });
  server.close();
  assert.equal(result.legacy, true);
  assert.equal(result.key, "agt_box");
  assert.deepEqual(result.extra, []);
  assert.equal(seen.filter((item) => item.url === "/v1/auth/cli/sessions").length, 2, "asked again without identities");
});

test("up waits for the new hub's hello, never for what this computer lacks", async () => {
  const red = (id) => ({ id, ok: false, required: true, text: id });
  const runs = [[red("board:hosts")], [red("board:hosts")], [{ id: "board:hosts", ok: true, required: true }]];
  let calls = 0;
  const green = await settledDoctor(async () => runs[Math.min(calls++, 2)], { doctorOk, waitMs: 1 });
  assert.equal(calls, 3);
  assert.equal(doctorOk(green), true);

  calls = 0;
  const local = await settledDoctor(async () => { calls += 1; return [red("runtime"), red("board:hosts")]; },
    { doctorOk, waitMs: 1 });
  assert.equal(calls, 1, "a missing runtime is reported at once");
  assert.equal(doctorOk(local), false);

  calls = 0;
  await settledDoctor(async () => { calls += 1; return [red("board:automation")]; }, { doctorOk, waitMs: 1 });
  assert.equal(calls, 1, "no hello turns automation on");
});

test("the doctor counts whichever runtime is installed when none was chosen", async () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-home-"));
  const offline = async () => { throw new Error("offline"); };
  const codexOnly = (command) => {
    if (command === "claude") throw new Error("not found");
    return "codex-cli 0.141.0";
  };
  for (const [runtime, run, ok, text] of [
    ["", codexOnly, true, "Codex is installed."],
    ["claude", codexOnly, false, "Claude Code is not installed or not on PATH."],
    ["", () => { throw new Error("not found"); }, false, "Claude Code or Codex is not installed or not on PATH."],
  ]) {
    const config = { host: "http://127.0.0.1:9", run_tasks: true, runtime, repos: [], identities: [] };
    const checks = await doctor({ home, config, nodeVersion: "24.0.0", run, fetchImpl: offline });
    const check = checks.find((item) => item.id === "runtime");
    assert.equal(check.ok, ok, runtime);
    assert.equal(check.text, text);
    if (!ok) assert.match(check.fix, /command-line tool, not the desktop app/);
  }
});

test("an identity the plan no longer wants is parked, not forgotten", () => {
  const plan = { identities: [{ role: "computer", label: "box" }] };
  const previous = { host: "https://veynod.com", identities: [
    { role: "computer", agent_id: "box", key_file: "/k/box.key" },
    { role: "review", agent_id: "box-review", key_file: "/k/box-review.key" },
  ] };
  const exists = () => true;
  const reused = reuseIdentities(plan, previous, { host: "https://veynod.com", exists });
  assert.deepEqual(reused.identities.map((item) => item.agent_id), ["box"]);
  assert.deepEqual(reused.missing, []);
  assert.deepEqual(reused.parked.map((item) => item.agent_id), ["box-review"]);
  // Asking for the twin again reuses the parked key: no box-review-2 on the board.
  const twin = { identities: [...plan.identities, { role: "review", label: "box-review" }] };
  const again = reuseIdentities(twin, previous, { host: "https://veynod.com", exists });
  assert.deepEqual(again.identities.map((item) => item.agent_id), ["box", "box-review"]);
  assert.deepEqual(again.missing, []);
  // Another board, --fresh, or a lost key file asks again.
  assert.equal(reuseIdentities(plan, previous, { host: "https://other.example", exists }).missing.length, 1);
  assert.equal(reuseIdentities(plan, previous, { host: "https://veynod.com", fresh: true, exists }).missing.length, 1);
  assert.equal(reuseIdentities(plan, previous, { host: "https://veynod.com", exists: () => false }).missing.length, 1);
  // The hub never starts a parked review seat on slots=1, in either mode.
  for (const run_tasks of [true, false]) {
    const workers = hubWorkers({ run_tasks, slots: 1, identities: previous.identities });
    assert.deepEqual(workers.map((item) => item.agent_id), ["box"]);
  }
});

test("a parked identity the board refuses does not turn the doctor red", async () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-park-"));
  const config = { host: "https://veynod.test", run_tasks: true, slots: 1, delivery: "kernel", runtime: "custom",
    exec: "true", identities: [
      { role: "computer", agent_id: "box", key_file: storeKey("box", "agt_box", home) },
      { role: "review", agent_id: "box-review", key_file: storeKey("box-review", "agt_review", home) },
    ] };
  const fetchImpl = async (url, init) => {
    const ok = init.headers.Authorization === "Bearer agt_box";
    return { status: url.endsWith("/v1/whoami") ? (ok ? 200 : 401) : 404, json: async () => ({}) };
  };
  const checks = await doctor({ home, config, fetchImpl, run: () => "", nodeVersion: "24.0.0" });
  const parked = checks.find((item) => item.id === "key:box-review");
  assert.equal(parked.ok, false);
  assert.equal(parked.required, false);
  assert.ok(doctorOk(checks), JSON.stringify(checks.filter((item) => !item.ok)));
});
