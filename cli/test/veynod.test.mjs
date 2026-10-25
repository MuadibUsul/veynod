import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  claudeCodeConfigPath,
  claudeDesktopConfigPath,
  claudeDesktopEntry,
  configureClaudeCode,
  cursorEntry,
  makeKeyPair,
  mergeServer,
  openSealed,
  parseArgs,
  parseSse,
  sealForTest,
} from "../bin/veynod.mjs";

test("a sealed key opens only with the matching private key", () => {
  const mine = makeKeyPair();
  const sealed = sealForTest("agt_example", mine.publicRaw);
  assert.equal(openSealed(sealed, mine.privateKey, mine.publicRaw), "agt_example");
  const other = makeKeyPair();
  assert.throws(() => openSealed(sealed, other.privateKey, other.publicRaw));
  assert.throws(() => openSealed("v2.a.b.c", mine.privateKey, mine.publicRaw), /format/);
});

test("arguments", () => {
  const options = parseArgs(["init", "--client", "cursor,print", "--host=veynod.example/", "--no-open"]);
  assert.equal(options.command, "init");
  assert.deepEqual(options.clients, ["cursor", "print"]);
  assert.equal(options.host, "https://veynod.example");
  assert.equal(options.open, false);
  assert.throws(() => parseArgs(["init", "--client", "vim"]), /unknown client/);
  assert.throws(() => parseArgs(["init", "--host"]), /needs a value/);
});

test("merging keeps other servers and leaves a backup", () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-"));
  const path = join(dir, "nested", "mcp.json");
  mergeServer(path, cursorEntry("https://x/mcp/", "agt_1"));
  writeFileSync(path, JSON.stringify({ theme: "dark", mcpServers: { other: { url: "o" } } }));
  mergeServer(path, cursorEntry("https://x/mcp/", "agt_2"));
  const config = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(config.theme, "dark");
  assert.deepEqual(config.mcpServers.other, { url: "o" });
  assert.equal(config.mcpServers.veynod.headers.Authorization, "Bearer agt_2");
  assert.ok(existsSync(`${path}.bak`));
  writeFileSync(path, "{nope");
  assert.throws(() => mergeServer(path, {}), /not valid JSON/);
});

test("client config shapes", () => {
  const desktop = claudeDesktopEntry("https://x/mcp/", "agt_3");
  assert.deepEqual(desktop.args.slice(0, 3), ["-y", "mcp-remote", "https://x/mcp/"]);
  assert.equal(desktop.env.VEYNOD_AUTH, "Bearer agt_3");
  assert.ok(claudeDesktopConfigPath("/home/u", "darwin").replaceAll("\\", "/").includes("Application Support/Claude"));
  assert.ok(claudeDesktopConfigPath("/home/u", "linux").replaceAll("\\", "/").endsWith(".config/Claude/claude_desktop_config.json"));
  assert.equal(claudeCodeConfigPath("/home/u").replaceAll("\\", "/"), "/home/u/.claude.json");
});

test("a failed Claude Code add restores the previous config", () => {
  const home = mkdtempSync(join(tmpdir(), "veynod-home-"));
  const path = claudeCodeConfigPath(home);
  const previous = { mcpServers: { veynod: { url: "https://old/mcp/" }, other: { url: "https://other/mcp/" } } };
  writeFileSync(path, JSON.stringify(previous), { mode: 0o600 });
  const calls = [];
  const fakeExec = (_command, args) => {
    calls.push(args.join(" "));
    if (args[1] === "add") throw new Error("claude refused the new entry");
  };
  assert.throws(
    () => configureClaudeCode("https://new/mcp/", "agt_new", { home, exec: fakeExec }),
    /previous Claude Code config restored/,
  );
  assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), previous);
  assert.equal(existsSync(`${path}.veynod-backup`), false);
  assert.ok(calls.some((call) => call.includes("mcp remove")));
  assert.ok(calls.some((call) => call.includes("mcp add")));
});

test("parseSse splits frames and keeps the partial tail", () => {
  const [events, rest] = parseSse(
    "retry: 3000\nevent: connected\ndata: {}\n\n: keep-alive\n\nid: 7\nevent: poke\ndata: {\"a\":1}\n\nid: 8\nev");
  assert.deepEqual(events.map((item) => item.event), ["connected", "message", "poke"]);
  assert.equal(events[2].id, "7");
  assert.deepEqual(JSON.parse(events[2].data), { a: 1 });
  assert.equal(rest, "id: 8\nev");
});

test("listen options parse", () => {
  const options = parseArgs(["listen", "--key", "agt_x", "--exec", "./hook.sh", "--once"]);
  assert.equal(options.command, "listen");
  assert.equal(options.key, "agt_x");
  assert.equal(options.exec, "./hook.sh");
  assert.equal(options.once, true);
});

test("listen keeps the cursor when --exec fails", async () => {
  const stateDir = mkdtempSync(join(tmpdir(), "veynod-state-"));
  const server = createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write('id: 7\nevent: poke\ndata: {"event_seq":7,"kind":"mention"}\n\n');
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const cli = fileURLToPath(new URL("../bin/veynod.mjs", import.meta.url));
  const hookDir = mkdtempSync(join(tmpdir(), "veynod-hook-"));
  const hook = join(hookDir, "fail.mjs");
  writeFileSync(hook, "process.exit(1);\n");
  let stderr = "";
  const child = spawn(process.execPath, [
    cli, "listen", "--host", `http://127.0.0.1:${port}`, "--key", "agt_test",
    "--exec", `"${process.execPath}" "${hook.replaceAll("\\", "/")}"`, "--once",
  ], { env: { ...process.env, VEYNOD_STATE_DIR: stateDir }, stdio: ["ignore", "pipe", "pipe"] });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const code = await new Promise((resolve) => child.on("close", resolve));
  if (code !== 1) process.stderr.write(stderr);
  server.closeAllConnections?.();
  await new Promise((resolve) => server.close(resolve));
  assert.equal(code, 1);
  assert.deepEqual(readdirSync(stateDir), []);
});
