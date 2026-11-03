import assert from "node:assert/strict";
import { test } from "node:test";

import { onePokeFromArgs, workerRunnerOptions } from "../bin/runner.mjs";
import { workerCommand } from "../bin/veynod-messenger.mjs";

const payload = { task_key: "T-1", lease_epoch: 4, placement_id: "plc_abc123" };

test("a nerve worker talks to the messenger's board and says hello as this computer", () => {
  const { args, env } = workerCommand({
    runner: "/r/runner.mjs", payload, token: "agt_x", agentId: "box",
    host: "https://veynod.example",
    runnerOptions: { repos: ["acme/site"], runtime: "codex", delivery: "kernel" },
  });
  assert.equal(env.VEYNOD_HOST, "https://veynod.example");
  assert.deepEqual(args.slice(-2), ["--placement", "plc_abc123"]);
  assert.deepEqual(workerRunnerOptions(env), { repos: ["acme/site"], runtime: "codex", delivery: "kernel" });
});

test("the one-poke worker knows its placement id (plc_…), not a guessed p- prefix", () => {
  const poke = onePokeFromArgs(["--task", "T-1", "--epoch", "4", "--placement", "plc_abc123"]);
  assert.equal(poke.placement_id, "plc_abc123");
});

test("broken runner options are ignored, not fatal", () => {
  assert.deepEqual(workerRunnerOptions({ VEYNOD_RUNNER_OPTIONS: "{not json" }), {});
  assert.deepEqual(workerRunnerOptions({}), {});
  assert.deepEqual(workerRunnerOptions({ VEYNOD_RUNNER_OPTIONS: JSON.stringify({ repos: "x", slots: 9 }) }), {});
});
