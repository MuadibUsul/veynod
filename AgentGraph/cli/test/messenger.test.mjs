import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { onePokeFromArgs, workerPoke, workerRunnerOptions } from "../bin/runner.mjs";
import { Messenger, laneOf, runComputer, workerCommand } from "../bin/veynod-messenger.mjs";

const payload = { task_key: "T-1", lease_epoch: 4, placement_id: "plc_abc123", step: "work" };

test("a nerve worker talks to the messenger's board and works as this computer", () => {
  const { args, env } = workerCommand({
    runner: "/r/runner.mjs", payload, token: "agt_x", agentId: "box",
    host: "https://veynod.example",
    runnerOptions: { repos: ["acme/site"], runtime: "codex" },
  });
  assert.equal(env.VEYNOD_HOST, "https://veynod.example");
  assert.deepEqual(args.slice(1), ["--placement", "plc_abc123", "--task", "T-1", "--step", "work", "--epoch", "4"]);
  assert.deepEqual(workerRunnerOptions(env), { repos: ["acme/site"], runtime: "codex" });
  // The worker reads the whole frame (step, diagram chunk) back from its environment.
  const review = { placement_id: "plc_r", step: "review", task_key: "DG-x-api", diagram_key: "x", chunk_key: "api" };
  const fromEnv = workerPoke(["--placement", "plc_r", "--task", "DG-x-api", "--step", "review"],
    { VEYNOD_TASK_POKE: JSON.stringify(review) });
  assert.equal(fromEnv.diagram_key, "x");
  assert.equal(onePokeFromArgs(["--placement", "plc_s", "--step", "supervise"]).task_key, null);
});

test("broken runner options are ignored, not fatal", () => {
  assert.deepEqual(workerRunnerOptions({ VEYNOD_RUNNER_OPTIONS: "{not json" }), {});
  assert.deepEqual(workerRunnerOptions({}), {});
  assert.deepEqual(workerRunnerOptions({ VEYNOD_RUNNER_OPTIONS: JSON.stringify({ repos: "x", slots: 9 }) }), {});
});

/** A messenger with a socket that records frames and a spawner that records workers. */
function wired(slots) {
  const sent = [];
  const spawned = [];
  const spawner = (command, args) => {
    const child = new EventEmitter();
    child.pid = 1000 + spawned.length;
    child.kill = () => child.emit("exit", null, "SIGKILL");
    spawned.push({ args, child });
    return child;
  };
  const messenger = new Messenger({ host: "https://h", key: "agt_x", agentId: "box", slots, spawner });
  messenger.ws = { readyState: WebSocket.OPEN, send: (data) => sent.push(JSON.parse(data)), close() {} };
  return { messenger, sent, spawned };
}

const poke = (id, task) => ({ type: "task.poke", frame_id: id,
  payload: { placement_id: id, task_key: task, step: "work", lease_epoch: 1, orientation_timeout_ms: 60_000 } });

test("slots bound the workers: a full computer refuses at once, a repeated frame is ignored", () => {
  const { messenger, sent, spawned } = wired(2);
  messenger.onFrame(poke("plc_1", "a"));
  messenger.onFrame(poke("plc_1", "a"));
  messenger.onFrame(poke("plc_2", "b"));
  messenger.onFrame(poke("plc_3", "c"));
  assert.equal(spawned.length, 2);
  assert.deepEqual(sent.filter((frame) => frame.type === "task.ack.net").map((frame) => frame.frame_id), ["plc_1", "plc_2"]);
  const busy = sent.find((frame) => frame.type === "task.busy.reject");
  assert.equal(busy.frame_id, "plc_3");
  // The ping says how full the computer is, so the hub stops poking it.
  const ping = sent.filter((frame) => frame.type === "sys.ping").at(-1);
  assert.deepEqual([ping.payload.slots, ping.payload.active_count, ping.payload.worker_busy], [2, 2, true]);
  assert.deepEqual([...messenger.running.keys()], ["a", "b"]);

  // The second receipt names the worker; a finished worker frees its slot.
  spawned[0].child.emit("message", { type: "IPC_ORIENTATION_READY" });
  assert.equal(sent.find((frame) => frame.type === "task.ack.exec").payload.worker_pid, 1000);
  spawned[0].child.emit("exit", 0, null);
  assert.equal(messenger.full, false);
  assert.deepEqual([...messenger.running.keys()], ["b"]);
  messenger.stop();
});

