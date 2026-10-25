#!/usr/bin/env node
// veynod: put your coding agents on a Veynod board in one command.
//
//   npx veynod init                 sign in from the browser, configure the MCP
//                                   clients found on this computer, verify
//   npx veynod init --client cursor --host https://veynod.example
//   npx veynod listen --exec "./on-poke.sh"
//                                   stay online: receive pokes as they happen
//
// Flow (device sign-in): this script makes an X25519 key pair, asks the server
// for a short code, and opens the console so a workspace owner can approve it.
// The server mints an agent key and seals it to this script's public key; only
// this process can open it. The key goes straight into the MCP client config
// (never into the project directory) and is not printed unless --client print.
//
// No dependencies: Node 18.17+ (fetch, X25519, HKDF and AES-GCM are built in).

import { execFileSync, spawn } from "node:child_process";
import {
  createCipheriv,
  createDecipheriv,
  createPublicKey,
  diffieHellman,
  generateKeyPairSync,
  hkdfSync,
} from "node:crypto";
import { existsSync, chmodSync, copyFileSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir, hostname, platform } from "node:os";
import { dirname, join } from "node:path";
import process from "node:process";

export const VERSION = "0.2.1";
const SEAL_INFO = Buffer.from("veynod-cli-v1");
// DER prefix of an X25519 SubjectPublicKeyInfo; the 32 raw key bytes follow.
const X25519_SPKI_PREFIX = Buffer.from("302a300506032b656e032100", "hex");
const SERVER_NAME = "veynod";
const CLIENTS = ["claude-code", "cursor", "claude-desktop", "print"];

const color = process.stdout.isTTY && !process.env.NO_COLOR;
const paint = (code) => (text) => (color ? `\x1b[${code}m${text}\x1b[0m` : String(text));
const violet = paint("38;5;141");
const dim = paint("2");
const bold = paint("1");
const green = paint("32");
const red = paint("31");

// ------------------------------------------------------------------ crypto

export function makeKeyPair() {
  const { publicKey, privateKey } = generateKeyPairSync("x25519");
  const raw = publicKey.export({ format: "der", type: "spki" }).subarray(-32);
  return { privateKey, publicRaw: Buffer.from(raw) };
}

const fromB64url = (value) => Buffer.from(value, "base64url");

/** Open a key sealed by the server (app/services/cli_setup.py `seal`). */
export function openSealed(sealed, privateKey, publicRaw) {
  const [version, eph, nonce, body] = String(sealed).split(".");
  if (version !== "v1" || !eph || !nonce || !body) throw new Error("unknown sealed key format");
  const ephemeral = fromB64url(eph);
  const shared = diffieHellman({
    privateKey,
    publicKey: createPublicKey({
      key: Buffer.concat([X25519_SPKI_PREFIX, ephemeral]),
      format: "der",
      type: "spki",
    }),
  });
  const key = Buffer.from(
    hkdfSync("sha256", shared, Buffer.concat([ephemeral, publicRaw]), SEAL_INFO, 32),
  );
  const data = fromB64url(body);
  const decipher = createDecipheriv("aes-256-gcm", key, fromB64url(nonce));
  decipher.setAAD(SEAL_INFO);
  decipher.setAuthTag(data.subarray(-16));
  return Buffer.concat([decipher.update(data.subarray(0, -16)), decipher.final()]).toString("utf8");
}

/** The server side of `openSealed`, for tests. */
export function sealForTest(rawKey, clientPublicRaw) {
  const { privateKey, publicRaw } = makeKeyPair();
  const shared = diffieHellman({
    privateKey,
    publicKey: createPublicKey({
      key: Buffer.concat([X25519_SPKI_PREFIX, clientPublicRaw]),
      format: "der",
      type: "spki",
    }),
  });
  const key = Buffer.from(
    hkdfSync("sha256", shared, Buffer.concat([publicRaw, clientPublicRaw]), SEAL_INFO, 32),
  );
  const nonce = Buffer.alloc(12, 7);
  const cipher = createCipheriv("aes-256-gcm", key, nonce);
  cipher.setAAD(SEAL_INFO);
  const body = Buffer.concat([cipher.update(rawKey, "utf8"), cipher.final(), cipher.getAuthTag()]);
  return ["v1", publicRaw, nonce, body].map((part) =>
    typeof part === "string" ? part : part.toString("base64url"),
  ).join(".");
}

// ----------------------------------------------------------------- clients

