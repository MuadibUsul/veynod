import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { forgetLogins, loginDir, loginEnv, loginRuntimes, plainText, ptyCommand, readClaudeSetup, readCodexDevice,
  signedIn, startLogin } from "../bin/login.mjs";
import { Messenger } from "../bin/veynod-messenger.mjs";

const scratch = () => mkdtempSync(join(tmpdir(), "veynod-login-"));
const resolve = (name) => ({ command: name, prefix: [] });
const CLAUDE_URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
  + "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference"
  + "&code_challenge=rmRlQci&code_challenge_method=S256&state=REmGKSI";
// Claude Code 2.1.318's setup-token as a terminal shows it: spaces are cursor moves.
const CLAUDE_SCREEN = `\x1b[?25l\x1b]0;Claude\x07Welcome\x1b[1Cto\x1b[1CClaude\x1b[1CCode\x1b[1Cv2.1.318\r\n`
  + `Browser\x1b[1Cdidn't\x1b[1Copen?\x1b[1CUse\x1b[1Cthe\x1b[1Curl\x1b[1Cbelow\x1b[1Cto\x1b[1Csign\x1b[1Cin\r\n`
  + `\x1b[2m${CLAUDE_URL}\x1b[22m\r\n\r\nPaste\x1b[1Ccode\x1b[1Chere\x1b[1Cif\x1b[1Cprompted\x1b[1C>\r\n`;
const TOKEN = "sk-ant-oat01-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-AbCdEfGhIjKl";
// Codex 0.167's `login --device-auth`.
const CODEX_SCREEN = "Welcome to Codex [v0.167.0]\nFollow these steps to sign in with ChatGPT using device code authorization:\n\n"
  + "1. Open this link in your browser and sign in to your account\n   https://auth.openai.com/codex/device\n\n"
  + "2. Enter this one-time code (expires in 15 minutes)\n   318M-3SF5H\n";

/** A fake process: `screen(text)` writes to its stdout; stdin writes are recorded. */
function fakeProcess() {
  const calls = [];
  let last;
  const spawnImpl = (command, args, options) => {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.typed = [];
    child.stdin = { write: (text) => child.typed.push(text) };
    child.kill = () => setImmediate(() => child.emit("exit", null, "SIGTERM"));
    calls.push({ command, args, env: options.env });
    last = child;
    return child;
  };
  return { spawnImpl, calls, child: () => last, screen: (text) => last.stdout.emit("data", text) };
}

const tick = () => new Promise((resolveTick) => setImmediate(resolveTick));

test("a terminal screen reads as text: the sign-in link and the prompt are found", () => {
  assert.match(plainText(CLAUDE_SCREEN), /Welcome to Claude Code v2\.1\.318/);
  assert.deepEqual(readClaudeSetup(CLAUDE_SCREEN), { url: CLAUDE_URL, asking: true, refused: false });
  // What it shows for a wrong code (seen live).
  assert.equal(readClaudeSetup(`${CLAUDE_SCREEN} OAuth error: That code wasn't accepted.`).refused, true);
  assert.equal(readClaudeSetup(`${CLAUDE_SCREEN}\r\nYour OAuth token (valid for 1 year):\r\n${TOKEN}\r\n`).token, TOKEN);
  // A link to anywhere but the provider's own sign-in is never shown.
  assert.equal(readClaudeSetup("https://evil.example/oauth/authorize?x=1 Paste code").url, undefined);
  assert.deepEqual(readCodexDevice(CODEX_SCREEN),
    { url: "https://auth.openai.com/codex/device", user_code: "318M-3SF5H", expires_in_s: 900 });
});

test("Claude Code signs in under a pseudo-terminal wide enough that its link never wraps", () => {
  assert.deepEqual(ptyCommand("claude", ["setup-token"], "linux"),
    { command: "script", args: ["-qfec", "stty cols 4000 rows 40 2>/dev/null; exec 'claude' 'setup-token'", "/dev/null"] });
  assert.equal(ptyCommand("/opt/it's/claude", [], "darwin").args.at(-1),
    "stty cols 4000 rows 40 2>/dev/null; exec '/opt/it'\\''s/claude'");
  assert.equal(ptyCommand("claude", [], "win32"), null);
  const both = [{ id: "claude", available: true }, { id: "codex", available: true }];
  assert.deepEqual(loginRuntimes(both, { platform: "linux", has: () => true }), ["claude", "codex"]);
  assert.deepEqual(loginRuntimes(both, { platform: "win32", has: () => true }), ["codex"]);
  assert.deepEqual(loginRuntimes(both, { platform: "linux", has: () => false }), ["codex"], "no `script`");
  assert.deepEqual(loginRuntimes([{ id: "claude", available: false }], { platform: "linux", has: () => true }), []);
});

