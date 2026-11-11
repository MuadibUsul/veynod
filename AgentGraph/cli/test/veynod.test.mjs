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
  computerCapabilities,
  claudeCodeConfigPath,
  claudeDesktopConfigPath,
  claudeDesktopEntry,
  configureClaudeCode,
  cursorEntry,
  isNpxCachePath,
  makeKeyPair,
  manualCommand,
  mergeServer,
  openSealed,
  acquireListenPid,
  follow,
  isPlannedClose,
  nextReconnect,
  parseArgs,
  parseCsv,
  parseRetryAfter,
  parseSse,
  processAlive,
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
  // No command is the one command; flags given are remembered as given.
  const bare = parseArgs(["--run-tasks", "--repo", "acme/site"]);
  assert.equal(bare.command, null);
  assert.deepEqual([...bare.explicit].sort(), ["repos", "runTasks"]);
  assert.equal(parseArgs(["--no-run-tasks"]).runTasks, false);
  // --supervisor (what the board's doctor suggests) runs tasks and says so to the board.
  const supervisor = parseArgs(["up", "--supervisor"]);
  assert.equal(supervisor.runTasks, true);
  assert.deepEqual(computerCapabilities({ ...supervisor, runtime: "claude" }), ["claude", "supervisor"]);
  assert.deepEqual(computerCapabilities({ runtime: "", deploy: false }), ["agent"]);
  assert.deepEqual(computerCapabilities({ deploy: true }), ["deploy"]);
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
    "retry: 3000\nevent: connected\ndata: {}\n\n: keep-alive\n\n: ping\n\nid: 7\nevent: poke\ndata: {\"a\":1}\n\nid: 8\nev");
  assert.deepEqual(events.map((item) => item.event), ["connected", "poke"]);
  assert.equal(events[1].id, "7");
  assert.deepEqual(JSON.parse(events[1].data), { a: 1 });
  assert.equal(rest, "id: 8\nev");
  assert.equal(isPlannedClose({ event: "planned-close", data: "{\"reason\":\"max_lifetime\"}" }), true);
  assert.equal(isPlannedClose({ event: "closed", data: "{\"reason\":\"max_lifetime\"}" }), true);
  assert.equal(isPlannedClose({ event: "closed", data: "{\"reason\":\"credential_revoked\"}" }), false);
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

test("listen hands direct messages to the hook, not only pokes", async () => {
  const { follow } = await import("../bin/veynod.mjs");
  const server = createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.write("event: connected\ndata: {}\n\n");
    res.write('id: 41\nevent: message\ndata: {"event_seq":41,"kind":"message.sent","message":{"conv_key":"dm_1","sender":"agent:a"}}\n\n');
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const previous = process.env.VEYNOD_STATE_DIR;
  process.env.VEYNOD_STATE_DIR = mkdtempSync(join(tmpdir(), "veynod-follow-"));
  const got = [];
  try {
    const code = await follow({ host: `http://127.0.0.1:${server.address().port}`, key: "agt_x", once: true,
      onPoke: async (poke) => { got.push(poke); } });
    assert.equal(code, 0);
  } finally {
    server.close();
    server.closeAllConnections?.();
    if (previous === undefined) delete process.env.VEYNOD_STATE_DIR;
    else process.env.VEYNOD_STATE_DIR = previous;
  }
  assert.equal(got.length, 1);
  assert.equal(got[0].kind, "message.sent");
  assert.equal(got[0].message.conv_key, "dm_1");
});

test("a service is never pointed into the npx cache", () => {
  assert.equal(isNpxCachePath(String.raw`C:\Users\me\AppData\Local\npm-cache\_npx\a6fe6d\node_modules\veynod\bin\veynod.mjs`), true);
  assert.equal(isNpxCachePath("/home/me/.npm/_npx/abc/node_modules/veynod/bin/veynod.mjs"), true);
  assert.equal(isNpxCachePath(String.raw`C:\Users\me\AppData\Roaming\npm\node_modules\veynod\bin\veynod.mjs`), false);
  assert.equal(isNpxCachePath("/usr/local/lib/node_modules/veynod/bin/veynod.mjs"), false);
});

test("a blocked service command is printed so it can be run by hand", () => {
  const launcher = String.raw`C:\Users\me\.veynod\veynod-hub.vbs`;
  const line = manualCommand("schtasks", ["/Create", "/TN", "VeynodHub",
    "/TR", `wscript.exe //B //Nologo "${launcher}"`, "/F"]);
  assert.match(line, /^schtasks \/Create \/TN VeynodHub /);
  assert.ok(line.includes(`/TR "wscript.exe //B //Nologo ""${launcher}"""`));
  assert.ok(line.endsWith(" /F"));
  assert.equal(manualCommand("systemctl", ["--user", "daemon-reload"]), "systemctl --user daemon-reload");
});