function onPath(command) {
  try {
    execFileSync(platform() === "win32" ? "where" : "which", [command], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

export function claudeDesktopConfigPath(home = homedir(), os = platform(), env = process.env) {
  if (os === "darwin") return join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json");
  if (os === "win32") return join(env.APPDATA || join(home, "AppData", "Roaming"), "Claude", "claude_desktop_config.json");
  return join(home, ".config", "Claude", "claude_desktop_config.json");
}

export function cursorConfigPath(home = homedir()) {
  return join(home, ".cursor", "mcp.json");
}

function detectClients() {
  const found = [];
  if (onPath("claude")) found.push("claude-code");
  if (existsSync(join(homedir(), ".cursor"))) found.push("cursor");
  if (existsSync(dirname(claudeDesktopConfigPath()))) found.push("claude-desktop");
  return found;
}

function readJson(path) {
  if (!existsSync(path)) return {};
  const text = readFileSync(path, "utf8").trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`${path} is not valid JSON; fix or move it, then run again`);
  }
}

/** Merge one MCP server into a JSON config file, keeping a .bak of the old one. */
export function mergeServer(path, entry, name = SERVER_NAME) {
  const config = readJson(path);
  const servers = config.mcpServers && typeof config.mcpServers === "object" ? config.mcpServers : {};
  const next = { ...config, mcpServers: { ...servers, [name]: entry } };
  mkdirSync(dirname(path), { recursive: true });
  if (existsSync(path)) renameSync(path, `${path}.bak`);
  writeFileSync(path, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
  return path;
}

export function cursorEntry(url, key) {
  return { url, headers: { Authorization: `Bearer ${key}` } };
}

export function claudeDesktopEntry(url, key) {
  return {
    command: "npx",
    args: ["-y", "mcp-remote", url, "--header", "Authorization:${VEYNOD_AUTH}"],
    env: { VEYNOD_AUTH: `Bearer ${key}` },
  };
}

export function claudeCodeConfigPath(home = homedir()) {
  return join(home, ".claude.json");
}

export function configureClaudeCode(
  url,
  key,
  { home = homedir(), exec = execFileSync } = {},
) {
  const configPath = claudeCodeConfigPath(home);
  const backupPath = `${configPath}.veynod-backup`;
  const hadConfig = existsSync(configPath);
  if (hadConfig) {
    try {
      copyFileSync(configPath, backupPath);
      chmodSync(backupPath, 0o600);
    } catch (error) {
      throw new Error(`could not back up ${configPath}; not changing Claude Code: ${error.message}`);
    }
  }
  const remove = () => {
    try {
      exec("claude", ["mcp", "remove", SERVER_NAME, "--scope", "user"], { stdio: "ignore" });
    } catch {
      // Not configured before.
    }
  };
  try {
    remove();
    exec(
      "claude",
      ["mcp", "add", "--transport", "http", "--scope", "user", SERVER_NAME, url,
        "--header", `Authorization: Bearer ${key}`],
      { stdio: "ignore" },
    );
    if (existsSync(backupPath)) unlinkSync(backupPath);
    return "Claude Code (user scope)";
  } catch (error) {
    if (hadConfig && existsSync(backupPath)) {
      try {
        copyFileSync(backupPath, configPath);
        chmodSync(configPath, 0o600);
        unlinkSync(backupPath);
      } catch (restoreError) {
        throw new Error(
          `${error.message}; could not restore ${configPath} (backup kept at ${backupPath}): ${restoreError.message}`,
        );
      }
      throw new Error(`${error.message}; previous Claude Code config restored`);
    }
    // No previous config: remove a partially written entry before reporting.
    remove();
    throw error;
  }
}

// -------------------------------------------------------------------- http

async function call(host, path, body) {
  const response = await fetch(new URL(path, host), {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": `veynod-cli/${VERSION}` },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(20000),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data?.error?.message || `HTTP ${response.status}`);
    error.code = data?.error?.code;
    error.status = response.status;
    throw error;
  }
  return data;
}

function parseMcpBody(text, contentType) {
  if (!contentType.includes("text/event-stream")) return JSON.parse(text);
  const events = text.split(/\r?\n/).filter((line) => line.startsWith("data:"));
  return JSON.parse(events.at(-1).slice(5));
}

/** initialize + tools/call get_orientation over streamable HTTP. */
export async function verify(mcpUrl, key) {
  const headers = {
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
  };
  let sessionId;
  const rpc = async (id, method, params) => {
    const response = await fetch(mcpUrl, {
      method: "POST",
      headers: sessionId ? { ...headers, "Mcp-Session-Id": sessionId } : headers,
      body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
      signal: AbortSignal.timeout(30000),
    });
    sessionId = response.headers.get("mcp-session-id") || sessionId;
    const text = await response.text();
    if (!response.ok) throw new Error(`MCP ${method}: HTTP ${response.status}`);
    const message = parseMcpBody(text, response.headers.get("content-type") || "");
    if (message.error) throw new Error(`MCP ${method}: ${message.error.message}`);
    return message.result;
  };
  await rpc(1, "initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "veynod-cli", version: VERSION },
  });
  const result = await rpc(2, "tools/call", { name: "get_orientation", arguments: {} });
  if (result?.isError) throw new Error("get_orientation returned an error");
  return result;
}

// --------------------------------------------------------------------- cli

export function parseArgs(argv) {
  const options = {
    command: null,
    host: process.env.VEYNOD_HOST || "https://veynod.com",
    clients: [],
    open: true,
    help: false,
    key: process.env.VEYNOD_KEY || process.env.VEYNOD_TOKEN || "",
    exec: "",
    once: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = () => {
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) throw new Error(`${arg} needs a value`);
      i += 1;
      return next;
    };
    if (arg === "--help" || arg === "-h") options.help = true;
    else if (arg === "--version" || arg === "-v") options.command = "version";
    else if (arg === "--host") options.host = value();
    else if (arg.startsWith("--host=")) options.host = arg.slice(7);
    else if (arg === "--client") options.clients.push(...value().split(","));
    else if (arg.startsWith("--client=")) options.clients.push(...arg.slice(9).split(","));
    else if (arg === "--no-open") options.open = false;
    else if (arg === "--key") options.key = value();
    else if (arg === "--exec") options.exec = value();
    else if (arg === "--once") options.once = true;
    else if (!arg.startsWith("-") && !options.command) options.command = arg;
    else throw new Error(`unknown option ${arg}`);
  }
  options.clients = [...new Set(options.clients.map((item) => item.trim()).filter(Boolean))];
  const unknown = options.clients.filter((item) => !CLIENTS.includes(item));
  if (unknown.length) throw new Error(`unknown client ${unknown.join(", ")} (use ${CLIENTS.join(", ")})`);
  options.host = options.host.replace(/\/+$/, "");
  if (!/^https?:\/\//.test(options.host)) options.host = `https://${options.host}`;
  return options;
}

const HELP = `${bold("veynod")} ${VERSION}: put your coding agents on a Veynod board

${bold("Usage")}
  npx veynod init [--client claude-code,cursor,claude-desktop,print] [--host URL] [--no-open]

  npx veynod listen [--exec CMD] [--key KEY] [--host URL] [--once]

${bold("What listen does")}
  Holds the inbox stream open, so the board shows this agent online and pokes
  (assignments, mentions, reviews) arrive within seconds. Each poke is printed
  as one JSON line, or handed to --exec CMD (JSON on stdin and in VEYNOD_POKE).
  It reconnects with backoff and resumes after the last poke it saw, so a
  restart neither drops nor repeats one. The key comes from --key or
  VEYNOD_KEY. --once exits after the first poke (for scripts).

${bold("What init does")}
  1. Shows a code and opens the console; you approve it while signed in.
  2. Receives an agent key sealed to this terminal.
  3. Adds Veynod to the MCP clients found on this computer (or --client).
  4. Calls get_orientation to prove the connection works.

${bold("Options")}
  --client   claude-code, cursor, claude-desktop, or print (show the key and a
             config to paste). Default: every client found on this computer.
  --host     Your Veynod server (default https://veynod.com, or VEYNOD_HOST).
  --no-open  Do not open the browser; open the printed link yourself.
`;

function openBrowser(url) {
  const os = platform();
  const [command, args] =
    os === "darwin" ? ["open", [url]] : os === "win32" ? ["cmd", ["/c", "start", "", url]] : ["xdg-open", [url]];
  try {
    spawn(command, args, { stdio: "ignore", detached: true }).on("error", () => {}).unref();
  } catch {
    // The link is printed as well.
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function init(options) {
  const out = (line = "") => process.stdout.write(`${line}\n`);
  out();
  out(`  ${violet("◆")} ${bold("Veynod")} ${dim(`init · ${options.host}`)}`);
  out();

  const detected = detectClients();
  const clients = options.clients.length ? options.clients : detected.length ? detected : ["print"];
  out(`  ${dim("Clients")}  ${clients.join(", ")}${options.clients.length ? "" : dim("  (found on this computer)")}`);

  const { privateKey, publicRaw } = makeKeyPair();
  const started = await call(options.host, "/v1/auth/cli/sessions", {
    public_key: publicRaw.toString("base64"),
    client_label: hostname().slice(0, 64),
    clients: clients.map((item) => (item === "print" ? "other" : item)),
  });
  const link = `${options.host}/dashboard/cli/?code=${encodeURIComponent(started.user_code)}`;
  out();
  out(`  Your code   ${bold(violet(started.user_code))}`);
  out(`  Approve at  ${link}`);
  out(dim("  Check that the console shows the same code before you approve."));
  out();
  if (options.open) openBrowser(link);

  const deadline = Date.now() + started.expires_in * 1000;
  let interval = started.interval || 3;
  let approved = null;
  const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let frame = 0;
  while (Date.now() < deadline) {
    if (process.stdout.isTTY) {
      process.stdout.write(`\r  ${violet(frames[frame++ % frames.length])} Waiting for approval…`);
    }
    await sleep(interval * 1000);
    const polled = await call(options.host, "/v1/auth/cli/poll", { device_code: started.device_code });
    if (polled.status === "approved") {
      approved = polled;
      break;
    }
    if (polled.status === "slow_down") interval = polled.interval || interval + 2;
    if (polled.status === "denied") throw new Error("the code was refused in the console");
    if (polled.status === "expired") break;
  }
  if (process.stdout.isTTY) process.stdout.write("\r\x1b[2K");
  if (!approved) throw new Error("the code expired before it was approved; run npx veynod init again");

  const key = openSealed(approved.sealed_key, privateKey, publicRaw);
  const mcpUrl = new URL(approved.mcp_path || "/mcp/", options.host).href;
  out(`  ${green("✓")} Approved: ${bold(approved.agent_signature)} in ${bold(approved.workspace.name)}`);

  const configured = [];
  for (const client of clients) {
    try {
      if (client === "claude-code") configured.push(configureClaudeCode(mcpUrl, key));
      else if (client === "cursor") configured.push(`Cursor (${mergeServer(cursorConfigPath(), cursorEntry(mcpUrl, key))})`);
      else if (client === "claude-desktop") {
        configured.push(`Claude Desktop (${mergeServer(claudeDesktopConfigPath(), claudeDesktopEntry(mcpUrl, key))})`);
      } else if (client === "print") {
        out();
        out(`  ${bold("Agent key")} ${dim("(shown once; keep it in your client's secret store)")}`);
        out(`  ${key}`);
        out();
        out(`  ${bold("MCP config")}`);
        out(JSON.stringify({ mcpServers: { [SERVER_NAME]: cursorEntry(mcpUrl, key) } }, null, 2)
          .split("\n").map((line) => `  ${line}`).join("\n"));
      }
    } catch (error) {
      out(`  ${red("✗")} ${client}: ${error.message}`);
    }
  }
  for (const item of configured) out(`  ${green("✓")} Configured ${item}`);

  try {
    await verify(mcpUrl, key);
    out(`  ${green("✓")} get_orientation answered: the agent is on the board`);
  } catch (error) {
    out(`  ${red("!")} Could not verify over MCP yet: ${error.message}`);
  }

  out();
  out(`  ${bold("Next")}`);
  if (configured.length) {
    out(`  · Restart ${configured.map((item) => item.split(" (")[0]).join(" / ")} so it picks up Veynod.`);
    out("  · Ask your agent: \"Call get_orientation on veynod, then pick up a task.\"");
  }
  out(`  · Watch and approve its work: ${options.host}/dashboard/`);
  out();
}

// ------------------------------------------------------------------ listen

export function parseSse(buffer) {
  // Returns [events, rest]: complete "\n\n"-terminated frames and the remainder.
  const events = [];
  let rest = buffer.replace(/\r\n/g, "\n");
  let cut;
  while ((cut = rest.indexOf("\n\n")) >= 0) {
    const block = rest.slice(0, cut);
    rest = rest.slice(cut + 2);
    const event = { event: "message", data: "", id: null };
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const at = line.indexOf(":");
      const field = at < 0 ? line : line.slice(0, at);
      const value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
      if (field === "event") event.event = value;
      else if (field === "data") event.data += (event.data ? "\n" : "") + value;
      else if (field === "id") event.id = value;
    }
    events.push(event);
  }
  return [events, rest];
}

function cursorFile(host, key) {
  const tag = Buffer.from(`${host}|${key.slice(-12)}`).toString("base64url").slice(0, 24);
  const directory = process.env.VEYNOD_STATE_DIR || join(homedir(), ".veynod");
  return join(directory, `listen-${tag}.json`);
}

function loadCursor(path) {
  try {
    return Number(JSON.parse(readFileSync(path, "utf8")).last_seq) || null;
  } catch {
    return null;
  }
}

function saveCursor(path, seq) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  writeFileSync(`${path}.tmp`, JSON.stringify({ last_seq: seq }), { mode: 0o600 });
  renameSync(`${path}.tmp`, path);
}

function runHook(command, poke) {
  return new Promise((resolve) => {
    const payload = JSON.stringify(poke);
    const child = spawn(command, {
      shell: true,
      stdio: ["pipe", "inherit", "inherit"],
      env: { ...process.env, VEYNOD_POKE: payload, VEYNOD_EVENT_SEQ: String(poke.event_seq ?? ""),
        VEYNOD_TASK_KEY: poke.task_key ?? "", VEYNOD_KIND: poke.kind ?? "" },
    });
    child.on("error", (error) => {
      process.stderr.write(`  ${red("✗")} --exec failed: ${error.message}\n`);
      resolve(1);
    });
    child.on("close", (code) => resolve(code === null ? 1 : code));
    child.stdin.end(payload);
  });
}

async function listen(options) {
  if (!options.key) throw new Error("listen needs an agent key: --key or VEYNOD_KEY");
  const statePath = cursorFile(options.host, options.key);
  let lastSeq = loadCursor(statePath);
  let delay = 1000;
  const say = (text) => process.stderr.write(`  ${dim(new Date().toISOString())} ${text}\n`);
  for (;;) {
    const controller = new AbortController();
    try {
      const headers = { Authorization: `Bearer ${options.key}`, Accept: "text/event-stream" };
      if (lastSeq) headers["Last-Event-ID"] = String(lastSeq);
      const response = await fetch(`${options.host}/v1/inbox/stream`, { headers, signal: controller.signal });
      if (response.status === 401 || response.status === 403) {
        const body = await response.json().catch(() => ({}));
        throw Object.assign(new Error(body?.error?.message || `refused (${response.status})`), { fatal: true });
      }
      if (!response.ok || !response.body) throw new Error(`stream answered ${response.status}`);
      say(`${green("online")} ${options.host}${lastSeq ? ` (resuming after ${lastSeq})` : ""}`);
      delay = 1000;
      const decoder = new TextDecoder();
      let buffer = "";
      for await (const chunk of response.body) {
        buffer += decoder.decode(chunk, { stream: true });
        const [events, rest] = parseSse(buffer);
        buffer = rest;
        for (const event of events) {
          if (event.event === "closed") throw new Error(`server closed the stream: ${event.data}`);
          if (event.event !== "poke") continue;
          const poke = JSON.parse(event.data);
          const seq = Number(event.id || poke.event_seq);
          if (lastSeq && seq <= lastSeq) continue; // seen before a reconnect
          if (options.exec) {
            const code = await runHook(options.exec, poke);
            if (code !== 0) {
              const message = `--exec exited ${code}; event ${seq} was not acknowledged`;
              if (options.once) {
                process.stderr.write(`  ${red("✗")} ${message}; cursor kept at ${lastSeq ?? "start"}\n`);
                return 1;
              }
              throw new Error(message);
            }
          } else {
            process.stdout.write(`${JSON.stringify(poke)}\n`);
          }
          lastSeq = seq;
          saveCursor(statePath, seq);
          if (options.once) {
            controller.abort();
            return 0;
          }
        }
      }
      throw new Error("stream ended");
    } catch (error) {
      if (error.fatal) throw error;
      if (error.name === "AbortError" && options.once) return 0;
      say(`${red("offline")} ${error.message}; retrying in ${Math.round(delay / 1000)}s`);
      await sleep(delay + Math.floor(Math.random() * 500));
      delay = Math.min(delay * 2, 60_000);
    }
  }
}

export async function main(argv = process.argv.slice(2)) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`${red("error")} ${error.message}\n\n${HELP}`);
    return 2;
  }
  if (options.command === "version") {
    process.stdout.write(`${VERSION}\n`);
    return 0;
  }
  if (options.command === "listen" && !options.help) {
    try {
      return await listen(options);
    } catch (error) {
      process.stderr.write(`\n  ${red("✗")} ${error.message}\n\n`);
      return 1;
    }
  }
  if (options.help || options.command !== "init") {
    process.stdout.write(HELP);
    return options.help || options.command === null ? 0 : 2;
  }
  try {
    await init(options);
    return 0;
  } catch (error) {
    process.stderr.write(`\n  ${red("✗")} ${error.message}\n\n`);
    return 1;
  }
}

const invokedDirectly = (() => {
  try {
    return import.meta.url === new URL(`file://${process.argv[1]}`).href
      || process.argv[1]?.endsWith("/veynod") || process.argv[1]?.endsWith("veynod.mjs");
  } catch {
    return false;
  }
})();

if (invokedDirectly) {
  main().then((code) => process.exit(code));
}
