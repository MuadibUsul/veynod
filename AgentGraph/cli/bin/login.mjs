// Signing a computer in to a person's own subscription from the console
// (v2 phase 1, P1-D), for a server with no browser and no one at its keyboard.
//
// The console asks; the hub sends `login.start` over the nerve socket; this
// computer runs the agent's own sign-in and reports only what the person
// needs to see (a link, a one-time code), never the login itself:
//   Claude Code  `claude setup-token` under a pseudo-terminal (it draws a
//                terminal UI): it shows a sign-in link and asks for the code
//                the sign-in page then shows. The person pastes that code in
//                the console and it comes back as `login.code`. The code is
//                useless without the PKCE verifier, which never leaves the
//                process. The 1-year token it prints is kept here (0600) and
//                given to runs as CLAUDE_CODE_OAUTH_TOKEN.
//   Codex        `codex login --device-auth`: a link and a code the person
//                enters on auth.openai.com. Codex keeps the login in its own
//                CODEX_HOME here; nothing travels back at all.
// Each login belongs to one identity (~/.veynod/logins/<agent>/), so a
// subscription serves only the workspace that computer identity works for. A
// computer without one keeps using whatever login its agents already have.
// Checked against Claude Code 2.1.318 and Codex 0.167 on 2026-11-06.

import { spawn } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, join } from "node:path";

export const LOGIN_TIMEOUT_MS = 16 * 60 * 1000;
const safeName = (value) => String(value).replace(/[^A-Za-z0-9_.-]+/g, "_").slice(0, 64) || "agent";
const defaultStateDir = () => process.env.VEYNOD_STATE_DIR || join(homedir(), ".veynod");
const TOKEN_FILE = "claude-oauth-token";

export function loginDir(agentId, stateDir = defaultStateDir()) {
  return join(stateDir, "logins", safeName(agentId));
}

/** What a run of this identity adds to its environment for the logins made through Veynod. */
export function loginEnv(agentId, { stateDir = defaultStateDir(), read = readFileSync, exists = existsSync } = {}) {
  if (!agentId) return {};
  const dir = loginDir(agentId, stateDir);
  const env = {};
  try {
    const token = String(read(join(dir, TOKEN_FILE), "utf8")).trim();
    if (token) env.CLAUDE_CODE_OAUTH_TOKEN = token;
  } catch {
    // no Claude login made through Veynod
  }
  if (exists(join(dir, "codex", "auth.json"))) env.CODEX_HOME = join(dir, "codex");
  return env;
}

/** The runtimes signed in through Veynod for this identity (for the host report). */
export function signedIn(agentId, options) {
  const env = loginEnv(agentId, options);
  return [...(env.CLAUDE_CODE_OAUTH_TOKEN ? ["claude"] : []), ...(env.CODEX_HOME ? ["codex"] : [])];
}

const onPath = (name, env = process.env, exists = existsSync) =>
  String(env.PATH || "").split(delimiter).filter(Boolean).some((dir) => exists(join(dir, name)));

/**
 * Which installed runtimes this computer can sign in from the console. Claude
 * Code's sign-in needs a pseudo-terminal, which `script` gives on Linux and
 * macOS; Windows computers sign in with the agent itself, as people do.
 */
export function loginRuntimes(runtimes, { platform = process.platform, has = (name) => onPath(name) } = {}) {
  const installed = new Set((runtimes || []).filter((item) => item.available).map((item) => item.id));
  const can = [];
  if (installed.has("claude") && platform !== "win32" && has("script")) can.push("claude");
  if (installed.has("codex")) can.push("codex");
  return can;
}

const shellQuote = (value) => `'${String(value).replace(/'/g, "'\\''")}'`;

/** `command args…` under a pseudo-terminal wide enough that nothing wraps, or null. */
export function ptyCommand(command, args, platform = process.platform) {
  const inner = `stty cols 4000 rows 40 2>/dev/null; exec ${[command, ...args].map(shellQuote).join(" ")}`;
  if (platform === "linux") return { command: "script", args: ["-qfec", inner, "/dev/null"] };
  if (platform === "darwin") return { command: "script", args: ["-q", "/dev/null", "/bin/sh", "-c", inner] };
  return null;
}

