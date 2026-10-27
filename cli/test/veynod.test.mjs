import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditWindowsTasks,
  claudeCodeConfigPath,
  claudeDesktopConfigPath,
  claudeDesktopEntry,
  configureClaudeCode,
  cursorEntry,
  makeKeyPair,
  mergeServer,
  openSealed,
  parseArgs,
  parseCsv,
  parseSse,
  selectedClients,
  sealForTest,
  servicePlan,
  startHeartbeat,
  visibleWindow,
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

test("a missing client never reveals the key implicitly", () => {
  assert.throws(() => selectedClients([], []), /--client print/);
  assert.deepEqual(selectedClients(["print"], []), ["print"]);
  assert.deepEqual(selectedClients([], ["cursor"]), ["cursor"]);
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

// ------------------------------------------------------------- service (L4)

const PLAN = {
  home: "/home/ada",
  node: "/usr/bin/node",
  script: "/opt/veynod/bin/veynod.mjs",
  host: "https://veynod.example",
  keyFile: "/home/ada/.veynod/listen.key",
  exec: "./on-poke.sh",
  cwd: "/srv/agent",
};

test("service plans: systemd restarts listen and runs it from the install directory", () => {
  const plan = servicePlan("install", { ...PLAN, os: "linux" });
  const unit = plan.files[0];
  assert.equal(unit.path, "/home/ada/.config/systemd/user/veynod-listen.service");
  assert.match(unit.content, /^Restart=always$/m);
  assert.match(unit.content, /^WorkingDirectory="\/srv\/agent"$/m);
  assert.match(unit.content, /"--key-file" "\/home\/ada\/\.veynod\/listen\.key" "--exec" "\.\/on-poke\.sh"/);
  assert.doesNotMatch(unit.content, /agt_/); // the key stays in its own file
  assert.deepEqual(plan.commands.at(-1), ["systemctl", "--user", "enable", "--now", "veynod-listen.service"]);
  const removal = servicePlan("uninstall", { ...PLAN, os: "linux" });
  assert.deepEqual(removal.remove, [unit.path]);
});

test("service plans: launchd keeps it alive", () => {
  const plan = servicePlan("install", { ...PLAN, os: "darwin", exec: "a&b" });
  const plist = plan.files[0].content;
  assert.match(plist, /<key>KeepAlive<\/key><true\/>/);
  assert.match(plist, /<string>a&amp;b<\/string>/);
  assert.deepEqual(plan.commands.at(-1), ["launchctl", "load", "-w", plan.files[0].path]);
});

test("service plans: Windows runs hidden through wscript at logon", () => {
  const plan = servicePlan("install", { ...PLAN, os: "win32", home: "C:\\Users\\ada",
    node: "C:\\Program Files\\nodejs\\node.exe", cwd: "C:\\agent" });
  const vbs = plan.files[0].content;
  assert.match(vbs, /shell\.Run ".*", 0, True/); // window style 0: no console
  assert.match(vbs, /WScript\.Sleep 5000/);
  assert.match(vbs, /cd \/d ""C:\\agent""/);
  const create = plan.commands[0];
  assert.equal(create[0], "schtasks");
  assert.ok(create.includes("ONLOGON"));
  assert.match(create[create.indexOf("/TR") + 1], /^wscript\.exe \/\/B \/\/Nologo "/);
});

test("audit flags tasks that open a console window, once per task", () => {
  const csv = [
    '"HostName","TaskName","Next Run Time","Status","Schedule Type","Repeat: Every","Task To Run","Run As User"',
    '"PC","\\\\DouWatchdog","10:05","Ready","One Time Only, Minutes","0 Hour(s), 5 Minute(s)","cmd /c C:\\\\dou\\\\watch.bat","ada"',
    '"PC","\\\\DouWatchdog","10:05","Ready","At logon time","","cmd /c C:\\\\dou\\\\watch.bat","ada"',
    '"PC","\\\\Quiet","N/A","Ready","At logon time","","wscript.exe //B C:\\\\x\\\\run.vbs node","ada"',
    '"PC","\\\\Microsoft\\\\Windows\\\\Defrag","N/A","Ready","Weekly","","defrag.exe -c","SYSTEM"',
    '"PC","\\\\Backup","N/A","Ready","Daily","","C:\\\\Tools\\\\robocopy.exe a b","ada"',
  ].join("\r\n");
  assert.equal(parseCsv(csv).length, 6);
  const findings = auditWindowsTasks(csv);
  assert.deepEqual(findings.map((item) => [item.name, item.visible_window]),
    [["\\\\DouWatchdog", true], ["\\\\Quiet", false]]);
  assert.match(findings[0].schedule, /5 Minute/);
  assert.equal(visibleWindow("powershell -WindowStyle Hidden -File x.ps1"), false);
  assert.equal(visibleWindow("powershell -File x.ps1"), true);
});

test("heartbeat beats at once and on its interval, and stops on 404", async () => {
  const calls = [];
  let status = 200;
  const fetchImpl = async (url, init) => {
    calls.push([url, init.method, init.headers.Authorization]);
    return { ok: status === 200, status };
  };
  const stop = startHeartbeat("https://veynod.example", "agt_x", { every: 20, fetchImpl });
  await new Promise((resolve) => setTimeout(resolve, 70));
  stop();
  assert.ok(calls.length >= 3, `beats: ${calls.length}`);
  assert.deepEqual(calls[0], ["https://veynod.example/v1/presence/heartbeat", "POST", "Bearer agt_x"]);
  status = 404;
  const older = [];
  const stopOld = startHeartbeat("https://old.example", "agt_y", { every: 10,
    fetchImpl: async (url) => { older.push(url); return { ok: false, status: 404 }; } });
  await new Promise((resolve) => setTimeout(resolve, 50));
  stopOld();
  assert.equal(older.length, 1); // an older server without the endpoint: one try, then quiet
});

test("arguments: service subcommand, key file, no heartbeat, dry run", () => {
  const options = parseArgs(["service", "install", "--key-file", "/k", "--no-heartbeat", "--dry-run"]);
  assert.equal(options.command, "service");
  assert.equal(options.subcommand, "install");
  assert.equal(options.keyFile, "/k");
  assert.equal(options.heartbeat, false);
  assert.equal(options.dryRun, true);
});