test("a revoked lease kills only that task's worker", () => {
  const { messenger, spawned } = wired(2);
  messenger.onFrame(poke("plc_1", "a"));
  messenger.onFrame(poke("plc_2", "b"));
  messenger.onFrame({ type: "lease.revoked", payload: { task_key: "a", lease_epoch: 1, reason: "moved" } });
  assert.deepEqual([...messenger.running.keys()], ["b"]);
  assert.equal(spawned.length, 2);
  messenger.stop();
});

test("runComputer keeps the host record with the messenger's own view of what runs", async () => {
  let seen = null;
  const createRunner = async (options) => {
    seen = options;
    return { hello: async () => ({}), beat: async () => {} };
  };
  const messenger = await runComputer({ host: "http://127.0.0.1:9", key: "agt_x", agent: "box", slots: 3,
    repos: ["acme/site"], runtime: "codex", warm: 0 }, { createRunner });
  messenger.stop();
  assert.equal(seen.running, messenger.running);
  assert.equal(messenger.slots, 3);
  assert.deepEqual(messenger.runnerOptions, { repos: ["acme/site"], runtime: "codex", exec: "" });
});

// ------------------------------------------------------------------ start failures

const rejects = (sent) => sent.filter((frame) => frame.type === "task.busy.reject")
  .map((frame) => [frame.frame_id, frame.payload.reason]);

test("a worker that cannot start is refused to the hub at once, with the reason, and its slot is free", () => {
  const { messenger, sent, spawned } = wired(1);
  // The spawn fails later, as an event: unhandled, it would take the hub down.
  messenger.onFrame(poke("plc_1", "a"));
  spawned[0].child.emit("error", new Error("spawn node ENOENT"));
  assert.deepEqual(rejects(sent), [["plc_1", "WORKER_START_FAILED: spawn node ENOENT"]]);
  assert.equal(messenger.full, false);
  assert.deepEqual([...messenger.running.keys()], []);

  // It exits while starting (a crash on the way up): said at once, not at the ACK timeout.
  messenger.onFrame(poke("plc_2", "b"));
  spawned[1].child.emit("exit", 3221226505, null);
  assert.deepEqual(rejects(sent).at(-1), ["plc_2", "WORKER_EXITED_BEFORE_READY: code=3221226505, signal=null"]);
  assert.equal(messenger.full, false);

  // The next round is not refused for anything left behind.
  messenger.onFrame(poke("plc_3", "c"));
  assert.equal(spawned.length, 3);
  assert.equal(sent.filter((frame) => frame.type === "task.ack.net").length, 3);
  messenger.stop();
});

test("a spawn that throws is refused with its reason", () => {
  const sent = [];
  const messenger = new Messenger({ host: "https://h", key: "agt_x", agentId: "box",
    spawner: () => { throw new Error("EAGAIN"); } });
  messenger.ws = { readyState: WebSocket.OPEN, send: (data) => sent.push(JSON.parse(data)), close() {} };
  messenger.onFrame(poke("plc_1", "a"));
  assert.deepEqual(rejects(sent), [["plc_1", "WORKER_START_FAILED: EAGAIN"]]);
  assert.equal(messenger.workers.size, 0);
});

test("the watchdog uses the hub's budget and refuses before it kills", async () => {
  const { messenger, sent, spawned } = wired(1);
  const frame = poke("plc_1", "a");
  frame.payload.orientation_timeout_ms = 30;
  messenger.onFrame(frame);
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.deepEqual(rejects(sent), [["plc_1", "ORIENTATION_TIMEOUT: no worker after 30ms"]]);
  assert.equal(messenger.workers.size, 0);
  // The kill's own exit event adds nothing: one refusal, one reason.
  assert.equal(sent.filter((item) => item.type === "task.worker.exit").length, 0);
  assert.equal(spawned.length, 1);
  messenger.stop();
});

