// Recognising the coding agents on a computer, wiring the ones whose MCP file
// is known, and choosing the model provider an agent runs on.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { AGENT_CLIENTS, codexTomlWith, detectAgentClients, wireAgentClient } from "../bin/clients.mjs";
import { PROVIDERS, providerRun } from "../bin/providers.mjs";
import { adapterFor, workerRunnerOptions } from "../bin/runner.mjs";
import { parseArgs, wireClients } from "../bin/veynod.mjs";

const home = () => mkdtempSync(join(tmpdir(), "veynod-clients-"));
const URL_ = "https://veynod.example/mcp/";

// The owner's list (2026-11-05). Claude Code is wired by veynod.mjs itself.
const WANTED = ["Codex", "Gemini CLI", "OpenCode", "MiMo Code", "DevEco Code", "Pi", "Goose", "Cursor CLI",
  "Copilot CLI", "VS Code", "Crush", "DeepSeek Harness", "Command Code", "fx", "omp", "OmO", "Devin",
  "Hermes Agent", "Mister Morph", "Kimi Code", "Muse Code", "Empryo", "MiniMax Code", "Droid", "Grok Build",
  "ZCode", "OpenHanako", "AtomCode", "Snow CLI", "QwenPaw", "Alma"];

test("every agent on the list is known, each under its own id", () => {
  assert.deepEqual(AGENT_CLIENTS.map((item) => item.name), WANTED);
  assert.equal(new Set(AGENT_CLIENTS.map((item) => item.id)).size, AGENT_CLIENTS.length);
  // With Claude Code that is the 32.
  assert.equal(AGENT_CLIENTS.length + 1, 32);
  for (const item of AGENT_CLIENTS) {
    assert.ok(item.bins.length || item.dirs.length || item.found, `${item.name} can be found somehow`);
  }
});

test("an agent is recognised by its command or by its own settings folder, and only then", () => {
  const dir = home();
  assert.deepEqual(detectAgentClients({ home: dir, os: "linux", env: {} }), []);
  mkdirSync(join(dir, ".gemini"));
  mkdirSync(join(dir, ".config", "goose"), { recursive: true });
  mkdirSync(join(dir, ".hanako"));
  assert.deepEqual(detectAgentClients({ home: dir, os: "linux", env: {} }), ["gemini-cli", "goose", "openhanako"]);
  // On PATH counts too, whatever is in the home folder.
  const found = detectAgentClients({ home: home(), os: "linux", env: {}, onPath: (bin) => bin === "droid" || bin === "dsh" });
  assert.deepEqual(found, ["deepseek-harness", "droid"]);
  // Every listed agent is found when its first folder (or command) is there.
  for (const item of AGENT_CLIENTS) {
    const one = home();
    if (item.dirs.length) mkdirSync(join(one, ...item.dirs[0].split("/")), { recursive: true });
    const seen = detectAgentClients({ home: one, os: "linux", env: {},
      onPath: (bin) => !item.dirs.length && item.bins.includes(bin) });
    assert.ok(seen.includes(item.id), `${item.name} is recognised`);
  }
});

