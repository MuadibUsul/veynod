// The worker process of a placement: up is not started, and started is not finished.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { begin, finish, placementOf, runPlacement } from "../bin/runner.mjs";

const RUNNER = join(dirname(fileURLToPath(import.meta.url)), "..", "bin", "runner.mjs");
const PLC = "plc_01M630S0A35N82N1TFY7XGSQ99";

/** A board that answers `start` (or has no such call) and records what it was asked. */
async function board({ start = { start: true, status: "offered", detail: { agenda: [{ kind: "stopped", task_key: "t7" }] } },
  hasStart = true, status = "offered" } = {}) {
  const calls = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      calls.push({ method: req.method, path: req.url, body: body ? JSON.parse(body) : null, at: Date.now() });
      res.setHeader("Content-Type", "application/json");
      if (req.url.endsWith("/start")) {
        if (!hasStart) {
          res.statusCode = 404;
          res.end(JSON.stringify({ error: { code: "not_found", message: "Not Found" } }));
        } else res.end(JSON.stringify(start));
      } else if (req.url === `/v1/runner/placements/${PLC}`) {
        res.end(JSON.stringify({ status, detail: { agenda: [] } }));
      } else res.end("{}");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { host: `http://127.0.0.1:${server.address().port}`, calls, close: () => server.close() };
}

const placed = placementOf({ placement_id: PLC, step: "supervise" });

test("the worker asks the board to start, with the lease epoch its poke carried", async () => {
  const fake = await board();
  const answer = await begin({ host: fake.host, key: "k" }, placementOf({ placement_id: PLC, task_key: "t", step: "work" }),
    { lease_epoch: 7 });
  fake.close();
  assert.equal(answer.start, true);
  assert.deepEqual(fake.calls.map((call) => [call.method, call.path, call.body]),
    [["POST", `/v1/runner/placements/${PLC}/start`, { lease_epoch: 7 }]]);
});

test("a placement that moved on is not started, and the reason comes back", async () => {
  const fake = await board({ start: { start: false, status: "moved", skip_reason: "placement is moved (nerve:QOS_TIMEOUT_EXEC_ACK_5000MS)" } });
  const answer = await begin({ host: fake.host, key: "k" }, placed, {});
  fake.close();
  assert.deepEqual(answer, { start: false, reason: "placement is moved (nerve:QOS_TIMEOUT_EXEC_ACK_5000MS)" });
});

test("a board without the start call is asked for the placement instead", async () => {
  const open = await board({ hasStart: false });
  assert.equal((await begin({ host: open.host, key: "k" }, placed, {})).start, true);
  open.close();
  const moved = await board({ hasStart: false, status: "moved" });
  assert.deepEqual(await begin({ host: moved.host, key: "k" }, placed, {}), { start: false, reason: "placement is moved" });
  moved.close();
});

test("a skipped placement runs nothing, reports nothing and leaves a skipped receipt with the reason", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-worker-"));
  const fake = await board({ start: { start: false, status: "moved", skip_reason: "placement is moved (timed_out)" } });
  const result = await runPlacement({ host: fake.host, key: "k", runtime: "custom", exec: "node -e \"process.exit(9)\"" },
    { placement_id: PLC, step: "supervise" }, { stateDir: state, version: "t" });
  fake.close();
  assert.deepEqual(result, { outcome: "skipped", reason: "placement is moved (timed_out)" });
  assert.deepEqual(fake.calls.map((call) => call.path), [`/v1/runner/placements/${PLC}/start`]);
  const receipts = readFileSync(join(state, "runner-receipts.jsonl"), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(receipts.map((item) => [item.stage, item.reason]), [["skipped", "placement is moved (timed_out)"]]);
});

test("a worker ends by closing its channel, never by exiting at once", () => {
  const events = [];
  const proc = { connected: true, exitCode: undefined,
    disconnect() { events.push("disconnect"); this.connected = false; },
    exit(code) { events.push(`exit ${code}`); } };
  const timer = finish(3, { proc, graceMs: 60_000 });
  clearTimeout(timer);
  assert.equal(proc.exitCode, 3);
  assert.deepEqual(events, ["disconnect"], "no process.exit() while Node still has work in flight");
  // Already disconnected: nothing to close, nothing thrown.
  finish(0, { proc: { connected: false, exit() {} }, graceMs: 1 });
});

