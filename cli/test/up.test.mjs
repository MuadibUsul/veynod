import assert from "node:assert/strict";
import { mkdtempSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { acquireLock, doctor, doctorOk, keyPath, loadConfig, saveConfig, storeKey, upPlan } from "../bin/up.mjs";
import { parseArgs, servicePlan } from "../bin/veynod.mjs";

test("up plans one identity, or two when the computer runs tasks", () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-up-"));
  const quiet = upPlan(parseArgs(["up", "--name", "Box 1"]), { home, clients: ["cursor"] });
  assert.deepEqual(quiet.identities.map((item) => [item.role, item.label, item.wants_role]),
    [["computer", "box_1", "EXECUTOR"]]);
  assert.equal(quiet.run_tasks, false, "no unattended runs without --run-tasks");
  assert.equal(quiet.hub.mode, "listen");

  const busy = upPlan(parseArgs(["up", "--run-tasks", "--name", "box", "--repo", "acme/site"]), { home });
  assert.deepEqual(busy.identities.map((item) => [item.role, item.label, item.wants_role]),
    [["computer", "box", "EXECUTOR"], ["review", "box-review", "AUDITOR"]]);
  assert.equal(busy.hub.mode, "runner");
  assert.deepEqual(busy.hub.repos, ["acme/site"]);
  assert.deepEqual(busy.files, [join(home, ".veynod", "config.json"), keyPath("box", home), keyPath("box-review", home)]);
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
  assert.equal(byId.reviewer.ok, false, "a computer that runs tasks needs its review identity");
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