test("a wired agent gets Veynod's server in its own file and shape, and keeps what it had", () => {
  const dir = home();
  const read = (...parts) => JSON.parse(readFileSync(join(dir, ...parts), "utf8"));
  mkdirSync(join(dir, ".gemini"));
  writeFileSync(join(dir, ".gemini", "settings.json"), JSON.stringify({ theme: "dark",
    mcpServers: { other: { command: "x" } } }));
  assert.equal(wireAgentClient("gemini-cli", URL_, "agt_k", { home: dir }).wired, join(dir, ".gemini", "settings.json"));
  const gemini = read(".gemini", "settings.json");
  assert.equal(gemini.theme, "dark");
  assert.deepEqual(gemini.mcpServers.other, { command: "x" });
  assert.deepEqual(gemini.mcpServers.veynod, { httpUrl: URL_, headers: { Authorization: "Bearer agt_k" } });
  assert.ok(existsSync(join(dir, ".gemini", "settings.json.bak")), "the old file is kept beside it");

  wireAgentClient("opencode", URL_, "agt_k", { home: dir });
  assert.deepEqual(read(".config", "opencode", "opencode.json").mcp.veynod,
    { type: "remote", url: URL_, enabled: true, headers: { Authorization: "Bearer agt_k" } });
  wireAgentClient("copilot-cli", URL_, "agt_k", { home: dir });
  assert.deepEqual(read(".copilot", "mcp-config.json").mcpServers.veynod,
    { type: "http", url: URL_, headers: { Authorization: "Bearer agt_k" }, tools: ["*"] });
  wireAgentClient("vscode", URL_, "agt_k", { home: dir, os: "linux", env: {} });
  assert.deepEqual(read(".config", "Code", "User", "mcp.json").servers.veynod,
    { type: "http", url: URL_, headers: { Authorization: "Bearer agt_k" } });
  wireAgentClient("crush", URL_, "agt_k", { home: dir });
  assert.equal(read(".config", "crush", "crush.json").mcp.veynod.type, "http");
  wireAgentClient("droid", URL_, "agt_k", { home: dir });
  assert.equal(read(".factory", "mcp.json").mcpServers.veynod.url, URL_);
  wireAgentClient("kimi-code", URL_, "agt_k", { home: dir });
  assert.equal(read(".kimi", "mcp.json").mcpServers.veynod.transport, "http");
  wireAgentClient("cursor-cli", URL_, "agt_k", { home: dir });
  assert.deepEqual(read(".cursor", "mcp.json").mcpServers.veynod, { url: URL_, headers: { Authorization: "Bearer agt_k" } });
  // A file with comments is not rewritten.
  writeFileSync(join(dir, ".config", "crush", "crush.json"), "{ // mine\n}");
  assert.throws(() => wireAgentClient("crush", URL_, "agt_k", { home: dir }), /not plain JSON/);
});

test("Codex gets one section in config.toml; a second run replaces it and nothing else", () => {
  const before = ['model = "gpt-5"', "", "[mcp_servers.other]", 'command = "x"', "",
    "[mcp_servers.veynod]", 'url = "https://old.example/mcp/"', "", "[mcp_servers.veynod.env]", 'A = "b"', "",
    "[profiles.work]", 'model = "o3"'].join("\n");
  const after = codexTomlWith(before, "veynod", URL_, "agt_new");
  assert.match(after, /^model = "gpt-5"/);
  assert.match(after, /\[mcp_servers\.other\]\ncommand = "x"/);
  assert.match(after, /\[profiles\.work\]\nmodel = "o3"/);
  assert.equal(after.includes("old.example"), false);
  assert.equal(after.includes('A = "b"'), false, "the old section's sub-table goes with it");
  assert.equal(after.match(/\[mcp_servers\.veynod\]/g).length, 1);
  assert.match(after, /url = "https:\/\/veynod\.example\/mcp\/"\nhttp_headers = \{ Authorization = "Bearer agt_new" \}\n$/);
  assert.equal(codexTomlWith(after, "veynod", URL_, "agt_new"), after, "running again changes nothing");

  const dir = home();
  assert.equal(wireAgentClient("codex", URL_, "agt_k", { home: dir }).wired, join(dir, ".codex", "config.toml"));
  assert.match(readFileSync(join(dir, ".codex", "config.toml"), "utf8"), /^\[mcp_servers\.veynod\]/);
});

test("an agent whose MCP file is not known is found, not guessed at, and its key is not printed", () => {
  const dir = home();
  const manual = wireAgentClient("goose", URL_, "agt_secret", { home: dir });
  assert.deepEqual(manual, { manual: { url: URL_, headers: { Authorization: "Bearer agt_secret" } } });
  assert.equal(existsSync(join(dir, ".config", "goose")), false, "nothing was written");
  const lines = [];
  const configured = wireClients(["goose"], URL_, "agt_secret", (line = "") => lines.push(line));
  assert.deepEqual(configured, []);
  assert.match(lines.join("\n"), /Goose is installed; add this server/);
  assert.equal(lines.join("\n").includes("agt_secret"), false);
  assert.throws(() => wireAgentClient("nope", URL_, "k"), /unknown client/);
});

test("--client takes any of the agents; an unknown one is refused by name", () => {
  assert.deepEqual(parseArgs(["init", "--client", "gemini-cli,droid"]).clients, ["gemini-cli", "droid"]);
  assert.throws(() => parseArgs(["init", "--client", "notepad"]), /unknown client notepad/);
});

// ------------------------------------------------------------------ models