function framesResponse(frames, { status = 200, headers = {}, advanceMs = 0, clock } = {}) {
  const encoder = new TextEncoder();
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[String(name).toLowerCase()] ?? null },
    json: async () => ({}),
    body: {
      async *[Symbol.asyncIterator]() {
        if (clock && advanceMs) clock.t += advanceMs;
        for (const frame of frames) yield encoder.encode(frame);
      },
    },
  };
}

async function withState(run) {
  const previous = process.env.VEYNOD_STATE_DIR;
  process.env.VEYNOD_STATE_DIR = mkdtempSync(join(tmpdir(), "veynod-listen-"));
  try {
    return await run(process.env.VEYNOD_STATE_DIR);
  } finally {
    if (previous === undefined) delete process.env.VEYNOD_STATE_DIR;
    else process.env.VEYNOD_STATE_DIR = previous;
  }
}

test("reconnect math: 429 uses Retry-After, short streams climb, stable and planned close reset", () => {
  assert.equal(parseRetryAfter("2"), 2_000);
  assert.equal(parseRetryAfter(""), null);
  const limited = nextReconnect({ attempt: 0, connectedAt: 0 }, { reason: "429", retryAfterMs: 2_000, random: () => 0 });
  assert.equal(limited.delayMs, 2_000);
  assert.equal(limited.reset, false);
  const missing = nextReconnect({ attempt: 3, connectedAt: 0 }, { reason: "429", retryAfterMs: null, random: () => 0 });
  assert.equal(missing.delayMs, 60_000);
  const climbed = nextReconnect({ attempt: 2, connectedAt: 0 }, { reason: "network", now: 5_000, random: () => 0 });
  assert.equal(climbed.attempt, 3);
  assert.equal(climbed.delayMs, 4_000);
  const young = nextReconnect({ attempt: 4, connectedAt: 1_000 }, { reason: "ended", now: 10_000, random: () => 0 });
  assert.equal(young.reset, false);
  assert.equal(young.attempt, 5);
  const stable = nextReconnect({ attempt: 4, connectedAt: 1_000 }, { reason: "ended", now: 70_000, random: () => 0 });
  assert.equal(stable.reset, true);
  assert.equal(stable.attempt, 0);
  assert.equal(stable.delayMs, 1_000);
  const planned = nextReconnect({ attempt: 9, connectedAt: 0 }, { reason: "planned-close", random: () => 0 });
  assert.equal(planned.reset, true);
  assert.equal(planned.delayMs, 250);
  const capped = nextReconnect({ attempt: 20, connectedAt: 0 }, { reason: "network", random: () => 0.99 });
  assert.equal(capped.delayMs, 60_000);
  const hook = nextReconnect({ attempt: 4, connectedAt: 0 }, { reason: "hook", random: () => 0 });
  assert.equal(hook.attempt, 4);
  assert.equal(hook.delayMs, 1_000);
});

test("processAlive treats ESRCH as dead and EPERM as alive", () => {
  const fail = (code) => () => {
    const error = new Error(code);
    error.code = code;
    throw error;
  };
  assert.equal(processAlive(5, fail("ESRCH")), false);
  assert.equal(processAlive(5, fail("EPERM")), true);
  assert.equal(processAlive(5, () => {}), true);
  assert.equal(processAlive(0, () => {}), false);
});

test("stale listen.pid is replaced and a live one is kept", () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-pid-"));
  writeFileSync(join(dir, "listen.pid"), "4242\n");
  const release = acquireListenPid({ dir, pid: 7, alive: (pid) => pid !== 4242 });
  assert.equal(readFileSync(join(dir, "listen.pid"), "utf8").trim(), "7");
  release();
  assert.equal(existsSync(join(dir, "listen.pid")), false);
  writeFileSync(join(dir, "listen.pid"), "99\n");
  assert.throws(() => acquireListenPid({ dir, pid: 7, alive: () => true }), /already running/);
  assert.equal(readFileSync(join(dir, "listen.pid"), "utf8").trim(), "99");
});