test("Claude Code: link, the person's code, then a token kept here and never sent", async () => {
  const stateDir = scratch();
  const fake = fakeProcess();
  const sent = [];
  const reads = [];
  const reader = async (id, options) => {
    reads.push([id, options.env.CLAUDE_CODE_OAUTH_TOKEN]);
    return { usage: { plan: "max", windows: [] } };
  };
  const login = startLogin({ runtime: "claude", agentId: "dou-server", stateDir, platform: "linux", resolve,
    spawnImpl: fake.spawnImpl, reader, send: (update) => sent.push(update) });
  assert.equal(fake.calls[0].command, "script");
  fake.screen(CLAUDE_SCREEN);
  fake.screen("\x1b[2K"); // a redraw shows nothing new
  assert.deepEqual(sent, [{ state: "starting" }, { state: "enter_code", url: CLAUDE_URL }]);

  assert.equal(login.code("not a code; rm -rf /"), false, "only a code goes to the terminal");
  assert.equal(login.code("  dGhpc2lzYWNvZGU#REmGKSI  "), true);
  assert.deepEqual(fake.child().typed, ["dGhpc2lzYWNvZGU#REmGKSI\r"]);

  fake.screen(`\r\nLong-lived authentication token created successfully!\r\n${TOKEN}\r\n`);
  assert.equal(await login.done, "connected");
  assert.deepEqual(sent.slice(2), [{ state: "verifying" }, { state: "connected", plan: "max" }]);
  assert.deepEqual(reads, [["claude", TOKEN]], "the new login is what the check reads");
  assert.ok(!JSON.stringify(sent).includes(TOKEN), "the token never leaves this computer");
  const file = join(loginDir("dou-server", stateDir), "claude-oauth-token");
  assert.equal(readFileSync(file, "utf8").trim(), TOKEN);
  if (process.platform !== "win32") assert.equal(statSync(file).mode & 0o777, 0o600);
  assert.deepEqual(loginEnv("dou-server", { stateDir }), { CLAUDE_CODE_OAUTH_TOKEN: TOKEN });
  assert.deepEqual(loginEnv("someone-else", { stateDir }), {}, "a login serves only its own identity");
  assert.deepEqual(signedIn("dou-server", { stateDir }), ["claude"]);
});

test("Claude Code: a refused code, an early exit and Windows each fail in plain words", async () => {
  const stateDir = scratch();
  const fake = fakeProcess();
  const sent = [];
  const login = startLogin({ runtime: "claude", agentId: "a", stateDir, platform: "linux", resolve,
    spawnImpl: fake.spawnImpl, send: (update) => sent.push(update) });
  fake.screen(`${CLAUDE_SCREEN} OAuth error: That code wasn't accepted.`);
  assert.equal(await login.done, "failed");
  assert.match(sent.at(-1).message, /did not accept that code/);

  const early = fakeProcess();
  const out = [];
  const gone = startLogin({ runtime: "claude", agentId: "a", stateDir, platform: "linux", resolve,
    spawnImpl: early.spawnImpl, send: (update) => out.push(update) });
  early.child().emit("exit", 1);
  assert.equal(await gone.done, "failed");
  assert.match(out.at(-1).message, /ended before it finished/);

  const windows = [];
  const none = startLogin({ runtime: "claude", agentId: "a", stateDir, platform: "win32", resolve,
    spawnImpl: () => assert.fail("nothing starts"), send: (update) => windows.push(update) });
  assert.equal(await none.done, "failed");
  assert.match(windows.at(-1).message, /Windows/);
  assert.deepEqual(signedIn("a", { stateDir }), []);
});

test("Codex: a device code to enter on auth.openai.com, and the login lands in this identity's CODEX_HOME", async () => {
  const stateDir = scratch();
  const fake = fakeProcess();
  const sent = [];
  const reader = async (id, options) => ({ usage: { plan: options.env.CODEX_HOME ? "plus" : null, windows: [] } });
  const login = startLogin({ runtime: "codex", agentId: "dou-server", stateDir, resolve, spawnImpl: fake.spawnImpl,
    reader, send: (update) => sent.push(update) });
  const home = join(loginDir("dou-server", stateDir), "codex");
  assert.deepEqual(fake.calls[0].args, ["login", "--device-auth"]);
  assert.equal(fake.calls[0].env.CODEX_HOME, home);
  fake.screen(CODEX_SCREEN);
  assert.deepEqual(sent.at(-1), { state: "open_url", url: "https://auth.openai.com/codex/device",
    user_code: "318M-3SF5H", expires_in_s: 900 });
  writeFileSync(join(home, "auth.json"), "{}"); // what Codex writes once the person signed in
  fake.child().emit("exit", 0);
  assert.equal(await login.done, "connected");
  assert.deepEqual(sent.slice(-2), [{ state: "verifying" }, { state: "connected", plan: "plus" }]);
  assert.deepEqual(loginEnv("dou-server", { stateDir }), { CODEX_HOME: home });
  forgetLogins("dou-server", { stateDir });
  assert.deepEqual(signedIn("dou-server", { stateDir }), []);
});

