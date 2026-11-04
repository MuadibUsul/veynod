import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { onePokeFromArgs, workerPoke, workerRunnerOptions } from "../bin/runner.mjs";
import { Messenger, runComputer, workerCommand } from "../bin/veynod-messenger.mjs";

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
    repos: ["acme/site"], runtime: "codex" }, { createRunner });
  messenger.stop();
  assert.equal(seen.running, messenger.running);
  assert.equal(messenger.slots, 3);
  assert.deepEqual(messenger.runnerOptions, { repos: ["acme/site"], runtime: "codex", exec: "" });
});