/** Start a real standby worker and collect what it says and how it ends. */
function standby(host, state, exec) {
  const child = spawn(process.execPath, [RUNNER, "--standby"], {
    stdio: ["ignore", "ignore", "pipe", "ipc"],
    env: { ...process.env, VEYNOD_HOST: host, VEYNOD_AGENT_TOKEN: "ag_box", VEYNOD_AGENT_ID: "box",
      VEYNOD_STATE_DIR: state, VEYNOD_RUNNER_OPTIONS: JSON.stringify({ runtime: "custom", exec }) },
  });
  const messages = [];
  const waiting = [];
  child.on("message", (message) => {
    const stamped = { ...message, at: Date.now() };
    messages.push(stamped);
    for (const item of [...waiting]) {
      if (message.type === item.type) {
        waiting.splice(waiting.indexOf(item), 1);
        item.resolve(stamped);
      }
    }
  });
  const said = (type) => new Promise((resolve) => {
    const seen = messages.find((message) => message.type === type);
    if (seen) resolve(seen);
    else waiting.push({ type, resolve });
  });
  const ended = new Promise((resolve) => child.on("exit", (code, signal) => resolve({ code, signal })));
  return { child, messages, said, ended };
}

test("a prepared worker: ready at once, started after the board's check, reported, then gone on its own", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-worker-"));
  const fake = await board();
  const worker = standby(fake.host, state, "node -e \"process.exit(0)\"");
  await worker.said("IPC_STANDBY_READY");
  const handed = Date.now();
  worker.child.send({ type: "IPC_PLACEMENT", payload: { placement_id: PLC, step: "supervise", lease_epoch: 0 } });
  const ready = await worker.said("IPC_ORIENTATION_READY");
  const started = await worker.said("IPC_EXECUTION_STARTED");
  await worker.said("IPC_REPORTED");
  const end = await worker.ended;
  fake.close();
  assert.deepEqual(end, { code: 0, signal: null });
  // The three points in order, each its own message.
  const types = worker.messages.map((message) => message.type);
  assert.deepEqual(types, ["IPC_STANDBY_READY", "IPC_ORIENTATION_READY", "IPC_EXECUTION_STARTED", "IPC_REPORTED"]);
  // Nothing to load: a prepared worker is ready and started well inside the targets.
  assert.ok(ready.at - handed < 1_000, `ready after ${ready.at - handed}ms`);
  assert.ok(started.at - handed < 2_000, `started after ${started.at - handed}ms`);
  // The board was asked to start before anything else, and got the report at the end.
  assert.equal(fake.calls[0].path, `/v1/runner/placements/${PLC}/start`);
  const report = fake.calls.find((call) => call.path.endsWith("/report"));
  assert.equal(report.body.outcome, "finished");
});

test("a prepared worker whose placement moved on does nothing, says skipped and exits 0 without a report", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-worker-"));
  const fake = await board({ start: { start: false, status: "moved", skip_reason: "placement is moved (host_offline)" } });
  const worker = standby(fake.host, state, "node -e \"process.exit(7)\"");
  await worker.said("IPC_STANDBY_READY");
  worker.child.send({ type: "IPC_PLACEMENT", payload: { placement_id: PLC, step: "supervise" } });
  const skipped = await worker.said("IPC_SKIPPED");
  const end = await worker.ended;
  fake.close();
  assert.equal(skipped.reason, "placement is moved (host_offline)");
  assert.deepEqual(end, { code: 0, signal: null });
  assert.equal(worker.messages.some((message) => message.type === "IPC_EXECUTION_STARTED"), false);
  assert.equal(fake.calls.some((call) => call.path.endsWith("/report")), false);
});

test("a standby worker whose messenger goes away ends on its own", async () => {
  const state = mkdtempSync(join(tmpdir(), "veynod-worker-"));
  const worker = standby("http://127.0.0.1:9", state, "true");
  await worker.said("IPC_STANDBY_READY");
  worker.child.disconnect();
  assert.deepEqual(await worker.ended, { code: 0, signal: null });
});

test("a task's command checks run in the work directory and say how they ended", async () => {
  const { runCommandChecks } = await import("../bin/runner.mjs");
  const dir = mkdtempSync(join(tmpdir(), "veynod-checks-"));
  const node = JSON.stringify(process.execPath);
  const task = { acceptance: [
    { id: "here", kind: "AUTO", check: { type: "command", run: `${node} -e "require('fs').accessSync('marker')"` } },
    { id: "fails", kind: "AUTO", check: { type: "command", run: `${node} -e "console.error('2 failed'); process.exit(3)"` } },
    { id: "slow", kind: "AUTO", check: { type: "command", run: `${node} -e "setTimeout(() => {}, 5000)"`, timeout_s: 1 } },
    { id: "person", kind: "MANUAL", text: "a person reads it" },
    { id: "doc", kind: "AUTO", check: { type: "document_exists", doc_key: "x" } },
  ] };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(join(dir, "marker"), "");
  const results = await runCommandChecks(task, dir);
  assert.deepEqual(results.map((item) => [item.id, item.passed]), [["here", true], ["fails", false], ["slow", false]]);
  assert.match(results[1].detail, /^exit 3: 2 failed/);
  assert.match(results[2].detail, /timed out after 1 s/);
  // Work from another workspace runs no command here.
  assert.deepEqual(await runCommandChecks({ ...task, input_context: { federation: { job: "x" } } }, dir), []);
  assert.deepEqual(await runCommandChecks(null, dir), []);
});