test("a worker that ends without reporting is reported; one that reported is not a failure", () => {
  const { messenger, sent, spawned } = wired(2);
  messenger.onFrame(poke("plc_1", "a"));
  messenger.onFrame(poke("plc_2", "b"));
  for (const item of spawned) item.child.emit("message", { type: "IPC_ORIENTATION_READY" });
  spawned[0].child.emit("exit", 134, null);
  spawned[1].child.emit("message", { type: "IPC_REPORTED", outcome: "finished" });
  spawned[1].child.emit("exit", 0, null);
  const exits = sent.filter((frame) => frame.type === "task.worker.exit").map((frame) => frame.payload);
  assert.deepEqual(exits.map((item) => [item.placement_id, item.code, item.reported]),
    [["plc_1", 134, false], ["plc_2", 0, true]]);
  assert.equal(rejects(sent).length, 0);
  messenger.stop();
});

// ------------------------------------------------------------------ lanes

const round = (id) => ({ type: "task.poke", frame_id: id,
  payload: { placement_id: id, task_key: "", step: "supervise", lease_epoch: 0, orientation_timeout_ms: 60_000 } });

test("a supervise round has its own lane beside a full work slot", () => {
  assert.deepEqual(["work", "review", "merge", "deploy", "supervise"].map(laneOf),
    ["work", "work", "work", "work", "supervise"]);
  const { messenger, sent, spawned } = wired(1);
  messenger.onFrame(poke("plc_1", "a"));
  assert.equal(messenger.full, true);
  // The one slot is taken by a long task; the round is still started.
  messenger.onFrame(round("plc_r1"));
  assert.equal(spawned.length, 2);
  // One round at a time; a second task is still refused.
  messenger.onFrame(round("plc_r2"));
  messenger.onFrame(poke("plc_2", "b"));
  assert.deepEqual(rejects(sent), [["plc_r2", "WORKER_ALREADY_BUSY"], ["plc_2", "WORKER_ALREADY_BUSY"]]);
  const ping = sent.filter((frame) => frame.type === "sys.ping").at(-1).payload;
  assert.deepEqual([ping.slots, ping.active_count, ping.supervise_slots, ping.supervising], [1, 1, 1, 1]);
  // The heartbeat lists the task, not the round: the round takes no slot.
  assert.deepEqual([...messenger.running.keys()], ["a"]);

  // A round is revoked by its placement, which the task key cannot name.
  messenger.onFrame({ type: "lease.revoked", payload: { task_key: "", lease_epoch: 0, reason: "moved",
    placement_id: "plc_r1" } });
  assert.deepEqual([...messenger.workers.keys()], ["plc_1"]);
  messenger.stop();
});

// ------------------------------------------------------------------ standby workers

/** A messenger whose spawner makes standby children that say they are ready. */
function warmed(slots, warm) {
  const sent = [];
  const spawned = [];
  const spawner = (command, args) => {
    const child = new EventEmitter();
    child.pid = 2000 + spawned.length;
    child.connected = true;
    child.inbox = [];
    child.send = (message) => child.inbox.push(message);
    child.kill = () => child.emit("exit", null, "SIGKILL");
    spawned.push({ args, child });
    return child;
  };
  const messenger = new Messenger({ host: "https://h", key: "agt_x", agentId: "box", slots, warm, spawner,
    exists: () => true });
  messenger.ws = { readyState: WebSocket.OPEN, send: (data) => sent.push(JSON.parse(data)), close() {} };
  return { messenger, sent, spawned };
}