/** Terminal output as plain text: no colours, cursor moves or title sequences. */
export function plainText(raw) {
  return String(raw)
    // A terminal UI draws spaces as cursor moves: keep them as spaces.
    .replace(/\x1b\[(\d*)C/g, (_, count) => " ".repeat(Math.min(Number(count) || 1, 200)))
    .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-9;?<>=]*[ -/]*[@-~]/g, "")
    .replace(/\x1b[()][A-Za-z0-9]/g, "")
    .replace(/\x1b[=>78]/g, "")
    .replace(/\r/g, "\n");
}

// Only links to the providers' own sign-in pages are ever shown to the person.
const CLAUDE_URL = /https:\/\/(?:claude\.com|claude\.ai|platform\.claude\.com|console\.anthropic\.com)\/[^\s"'<>]*oauth\/authorize\?[^\s"'<>]+/;
const CLAUDE_TOKEN = /\bsk-ant-oat\d{2}-[A-Za-z0-9_-]{20,}/;
const CODEX_URL = /https:\/\/auth\.openai\.com\/[^\s"'<>]+/;
const CODEX_CODE = /\b([A-Z0-9]{4}-[A-Z0-9]{4,8})\b/;

/** What Claude Code's setup-token has shown so far: {url?, asking?, token?, refused?}. */
export function readClaudeSetup(text) {
  const plain = plainText(text);
  const url = plain.match(CLAUDE_URL)?.[0];
  const token = plain.match(CLAUDE_TOKEN)?.[0];
  return {
    ...(url ? { url } : {}),
    asking: /paste\s*code/i.test(plain),
    ...(token ? { token } : {}),
    refused: /invalid (code|grant)|authorization failed|oauth error|failed to (exchange|create)/i.test(plain),
  };
}

/** What Codex's device sign-in has shown so far: {url?, user_code?, expires_in_s?}. */
export function readCodexDevice(text) {
  const plain = plainText(text);
  const url = plain.match(CODEX_URL)?.[0];
  const code = plain.match(CODEX_CODE)?.[1];
  const minutes = Number(plain.match(/expires in (\d+) minutes?/i)?.[1]);
  return {
    ...(url ? { url } : {}),
    ...(code ? { user_code: code } : {}),
    ...(minutes > 0 ? { expires_in_s: minutes * 60 } : {}),
  };
}

function storeClaudeToken(dir, token) {
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const path = join(dir, TOKEN_FILE);
  writeFileSync(`${path}.tmp`, `${token}\n`, { mode: 0o600 });
  renameSync(`${path}.tmp`, path);
  try {
    chmodSync(path, 0o600);
  } catch {
    // Windows has no modes
  }
}

/**
 * Runs one sign-in. `send(update)` gets what the console shows, as
 * {state, url?, user_code?, expires_in_s?, plan?, message?}: starting, then
 * open_url (Codex: open the link and enter the code) or enter_code (Claude
 * Code: open the link, sign in, paste the code), then verifying, connected or
 * failed. Returns {code(text), cancel()}; `done` resolves with the last state.
 */
export function startLogin({ runtime, agentId, send, stateDir = defaultStateDir(), platform = process.platform,
  resolve, spawnImpl = spawn, reader, timeoutMs = LOGIN_TIMEOUT_MS, env = process.env } = {}) {
  const dir = loginDir(agentId, stateDir);
  let child = null;
  let finished = false;
  let output = "";
  let shown = "";
  let resolveDone;
  const done = new Promise((resolvePromise) => { resolveDone = resolvePromise; });
  const finish = (update) => {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    try { child?.kill(); } catch {}
    send(update);
    resolveDone(update.state);
  };
  const show = (update) => {
    const key = JSON.stringify(update);
    if (finished || key === shown) return;
    shown = key;
    send(update);
  };
  const timer = setTimeout(() => finish({ state: "failed",
    message: "The sign-in was not finished in time. Start it again when you are ready." }), timeoutMs);

  const fail = (message) => finish({ state: "failed", message });
  const target = (name) => (resolve ? resolve(name) : { command: name, prefix: [] });
  const verify = async (id, extraEnv) => {
    send({ state: "verifying" });
    const read = reader ?? (await import("./usage.mjs")).readAgent;
    const found = await read(id, { resolve: resolve ?? ((name) => ({ command: name, prefix: [] })),
      env: { ...env, ...extraEnv } }).catch(() => null);
    const plan = found?.usage?.plan;
    finish({ state: "connected", ...(plan ? { plan: String(plan).slice(0, 32) } : {}) });
  };

  send({ state: "starting" });
  if (runtime === "claude") {
    const agent = target("claude");
    const pty = agent && ptyCommand(agent.command, [...agent.prefix, "setup-token"], platform);
    if (!pty) {
      fail(platform === "win32" ? "Sign Claude Code in on this computer itself (run `claude`); Windows has no terminal Veynod can drive."
        : "Claude Code is not installed on this computer.");
    } else {
      try {
        child = spawnImpl(pty.command, pty.args, { env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
      } catch {
        child = null;
      }
      if (!child) {
        fail("This computer cannot start `script` (util-linux), which Claude Code's sign-in needs.");
      } else {
        let token = null;
        const seen = (chunk) => {
          output = (output + chunk).slice(-200_000);
          const now = readClaudeSetup(output);
          if (now.token && !token) {
            token = now.token;
            output = "";
            try {
              storeClaudeToken(dir, token);
            } catch {
              fail("The login could not be saved on this computer (is its disk full?).");
              return;
            }
            void verify("claude", { CLAUDE_CODE_OAUTH_TOKEN: token });
          } else if (now.refused) {
            fail("Claude did not accept that code. Start again and paste the newest code the page shows.");
          } else if (now.url && now.asking) {
            show({ state: "enter_code", url: now.url });
          }
        };
        child.stdout?.on("data", seen);
        child.stderr?.on("data", seen);
        child.on("error", () => fail("This computer cannot start `script` (util-linux), which Claude Code's sign-in needs."));
        child.on("exit", () => {
          if (!token) fail("Claude Code's sign-in ended before it finished. Start it again.");
        });
      }
    }
  } else if (runtime === "codex") {
    const agent = target("codex");
    const home = join(dir, "codex");
    if (!agent) {
      fail("Codex is not installed on this computer.");
    } else {
      try {
        mkdirSync(home, { recursive: true, mode: 0o700 });
        child = spawnImpl(agent.command, [...agent.prefix, "login", "--device-auth"],
          { env: { ...env, CODEX_HOME: home }, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
      } catch {
        child = null;
      }
      if (!child) {
        fail("Codex could not be started on this computer.");
      } else {
        const seen = (chunk) => {
          output = (output + chunk).slice(-50_000);
          const now = readCodexDevice(output);
          if (now.url && now.user_code) show({ state: "open_url", ...now });
        };
        child.stdout?.on("data", seen);
        child.stderr?.on("data", seen);
        child.on("error", () => fail("Codex could not be started on this computer."));
        child.on("exit", (code) => {
          if (finished) return;
          if (code === 0 && existsSync(join(home, "auth.json"))) void verify("codex", { CODEX_HOME: home });
          else fail("The ChatGPT sign-in did not finish (the code expired or was cancelled). Start it again.");
        });
      }
    }
  } else {
    fail(`Veynod cannot sign ${String(runtime).slice(0, 32)} in from the console.`);
  }
  return {
    done,
    /** The code the person pasted (Claude Code only). */
    code(text) {
      if (finished || runtime !== "claude" || !child?.stdin) return false;
      const clean = String(text || "").trim();
      if (!/^[A-Za-z0-9#_.~-]{8,2048}$/.test(clean)) return false;
      child.stdin.write(`${clean}\r`);
      return true;
    },
    cancel() {
      finish({ state: "cancelled" });
    },
  };
}

/** Forgets the logins made through Veynod for this identity (its agents' own logins stay). */
export function forgetLogins(agentId, { stateDir = defaultStateDir() } = {}) {
  rmSync(loginDir(agentId, stateDir), { recursive: true, force: true });
}