test("Codex: an expired code fails; cancel and the deadline end a sign-in", async () => {
  const stateDir = scratch();
  const fake = fakeProcess();
  const sent = [];
  const expired = startLogin({ runtime: "codex", agentId: "a", stateDir, resolve, spawnImpl: fake.spawnImpl,
    send: (update) => sent.push(update) });
  fake.child().emit("exit", 1);
  assert.equal(await expired.done, "failed");
  assert.match(sent.at(-1).message, /did not finish/);

  const cancelled = startLogin({ runtime: "codex", agentId: "a", stateDir, resolve, spawnImpl: fakeProcess().spawnImpl,
    send: () => {} });
  cancelled.cancel();
  assert.equal(await cancelled.done, "cancelled");

  const late = [];
  const slow = startLogin({ runtime: "codex", agentId: "a", stateDir, resolve, spawnImpl: fakeProcess().spawnImpl,
    timeoutMs: 10, send: (update) => late.push(update) });
  assert.equal(await slow.done, "failed");
  assert.match(late.at(-1).message, /not finished in time/);
  assert.equal(startLogin({ runtime: "gemini", agentId: "a", stateDir, send: () => {} }).code("x"), false);
});

test("the messenger runs a sign-in the hub asks for and sends back only its updates", async () => {
  const sent = [];
  const refreshed = [];
  let typed = null;
  const startLogin = ({ runtime, agentId, send }) => {
    let finish;
    const done = new Promise((resolveDone) => { finish = resolveDone; });
    send({ state: "starting" });
    setImmediate(() => send({ state: "enter_code", url: CLAUDE_URL }));
    return { done, code: (text) => { typed = [agentId, text]; send({ state: "connected" }); finish("connected"); return true; },
      cancel: () => finish("cancelled"), runtime };
  };
  const messenger = new Messenger({ host: "https://h", key: "agt_x", agentId: "box", startLogin,
    hostRecord: { refresh: async () => refreshed.push(true) } });
  messenger.ws = { readyState: WebSocket.OPEN, send: (data) => sent.push(JSON.parse(data)), close() {} };
  messenger.onFrame({ type: "login.start", frame_id: "f1", payload: { login_id: "lgn_1", runtime: "claude" } });
  messenger.onFrame({ type: "login.start", frame_id: "f2", payload: { login_id: "lgn_1", runtime: "claude" } });
  for (let i = 0; i < 5; i += 1) await tick();
  messenger.onFrame({ type: "login.code", frame_id: "f3", payload: { login_id: "lgn_1", code: "abcdefgh#state" } });
  for (let i = 0; i < 5; i += 1) await tick();
  assert.deepEqual(typed, ["box", "abcdefgh#state"]);
  assert.deepEqual(sent.map((frame) => [frame.type, frame.payload.login_id, frame.payload.state]), [
    ["login.update", "lgn_1", "starting"],
    ["login.update", "lgn_1", "enter_code"],
    ["login.update", "lgn_1", "connected"],
  ], "a repeated start is ignored");
  assert.equal(sent[1].payload.runtime, "claude");
  assert.deepEqual(refreshed, [true], "the new subscription reaches the board at once");
  // A code for a sign-in that is not running here says so instead of vanishing.
  messenger.onFrame({ type: "login.code", frame_id: "f4", payload: { login_id: "lgn_gone", code: "abcdefgh" } });
  for (let i = 0; i < 3; i += 1) await tick();
  assert.equal(sent.at(-1).payload.state, "failed");
});

test("a sign-in that cannot start reports it and the hub keeps running", async () => {
  const sent = [];
  const startLogin = () => { throw new Error("EACCES: permission denied, mkdir"); };
  const messenger = new Messenger({ host: "https://h", key: "agt_x", agentId: "box", startLogin, say: () => {} });
  messenger.ws = { readyState: WebSocket.OPEN, send: (data) => sent.push(JSON.parse(data)), close() {} };
  messenger.onFrame({ type: "login.start", frame_id: "f1", payload: { login_id: "lgn_x", runtime: "codex" } });
  for (let i = 0; i < 5; i += 1) await tick();
  assert.deepEqual(sent.map((frame) => frame.payload.state), ["failed"]);
  assert.equal(messenger.logins.has("lgn_x"), false, "the id is free for a new attempt");
});