test("follow keeps retrying 429 and does not reset after a short 200", async () => {
  await withState(async () => {
    const delays = [];
    let calls = 0;
    await assert.rejects(follow({ host: "http://veynod.test", key: "agt_retry" }, () => {}, {
      random: () => 0,
      sleepImpl: async (ms) => { delays.push(ms); },
      fetchImpl: async () => {
        calls += 1;
        if (calls === 1) return framesResponse([], { status: 429, headers: { "retry-after": "2" } });
        if (calls === 2) return framesResponse([], { status: 429 });
        if (calls === 3) return framesResponse(["event: connected\ndata: {}\n\n"]);
        if (calls === 4) return framesResponse(["event: connected\ndata: {}\n\n"]);
        throw Object.assign(new Error("stop"), { fatal: true });
      },
    }), /stop/);
    // Two 429s climb the counter. The following short 200s must not restart at 1s.
    assert.deepEqual(delays, [2_000, 60_000, 4_000, 8_000]);
  });
});

test("pings are ignored, a planned close reconnects quickly, and the cursor is kept", async () => {
  await withState(async (stateDir) => {
    const delays = [];
    const headersSeen = [];
    let calls = 0;
    await assert.rejects(follow({ host: "http://veynod.test", key: "agt_cursor", onPoke: async () => {} }, () => {}, {
      random: () => 0,
      sleepImpl: async (ms) => { delays.push(ms); },
      fetchImpl: async (_url, init) => {
        calls += 1;
        headersSeen.push(init.headers["Last-Event-ID"] ?? null);
        if (calls === 1) {
          return framesResponse([
            "event: connected\ndata: {}\n\n",
            ": ping\n\n",
            ": ping\n\n",
            'id: 9\nevent: poke\ndata: {"event_seq":9,"kind":"mention"}\n\n',
            'event: planned-close\ndata: {"reason":"max_lifetime","reconnect":true}\n\n',
            'event: closed\ndata: {"reason":"max_lifetime"}\n\n',
          ]);
        }
        throw Object.assign(new Error("stop"), { fatal: true });
      },
    }), /stop/);
    assert.deepEqual(delays, [250]);
    assert.deepEqual(headersSeen, [null, "9"]);
    const [name] = readdirSync(stateDir);
    const saved = JSON.parse(readFileSync(join(stateDir, name), "utf8"));
    assert.equal(saved.last_seq, 9);
    assert.equal(saved.last_event_id, "9");
  });
});

test("a stable stream resets backoff; 401 still leaves the loop", async () => {
  await withState(async () => {
    const clock = { t: 1_000_000 };
    const delays = [];
    let calls = 0;
    await assert.rejects(follow({ host: "http://veynod.test", key: "agt_stable" }, () => {}, {
      random: () => 0,
      now: () => clock.t,
      sleepImpl: async (ms) => { delays.push(ms); },
      fetchImpl: async () => {
        calls += 1;
        if (calls === 1) return framesResponse(["event: connected\ndata: {}\n\n"]);
        if (calls === 2) return framesResponse(["event: connected\ndata: {}\n\n: ping\n\n"], { advanceMs: 70_000, clock });
        throw Object.assign(new Error("stop"), { fatal: true });
      },
    }), /stop/);
    assert.deepEqual(delays, [1_000, 1_000]);
    await assert.rejects(follow({ host: "http://veynod.test", key: "agt_auth" }, () => {}, {
      fetchImpl: async () => ({
        ok: false,
        status: 401,
        json: async () => ({ error: { message: "refused token" } }),
        body: null,
      }),
      sleepImpl: async () => { throw new Error("retried"); },
    }), /refused token/);
  });
});

test("the sign-in names the installation, and a board that does not know the field is asked without it", async () => {
  const { connect } = await import("../bin/veynod.mjs");
  const bodies = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    if (String(url).endsWith("/v1/auth/cli/sessions")) {
      bodies.push(body);
      if ("client_instance_id" in body) {
        return new Response(JSON.stringify({ error: { code: "validation_error", message: "extra field" } }),
          { status: 422, headers: { "Content-Type": "application/json" } });
      }
      // Stop the flow here: the session is all this test looks at.
      return new Response(JSON.stringify({ error: { code: "stop", message: "stop" } }),
        { status: 500, headers: { "Content-Type": "application/json" } });
    }
    return new Response("{}", { status: 500 });
  };
  try {
    await assert.rejects(connect({ host: "https://old.example", open: false }, { instanceId: "inst-0001-abcd" }));
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0].client_instance_id, "inst-0001-abcd");
  assert.equal("client_instance_id" in bodies[1], false);
});