test("a prepared worker takes the placement over IPC: no process starts for the poke", async () => {
  const { messenger, sent, spawned } = warmed(1, 2);
  const first = poke("plc_1", "a");
  // No standby yet: this one starts cold and the pool fills behind it.
  messenger.onFrame(first);
  assert.equal(spawned[0].args.includes("--placement"), true);
  const standby = spawned.filter((item) => item.args.includes("--standby"));
  assert.equal(standby.length, 2);
  for (const item of standby) item.child.emit("message", { type: "IPC_STANDBY_READY" });
  spawned[0].child.emit("message", { type: "IPC_ORIENTATION_READY" });
  spawned[0].child.emit("message", { type: "IPC_REPORTED" });
  spawned[0].child.emit("exit", 0, null);

  const before = spawned.length;
  messenger.onFrame(poke("plc_2", "b"));
  // Handed to a prepared process; the pool is topped up with a new standby one.
  assert.deepEqual(standby[0].child.inbox.map((message) => [message.type, message.payload.placement_id]),
    [["IPC_PLACEMENT", "plc_2"]]);
  assert.equal(spawned.slice(before).every((item) => item.args.includes("--standby")), true);
  standby[0].child.emit("message", { type: "IPC_ORIENTATION_READY" });
  const acks = sent.filter((frame) => frame.type === "task.ack.exec").map((frame) => [frame.frame_id, frame.payload.warm]);
  assert.deepEqual(acks, [["plc_1", false], ["plc_2", true]]);
  messenger.stop();
  assert.equal(messenger.standby.length, 0);
});

test("standby workers that keep dying turn the pool off instead of looping", () => {
  const { messenger, spawned } = warmed(1, 1);
  const said = [];
  messenger.say = (line) => said.push(line);
  const standbys = () => spawned.filter((item) => item.args.includes("--standby"));
  for (let turn = 0; turn < 4; turn += 1) {
    // Each placement starts cold and tries to prepare a standby worker behind it.
    messenger.onFrame(poke(`plc_${turn}`, `t${turn}`));
    const worker = spawned.filter((item) => item.args.includes("--placement")).at(-1).child;
    if (turn < 3) standbys().at(-1).child.emit("exit", 1, null);
    worker.emit("message", { type: "IPC_ORIENTATION_READY" });
    worker.emit("message", { type: "IPC_REPORTED" });
    worker.emit("exit", 0, null);
  }
  assert.equal(standbys().length, 3, "after three failures in a row no more are started");
  assert.ok(said.some((line) => line.includes("standby workers keep failing")), said.join("\n"));
  messenger.stop();
});

// ------------------------------------------------------------------ drain and a missing runner

test("a draining hub takes nothing new and resolves when its workers have ended", async () => {
  const { messenger, sent, spawned } = wired(2);
  messenger.onFrame(poke("plc_1", "a"));
  spawned[0].child.emit("message", { type: "IPC_ORIENTATION_READY" });
  let drained = false;
  const done = messenger.drain().then(() => { drained = true; });
  assert.equal(sent.filter((frame) => frame.type === "sys.ping").at(-1).payload.draining, true);
  messenger.onFrame(poke("plc_2", "b"));
  assert.deepEqual(rejects(sent), [["plc_2", "HUB_DRAINING"]]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(drained, false, "the running task is not cut short");
  spawned[0].child.emit("message", { type: "IPC_REPORTED" });
  spawned[0].child.emit("exit", 0, null);
  await done;
  assert.equal(drained, true);
  // With nothing running it resolves at once.
  await new Messenger({ host: "https://h", key: "k" }).drain();
});

test("a hub whose runner file is gone refuses with that reason and asks to be restarted", () => {
  const sent = [];
  const spawned = [];
  let missing = null;
  const messenger = new Messenger({ host: "https://h", key: "agt_x", agentId: "box", runner: "/gone/runner.mjs",
    spawner: () => { spawned.push(1); return new EventEmitter(); }, exists: () => false,
    onRunnerMissing: (file) => { missing = file; } });
  messenger.ws = { readyState: WebSocket.OPEN, send: (data) => sent.push(JSON.parse(data)), close() {} };
  messenger.onFrame(poke("plc_1", "a"));
  assert.deepEqual(rejects(sent), [["plc_1", "RUNNER_MISSING: /gone/runner.mjs"]]);
  assert.equal(spawned.length, 0);
  assert.equal(missing, "/gone/runner.mjs");
});