test("the thirteen providers can each be chosen", () => {
  assert.deepEqual(PROVIDERS.map((item) => item.name).sort(), ["AiHubMix", "Claude", "DeepSeek", "GLM", "Gemini",
    "Groq", "Kimi", "MiniMax", "Mistral", "OpenAI", "OpenRouter", "Qwen", "xAI"]);
  for (const item of PROVIDERS) {
    const options = parseArgs(["up", "--provider", item.id, "--model", "some-model"]);
    assert.deepEqual([options.provider, options.model, options.runtime], [item.id, "some-model", item.runtime]);
  }
  assert.throws(() => parseArgs(["up", "--provider", "nope"]), /unknown provider nope/);
  assert.throws(() => parseArgs(["up", "--provider", "deepseek", "--runtime", "codex"]), /runs through Claude Code/);
});

test("a provider's key stays on the computer: the run reads it from the environment, or does not start", async () => {
  // Claude Code on an Anthropic-compatible endpoint.
  const deepseek = providerRun("deepseek", { runtime: "claude", model: "deepseek-chat", env: { DEEPSEEK_API_KEY: "sk-d" } });
  assert.deepEqual(deepseek.env, { ANTHROPIC_BASE_URL: "https://api.deepseek.com/anthropic", ANTHROPIC_AUTH_TOKEN: "sk-d",
    ANTHROPIC_MODEL: "deepseek-chat", ANTHROPIC_SMALL_FAST_MODEL: "deepseek-chat" });
  // The owner's own endpoint for a provider wins over the preset.
  assert.equal(providerRun("glm", { runtime: "claude", env: { ZAI_API_KEY: "k", VEYNOD_GLM_BASE_URL: "https://open.bigmodel.cn/api/anthropic" } })
    .env.ANTHROPIC_BASE_URL, "https://open.bigmodel.cn/api/anthropic");
  // Codex with a model provider of its own; the key is named, never passed.
  const groq = providerRun("groq", { runtime: "codex", env: { GROQ_API_KEY: "gsk" } });
  assert.deepEqual(groq.env, {});
  assert.ok(groq.args.includes('model_providers.groq.env_key="GROQ_API_KEY"'));
  assert.ok(groq.args.includes('model_provider="groq"'));
  assert.equal(groq.args.join(" ").includes("gsk"), false);
  // Native sign-ins change nothing but the model.
  assert.deepEqual(providerRun("claude", { runtime: "claude", env: {} }), { env: {}, args: [] });
  assert.equal(providerRun("kimi", { runtime: "claude", env: {} }).missing, "MOONSHOT_API_KEY");
  assert.throws(() => providerRun("xai", { runtime: "claude", env: { XAI_API_KEY: "k" } }), /runs through Codex/);

  // In a run: the environment reaches Claude Code, the options reach Codex before the prompt.
  const calls = [];
  const spawner = async (command, args, options) => { calls.push({ command, args, env: options.env }); return { ok: true }; };
  process.env.MINIMAX_API_KEY = "mm-key";
  process.env.MISTRAL_API_KEY = "ms-key";
  try {
    await adapterFor("claude", { spawner, provider: "minimax", model: "MiniMax-M2" }).run({ cwd: ".", prompt: "do", env: {} });
    await adapterFor("codex", { spawner, provider: "mistral", model: "codestral" }).run({ cwd: ".", prompt: "do it", env: {} });
  } finally {
    delete process.env.MINIMAX_API_KEY;
    delete process.env.MISTRAL_API_KEY;
  }
  assert.equal(calls[0].env.ANTHROPIC_BASE_URL, "https://api.minimax.io/anthropic");
  assert.deepEqual(calls[0].args.slice(calls[0].args.indexOf("--model"), calls[0].args.indexOf("--model") + 2), ["--model", "MiniMax-M2"]);
  assert.equal(calls[1].args.at(-1), "do it");
  assert.ok(calls[1].args.includes('model_provider="mistral"'));
  const refused = await adapterFor("claude", { spawner, provider: "qwen" }).run({ cwd: ".", prompt: "do", env: {} });
  assert.deepEqual(refused, { ok: false, reason: "qwen needs DASHSCOPE_API_KEY set on this computer" });
  assert.equal(calls.length, 2, "nothing was started without the key");

  // The hub hands the choice to its workers.
  assert.deepEqual(workerRunnerOptions({ VEYNOD_RUNNER_OPTIONS: JSON.stringify({ runtime: "claude", provider: "kimi", model: "kimi-k2" }) }),
    { runtime: "claude", provider: "kimi", model: "kimi-k2" });
});
