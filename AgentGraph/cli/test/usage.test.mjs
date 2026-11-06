import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { claudeUsageWindows, codexUsageWindows, readClaude, readCodex } from "../bin/usage.mjs";

/**
 * A fake child process speaking JSON lines: `answer(message)` returns the
 * replies to one line written to its stdin (or nothing).
 */
function fakeSpawn(answer, seen = []) {
  return (command, args) => {
    seen.push([command, ...args]);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.write = (line) => {
      for (const reply of answer(JSON.parse(line)) || []) {
        setImmediate(() => child.stdout.emit("data", `${JSON.stringify(reply)}\n`));
      }
    };
    child.kill = () => setImmediate(() => child.emit("exit", 0));
    return child;
  };
}

const resolve = (name) => ({ command: name, prefix: [] });

test("Codex rate limits become the board's usage windows, one per bucket and window", () => {
  // The shape Codex 0.167's app-server returns for account/rateLimits/read.
  const usage = codexUsageWindows({
    rateLimits: { limitId: "codex", limitName: null, planType: "plus",
      primary: { usedPercent: 12.5, windowDurationMins: 300, resetsAt: 1793900000 },
      secondary: { usedPercent: 47, windowDurationMins: 10080, resetsAt: 1794300000 } },
    rateLimitsByLimitId: {
      codex: { limitId: "codex", primary: { usedPercent: 12.5, windowDurationMins: 300 } },
      codex_spark: { limitId: "codex_spark", limitName: "GPT-Spark", primary: { usedPercent: 3, windowDurationMins: 300 } },
    },
  });
  assert.deepEqual(usage, { plan: "plus", windows: [
    { name: "Codex · 5 hours", used_percent: 12.5, resets_at: new Date(1793900000 * 1000).toISOString() },
    { name: "Codex · 1 week", used_percent: 47, resets_at: new Date(1794300000 * 1000).toISOString() },
    { name: "GPT-Spark · 5 hours", used_percent: 3 },
  ] });
  assert.equal(codexUsageWindows({}), null);
});

test("Claude Code's get_usage becomes usage windows; an API key has none", () => {
  const usage = claudeUsageWindows({ rate_limits_available: true, subscription_type: "max",
    rate_limits: { five_hour: { utilization: 41.5, resets_at: "2026-11-06T10:00:00Z" },
      seven_day: { utilization: 20 }, model_scoped: [{ display_name: "Opus", utilization: 60 }] } });
  assert.deepEqual(usage, { plan: "max", windows: [
    { name: "Current session", used_percent: 41.5, resets_at: "2026-11-06T10:00:00.000Z" },
    { name: "Current week (all models)", used_percent: 20 },
    { name: "Current week (Opus)", used_percent: 60 },
  ] });
  // What Claude Code 2.1 answers when signed in with an API key, not a subscription.
  assert.equal(claudeUsageWindows({ rate_limits_available: false, rate_limits: null }), null);
});

test("Codex is read through its app-server: models, then quota for a ChatGPT login", async () => {
  const seen = [];
  const spawnImpl = fakeSpawn((message) => {
    if (message.method === "initialize") return [{ id: message.id, result: { userAgent: "veynod" } }];
    if (message.method === "model/list") {
      return [{ id: message.id, result: { data: [{ id: "gpt-6.1-sol", hidden: false }, { id: "internal", hidden: true }] } }];
    }
    if (message.method === "account/read") {
      return [{ id: message.id, result: { account: { type: "chatgpt", email: "dou@example.com", planType: "plus" } } }];
    }
    if (message.method === "account/rateLimits/read") {
      return [{ id: message.id, result: { rateLimits: { limitId: "codex", planType: "plus",
        primary: { usedPercent: 50, windowDurationMins: 300 } } } }];
    }
    return [];
  }, seen);
  const found = await readCodex({ resolve, spawnImpl });
  assert.deepEqual(found, { models: ["gpt-6.1-sol"],
    usage: { plan: "plus", windows: [{ name: "Codex · 5 hours", used_percent: 50 }] } });
  assert.deepEqual(seen, [["codex", "app-server", "--listen", "stdio://"]]);
  assert.ok(!JSON.stringify(found).includes("dou@example.com"), "the account email is never kept");
});

test("Codex signed in with an API key reports models but no subscription quota", async () => {
  const methods = [];
  const spawnImpl = fakeSpawn((message) => {
    methods.push(message.method);
    if (message.method === "initialize") return [{ id: message.id, result: {} }];
    if (message.method === "model/list") return [{ id: message.id, result: { data: [{ id: "gpt-6-luna" }] } }];
    if (message.method === "account/read") return [{ id: message.id, result: { account: { type: "apiKey" } } }];
    return [];
  });
  assert.deepEqual(await readCodex({ resolve, spawnImpl }), { models: ["gpt-6-luna"] });
  assert.ok(!methods.includes("account/rateLimits/read"), "no rate-limit read for an API key");
});

test("Claude Code is read through control requests: models from initialize, quota from get_usage", async () => {
  const seen = [];
  const spawnImpl = fakeSpawn((message) => {
    const reply = (response) => [{ type: "control_response",
      response: { subtype: "success", request_id: message.request_id, response } }];
    if (message.request?.subtype === "initialize") {
      return reply({ models: [{ value: "default" }, { value: "opus" }], account: { email: "dou@example.com" } });
    }
    if (message.request?.subtype === "get_usage") {
      return reply({ rate_limits_available: true, rate_limits: { five_hour: { utilization: 10 } } });
    }
    return [];
  }, seen);
  const found = await readClaude({ resolve, spawnImpl });
  assert.deepEqual(found, { models: ["default", "opus"],
    usage: { windows: [{ name: "Current session", used_percent: 10 }] } });
  assert.deepEqual(seen[0], ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
    "--verbose", "--no-session-persistence", "--safe-mode"]);
  assert.ok(!JSON.stringify(found).includes("dou@example.com"));
});

test("An agent that is missing, silent or exits says nothing instead of failing", async () => {
  assert.equal(await readCodex({ resolve: () => null }), null);
  const silent = fakeSpawn(() => []);
  assert.equal(await readClaude({ resolve, spawnImpl: silent, timeoutMs: 50 }), null);
  const exits = (command, args) => {
    const child = fakeSpawn(() => [])(command, args);
    setImmediate(() => child.emit("exit", 1));
    return child;
  };
  assert.equal(await readCodex({ resolve, spawnImpl: exits }), null);
});
