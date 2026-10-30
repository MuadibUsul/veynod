#!/usr/bin/env node
// Copyright (c) 2026 Veynod. MIT licensed.
// veynod: put your coding agents on a Veynod board in one command.
//
//   npx veynod init                 sign in from the browser, configure the MCP
//                                   clients found on this computer, verify
//   npx veynod init --client cursor --host https://veynod.example
//   npx veynod listen --exec "./on-poke.sh"
//                                   stay online: receive pokes as they happen
//   veynod service install --key-file ~/.veynod/listen.key
//                                   run listen as a background service that
//                                   starts at login and restarts itself
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
import { dirname, join, posix } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

export const VERSION = "0.5.1";
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
const yellow = paint("33");
const print = (line = "") => process.stdout.write(`${line}\n`);

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

export function selectedClients(requested, detected) {
  const clients = requested.length ? requested : detected;
  if (!clients.length) {
    throw new Error("no supported MCP client found; install one or use --client print to show the key");
  }
  return clients;
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
    try {
      unlinkSync(backupPath);
    } catch {
      // A stale backup is harmless; the new config is already active.
    }
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

async function call(host, path, body, key = "") {
  const response = await fetch(new URL(path, host), {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": `veynod-cli/${VERSION}`,
      ...(key ? { Authorization: `Bearer ${key}` } : {}) },
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
    keyFile: "",
    heartbeat: true,
    dryRun: false,
    subcommand: null,
    runner: false,
    runtime: process.env.VEYNOD_RUNTIME || "claude",
    slots: Number(process.env.VEYNOD_SLOTS) || 1,
    repos: (process.env.VEYNOD_REPOS || "").split(",").map((item) => item.trim()).filter(Boolean),
    diskMb: Number(process.env.VEYNOD_DISK_MB) || 5120,
    gitBase: process.env.VEYNOD_GIT_BASE || "https://github.com/",
    runTasks: false,
    yes: false,
    name: "",
    fresh: false,
    service: true,
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
    else if (arg === "--key-file") options.keyFile = value();
    else if (arg === "--no-heartbeat") options.heartbeat = false;
    else if (arg === "--dry-run") options.dryRun = true;
    else if (arg === "--runner") options.runner = true;
    else if (arg === "--runtime") options.runtime = value();
    else if (arg === "--slots") options.slots = Number(value());
    else if (arg === "--repo") options.repos.push(...value().split(",").map((item) => item.trim()).filter(Boolean));
    else if (arg === "--disk-mb") options.diskMb = Number(value());
    else if (arg === "--git-base") options.gitBase = value();
    else if (arg === "--run-tasks") options.runTasks = true;
    else if (arg === "--yes" || arg === "-y") options.yes = true;
    else if (arg === "--name") options.name = value();
    else if (arg === "--fresh") options.fresh = true;
    else if (arg === "--no-service") options.service = false;
    else if (!arg.startsWith("-") && !options.command) options.command = arg;
    else if (!arg.startsWith("-") && options.command === "service" && !options.subcommand) {
      options.subcommand = arg;
    }
    else throw new Error(`unknown option ${arg}`);
  }
  options.clients = [...new Set(options.clients.map((item) => item.trim()).filter(Boolean))];
  const unknown = options.clients.filter((item) => !CLIENTS.includes(item));
  if (unknown.length) throw new Error(`unknown client ${unknown.join(", ")} (use ${CLIENTS.join(", ")})`);
  if (!Number.isInteger(options.slots) || options.slots < 1 || options.slots > 64) {
    throw new Error("--slots must be a whole number from 1 to 64");
  }
  if (!["claude", "codex", "custom"].includes(options.runtime)) {
    throw new Error(`unknown runtime ${options.runtime} (claude, codex or custom)`);
  }
  options.repos = [...new Set(options.repos)];
  const badRepo = options.repos.find((repo) => !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo));
  if (badRepo) throw new Error(`--repo takes owner/name, not ${badRepo}`);
  options.host = options.host.replace(/\/+$/, "");
  if (!/^https?:\/\//.test(options.host)) options.host = `https://${options.host}`;
  return options;
}

const HELP = `${bold("veynod")} ${VERSION}: put your coding agents on a Veynod board

${bold("Usage")}
  npx veynod up [--run-tasks] [--repo owner/name,...] [--runtime claude|codex|custom]
                [--slots N] [--name NAME] [--client ...] [--host URL] [--yes] [--fresh]
                [--no-service] [--dry-run]
      One command: connects this computer (one console approval for all its
      identities), wires your MCP clients, installs the hub service and runs the
      doctor. --no-service leaves starting \`veynod hub\` to your own supervisor.
  veynod hub      The service up installs: every identity in ~/.veynod/config.json,
                  one stream each, as runners with --run-tasks, else as listeners.
  veynod doctor   What is missing on this computer and on the board, with the fix.

  npx veynod init [--client claude-code,cursor,claude-desktop,print] [--host URL] [--no-open]

  npx veynod listen [--exec CMD] [--key KEY | --key-file PATH] [--host URL] [--once]
                    [--no-heartbeat]

  veynod runner [--key KEY | --key-file PATH] [--runtime claude|codex|custom]
                [--slots N] [--repo owner/name,...] [--disk-mb N] [--exec CMD]

  veynod service install|uninstall|status|audit [--runner] [--key KEY | --key-file PATH]
                    [--exec CMD] [--host URL] [--dry-run]

${bold("What listen does")}
  Holds the inbox stream open, so the board shows this agent online and pokes
  (assignments, mentions, reviews) arrive within seconds. Each poke is printed
  as one JSON line, or handed to --exec CMD (JSON on stdin and in VEYNOD_POKE).
  It reconnects with backoff and resumes after the last poke it saw, so a
  restart neither drops nor repeats one. The key comes from --key, --key-file
  or VEYNOD_KEY. --once exits after the first poke (for scripts). Every 30 s it
  also sends a loop heartbeat, so the board shows the agent offline within
  90 s of this process dying, even when a proxy keeps the stream half-open.

${bold("What runner does")}
  Makes this computer run agents for the board. It tells the platform what the
  computer can do (system, Claude Code / Codex with models and quota, --slots
  tasks at once, the --repo repositories it may work on) and sends a heartbeat
  every 30 s. When the platform places a task here it starts the runtime in a
  fresh git worktree on branch veynod/<task> (pushes of any other branch are
  refused) with a prompt to claim and do the task, and cleans up afterwards.
  Three failed starts leave the task for another computer. --runtime custom
  runs --exec CMD with the prompt in VEYNOD_PROMPT. Receipts are written to
  ~/.veynod/runner-receipts.jsonl. Use OAR (npm i -g @botiverse/oar, Node 24+)
  for quota-aware runs; without it the runtime's own CLI is used.

${bold("What service does")}
  install    Runs listen in the background: a systemd user service (Linux), a
             LaunchAgent (macOS) or a hidden logon task (Windows; no console
             window). It starts at login and restarts within 5 s if it exits.
             The key is kept in ~/.veynod/listen.key (mode 600).
  uninstall  Stops and removes it.  status  Shows whether it runs.
  audit      Lists scheduled tasks and services that start node, python,
             PowerShell, cmd or batch files, and flags those that open a
             visible console window. It changes nothing.
  --runner   Installs the runner instead of listen (key in ~/.veynod/runner.key).
  --dry-run  Prints the files and commands instead of applying them.

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

/** The console's device flow: a code you approve, a key sealed to this terminal. */
/**
 * The console's device flow. `identities` ({name, role_scope, why}) asks for
 * every identity in one approval (servers from 3.64): the first is this
 * terminal's own agent, the rest come back in `extra`, each with its own key.
 * `legacy` is true when the server predates that and minted only one.
 */
export async function connect(options, { clients = [], label = hostname(), out = () => {}, identities = [] } = {}) {
  const { privateKey, publicRaw } = makeKeyPair();
  const body = {
    public_key: publicRaw.toString("base64"),
    client_label: String(label).slice(0, 64),
    clients: clients.map((item) => (item === "print" ? "other" : item)),
  };
  let started;
  let legacy = false;
  try {
    started = await call(options.host, "/v1/auth/cli/sessions", identities.length ? { ...body, identities } : body);
  } catch (error) {
    if (!identities.length || error.status !== 422) throw error;
    legacy = true;
    started = await call(options.host, "/v1/auth/cli/sessions", body);
  }
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
  if (!approved) throw new Error("the code expired before it was approved; run the command again");

  const key = openSealed(approved.sealed_key, privateKey, publicRaw);
  const extra = (approved.identities || []).map((item) => ({ name: item.name, agent_id: item.agent_signature,
    role_scope: item.role_scope, key: openSealed(item.sealed_key, privateKey, publicRaw) }));
  const mcpUrl = new URL(approved.mcp_path || "/mcp/", options.host).href;
  return { key, approved, mcpUrl, extra, legacy };
}

/** Adds Veynod with this key to each MCP client; returns what was configured. */
export function wireClients(clients, mcpUrl, key, out = () => {}) {
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
  return configured;
}

async function init(options) {
  const out = (line = "") => process.stdout.write(`${line}\n`);
  out();
  out(`  ${violet("◆")} ${bold("Veynod")} ${dim(`init · ${options.host}`)}`);
  out();

  const detected = detectClients();
  const clients = selectedClients(options.clients, detected);
  out(`  ${dim("Clients")}  ${clients.join(", ")}${options.clients.length ? "" : dim("  (found on this computer)")}`);

  const { key, approved, mcpUrl } = await connect(options, { clients, label: hostname(), out });
  out(`  ${green("✓")} Approved: ${bold(approved.agent_signature)} in ${bold(approved.workspace.name)}`);
  const configured = wireClients(clients, mcpUrl, key, out);

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

// Presence follows the loop, not the connection (blueprint L2, P-A): a beat
// every 30 s; three missed and the board shows the agent offline.
export const HEARTBEAT_MS = 30_000;

export function startHeartbeat(host, key, { every = HEARTBEAT_MS, say = () => {}, fetchImpl = fetch } = {}) {
  let stopped = false;
  let failing = false;
  let timer = null;
  const stop = () => {
    stopped = true;
    if (timer) clearInterval(timer);
  };
  const beat = async () => {
    if (stopped) return;
    try {
      const response = await fetchImpl(`${host}/v1/presence/heartbeat`, {
        method: "POST",
        headers: { Authorization: `Bearer ${key}` },
        signal: AbortSignal.timeout(10_000),
      });
      if (response.status === 404) {
        stop(); // an older server: the open stream alone keeps the agent online
        return;
      }
      if (!response.ok) throw new Error(`heartbeat answered ${response.status}`);
      if (failing) say(`${green("heartbeat")} restored`);
      failing = false;
    } catch (error) {
      if (!failing) say(`${red("heartbeat")} ${error.message}`);
      failing = true;
    }
  };
  timer = setInterval(() => void beat(), every);
  timer.unref?.();
  void beat();
  return stop;
}

async function listen(options) {
  if (!options.key) throw new Error("listen needs an agent key: --key, --key-file or VEYNOD_KEY");
  const say = (text) => process.stderr.write(`  ${dim(new Date().toISOString())} ${text}\n`);
  const stopHeartbeat = options.heartbeat ? startHeartbeat(options.host, options.key, { say }) : () => {};
  try {
    return await follow(options, say);
  } finally {
    stopHeartbeat();
  }
}

export async function follow(options, say = () => {}) {
  const statePath = cursorFile(options.host, options.key);
  let lastSeq = loadCursor(statePath);
  let delay = 1000;
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
          // Pokes and direct messages both reach the hook; "message" frames carry the
          // conversation pointer in poke.message.
          if (event.event !== "poke" && event.event !== "message") continue;
          const poke = JSON.parse(event.data);
          const seq = Number(event.id || poke.event_seq);
          if (lastSeq && seq <= lastSeq) continue; // seen before a reconnect
          if (options.onPoke) {
            await options.onPoke(poke);
          } else if (options.exec) {
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

// ----------------------------------------------------------------- service

// Blueprint L4: the host runs as a service, not a terminal window. One plan per
// platform: the files to write and the commands to run, so --dry-run and the
// tests see exactly what install would do.
export const SERVICE_NAME = "veynod-listen";
const LAUNCHD_LABEL = "com.veynod.listen";
const WINDOWS_TASK = "VeynodListen";

const systemdQuote = (value) => `"${String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
const xmlEscape = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const winQuote = (value) => `"${String(value).replace(/"/g, '\\"')}"`;

export function listenArguments({ script, host, keyFile, exec }) {
  return [script, "listen", "--host", host, "--key-file", keyFile, ...(exec ? ["--exec", exec] : [])];
}

export function runnerArguments({ script, host, keyFile, exec, runtime = "claude", slots = 1, repos = [],
  diskMb = 5120 }) {
  return [script, "runner", "--host", host, "--key-file", keyFile, "--runtime", runtime,
    "--slots", String(slots), ...(repos.length ? ["--repo", repos.join(",")] : []),
    "--disk-mb", String(diskMb), ...(exec ? ["--exec", exec] : [])];
}

// The listen service and the runner service live side by side.
const SERVICE_NAMES = {
  listen: { unit: SERVICE_NAME, label: LAUNCHD_LABEL, task: WINDOWS_TASK, log: "listen.log",
    title: "Veynod listen: keep this agent online and receive pokes" },
  runner: { unit: "veynod-runner", label: "com.veynod.runner", task: "VeynodRunner", log: "runner.log",
    title: "Veynod runner: run agents for the board on this computer" },
  hub: { unit: "veynod-hub", label: "com.veynod.hub", task: "VeynodHub", log: "hub.log",
    title: "Veynod hub: this computer's agents on the board (veynod up)" },
};

/** The hub reads its identities from ~/.veynod/config.json. */
export function hubArguments({ script }) {
  return [script, "hub"];
}

export function servicePlan(action, {
  os = platform(),
  home = homedir(),
  node = process.execPath,
  script = fileURLToPath(import.meta.url),
  host,
  keyFile,
  exec = "",
  cwd = process.cwd(),
  mode = "listen",
  runner = {},
} = {}) {
  // --exec runs from the directory install ran in, so relative hooks resolve.
  const names = SERVICE_NAMES[mode];
  const args = mode === "hub" ? hubArguments({ script })
    : mode === "runner"
      ? runnerArguments({ script, host, keyFile, exec, ...runner })
      : listenArguments({ script, host, keyFile, exec });
  const stateDir = os === "win32" ? join(home, ".veynod") : posix.join(home, ".veynod");
  const log = os === "win32" ? join(stateDir, names.log) : posix.join(stateDir, names.log);
  const plan = { os, files: [], remove: [], commands: [], notes: [] };
  if (os === "linux") {
    const unit = posix.join(home, ".config", "systemd", "user", `${names.unit}.service`);
    if (action === "install") {
      plan.files.push({ path: unit, mode: 0o644, content: [
        "[Unit]",
        `Description=${names.title}`,
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        `WorkingDirectory=${systemdQuote(cwd)}`,
        `ExecStart=${[node, ...args].map(systemdQuote).join(" ")}`,
        "Restart=always",
        "RestartSec=5",
        "Environment=NO_COLOR=1",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
      ].join("\n") });
      plan.commands.push(["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", `${names.unit}.service`]);
      plan.notes.push("To start it at boot without a login session: loginctl enable-linger $USER",
        `Logs: journalctl --user -u ${names.unit} -f`);
    } else if (action === "uninstall") {
      plan.commands.push(["systemctl", "--user", "disable", "--now", `${names.unit}.service`],
        ["systemctl", "--user", "daemon-reload"]);
      plan.remove.push(unit);
    } else {
      plan.commands.push(["systemctl", "--user", "status", "--no-pager", `${names.unit}.service`]);
    }
  } else if (os === "darwin") {
    const plist = posix.join(home, "Library", "LaunchAgents", `${names.label}.plist`);
    if (action === "install") {
      plan.files.push({ path: plist, mode: 0o644, content: [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        `  <key>Label</key><string>${names.label}</string>`,
        "  <key>ProgramArguments</key>",
        "  <array>",
        ...[node, ...args].map((item) => `    <string>${xmlEscape(item)}</string>`),
        "  </array>",
        `  <key>WorkingDirectory</key><string>${xmlEscape(cwd)}</string>`,
        "  <key>RunAtLoad</key><true/>",
        "  <key>KeepAlive</key><true/>",
        "  <key>ThrottleInterval</key><integer>5</integer>",
        "  <key>EnvironmentVariables</key><dict><key>NO_COLOR</key><string>1</string></dict>",
        `  <key>StandardOutPath</key><string>${xmlEscape(log)}</string>`,
        `  <key>StandardErrorPath</key><string>${xmlEscape(log)}</string>`,
        "</dict>",
        "</plist>",
        "",
      ].join("\n") });
      plan.commands.push(["launchctl", "unload", plist], ["launchctl", "load", "-w", plist]);
      plan.notes.push(`Logs: ${log}`);
    } else if (action === "uninstall") {
      plan.commands.push(["launchctl", "unload", "-w", plist]);
      plan.remove.push(plist);
    } else {
      plan.commands.push(["launchctl", "list", names.label]);
    }
  } else if (os === "win32") {
    // wscript runs the loop with window style 0: no console window ever opens,
    // and the loop restarts listen 5 s after it exits.
    const launcher = join(stateDir, `${names.unit}.vbs`);
    if (action === "install") {
      const inner = [node, ...args].map(winQuote).join(" ");
      const line = `cmd /s /c "cd /d ${winQuote(cwd)} && ${inner} >> ${winQuote(log)} 2>&1"`;
      plan.files.push({ path: launcher, mode: 0o600, content: [
        `' ${names.title}, hidden: no console window; restarted 5 s after it exits.`,
        'Set shell = CreateObject("WScript.Shell")',
        "Do",
        `  shell.Run "${line.replace(/"/g, '""')}", 0, True`,
        "  WScript.Sleep 5000",
        "Loop",
        "",
      ].join("\r\n") });
      plan.commands.push(["schtasks", "/Create", "/TN", names.task, "/SC", "ONLOGON", "/RL", "LIMITED",
        "/TR", `wscript.exe //B //Nologo "${launcher}"`, "/F"],
      ["schtasks", "/Run", "/TN", names.task]);
      plan.notes.push(`Logs: ${log}`, "No console window opens; check with: veynod service status");
    } else if (action === "uninstall") {
      plan.commands.push(["schtasks", "/End", "/TN", names.task],
        ["schtasks", "/Delete", "/TN", names.task, "/F"]);
      plan.remove.push(launcher);
      plan.notes.push(`If ${mode} is still running, end the wscript.exe and node.exe processes it started.`);
    } else {
      plan.commands.push(["schtasks", "/Query", "/TN", names.task, "/V", "/FO", "LIST"]);
    }
  } else {
    throw new Error(`service is not supported on ${os}`);
  }
  return plan;
}

// ------------------------------------------------------------------ audit

// L4 step one: find the watchdogs before touching them. Nothing is changed.
const WATCHED = /\b(node|python[0-9.]*|pythonw|powershell|pwsh|cmd|bash|sh)(\.exe)?\b|\.(bat|cmd|ps1|py|js|mjs|sh)\b|veynod|claude|codex|cursor|listen/i;

export function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      row.push(cell);
      if (row.some((item) => item !== "")) rows.push(row);
      row = [];
      cell = "";
    } else cell += char;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

export function visibleWindow(action) {
  const command = action.toLowerCase();
  if (/wscript|cscript|pythonw|-windowstyle\s+hidden|-w\s+hidden|conhost(\.exe)?\s+--headless/.test(command)) return false;
  return /\b(cmd|powershell|pwsh|python|node|bash)(\.exe)?\b|\.(bat|cmd|ps1|py|js|mjs)\b/.test(command);
}

export function auditWindowsTasks(csv) {
  const rows = parseCsv(csv);
  const header = rows.find((row) => row.includes("TaskName"));
  if (!header) return [];
  const at = (name) => header.indexOf(name);
  const findings = [];
  const seen = new Set();
  for (const row of rows) {
    if (row === header || row[at("TaskName")] === "TaskName") continue;
    const name = row[at("TaskName")] ?? "";
    const action = row[at("Task To Run")] ?? "";
    if (name.startsWith("\\Microsoft\\") || !WATCHED.test(action)) continue;
    const key = `${name}|${action}`;
    if (seen.has(key)) continue; // one row per trigger: report the task once
    seen.add(key);
    findings.push({
      name,
      action,
      schedule: [row[at("Schedule Type")], row[at("Repeat: Every")]].filter(Boolean).join(", "),
      next_run: row[at("Next Run Time")] ?? "",
      status: row[at("Status")] ?? "",
      run_as: row[at("Run As User")] ?? "",
      visible_window: visibleWindow(action),
    });
  }
  return findings;
}

function capture(command, args) {
  try {
    return execFileSync(command, args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
  } catch {
    return "";
  }
}

function audit() {
  const os = platform();
  if (os === "win32") {
    const findings = auditWindowsTasks(capture("schtasks", ["/Query", "/FO", "CSV", "/V"]));
    print(bold(`Scheduled tasks that start a script or runtime (${findings.length})`));
    for (const item of findings) {
      print(`\n  ${bold(item.name)}  ${item.visible_window ? red("opens a console window") : green("hidden")}`);
      print(`    runs:     ${item.action}`);
      print(`    schedule: ${item.schedule || "?"}   next: ${item.next_run || "?"}   status: ${item.status || "?"}`);
      if (item.visible_window) {
        print(dim("    keep it, hide it: run the same command through wscript (see `veynod service install`),"));
        print(dim('    or add -WindowStyle Hidden for PowerShell; do not delete it before knowing what it guards.'));
      }
    }
    return findings.some((item) => item.visible_window) ? 3 : 0;
  }
  const lines = [];
  if (os === "linux") {
    lines.push(...capture("systemctl", ["--user", "list-units", "--type=service", "--all", "--no-pager", "--plain"])
      .split("\n").filter((line) => WATCHED.test(line)));
  } else if (os === "darwin") {
    lines.push(...capture("launchctl", ["list"]).split("\n").filter((line) => WATCHED.test(line)));
  }
  lines.push(...capture("crontab", ["-l"]).split("\n").filter((line) => line && !line.startsWith("#") && WATCHED.test(line)));
  print(bold(`Services and cron jobs that start a script or runtime (${lines.length})`));
  for (const line of lines) print(`  ${line.trim()}`);
  return 0;
}

function serviceKeyFile(options) {
  if (options.keyFile) return options.keyFile;
  const path = join(homedir(), ".veynod", options.runner ? "runner.key" : "listen.key");
  if (options.key) {
    if (!options.dryRun) {
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
      writeFileSync(path, `${options.key}\n`, { mode: 0o600 });
      chmodSync(path, 0o600);
    }
    return path;
  }
  if (existsSync(path)) return path;
  throw new Error("service install needs the agent key: --key, --key-file or VEYNOD_KEY");
}

async function service(options) {
  const action = options.subcommand;
  if (action === "audit") return audit();
  if (!["install", "uninstall", "status"].includes(action ?? "")) {
    throw new Error("service needs install, uninstall, status or audit");
  }
  const script = fileURLToPath(import.meta.url);
  const plan = servicePlan(action, {
    host: options.host,
    script,
    exec: options.exec,
    keyFile: action === "install" ? serviceKeyFile(options) : "",
    mode: options.runner ? "runner" : "listen",
    runner: { runtime: options.runtime, slots: options.slots, repos: options.repos, diskMb: options.diskMb },
  });
  if (action === "install" && /[\\/]_npx[\\/]/.test(script)) {
    plan.notes.unshift("This copy runs from the npx cache, which can be cleaned: install it first with "
      + "`npm install -g veynod`, then run `veynod service install` again.");
  }
  return applyServicePlan(plan, action, { dryRun: options.dryRun });
}

/** True when this copy is running out of npm's throwaway npx cache. */
export function isNpxCachePath(script) {
  return /[\\/]_npx[\\/]/.test(String(script)) || /[\\/]npm-cache[\\/]_npx[\\/]/.test(String(script));
}

/** A failed plan command, quoted so it can be pasted into cmd.exe and run by hand. */
export function manualCommand(command, args) {
  return [command, ...args]
    .map((word) => (/[\s"]/.test(word) ? `"${word.replace(/"/g, '""')}"` : word))
    .join(" ");
}

/** Writes a service plan's files and runs its commands (or prints them for --dry-run). */
export function applyServicePlan(plan, action, { dryRun = false } = {}) {
  if (dryRun) {
    for (const file of plan.files) print(`${bold("write")} ${file.path}\n${file.content}`);
    for (const path of plan.remove) print(`${bold("remove")} ${path}`);
    for (const command of plan.commands) print(`${bold("run")} ${command.join(" ")}`);
    for (const note of plan.notes) print(dim(note));
    return 0;
  }
  for (const file of plan.files) {
    mkdirSync(dirname(file.path), { recursive: true });
    writeFileSync(file.path, file.content, { mode: file.mode });
  }
  let failed = 0;
  for (const [command, ...args] of plan.commands) {
    try {
      execFileSync(command, args, { stdio: action === "status" ? "inherit" : "ignore" });
    } catch (error) {
      // Unloading what is not loaded yet is expected on a first install.
      const benign = action === "install" && args[0] === "unload";
      if (!benign) {
        failed += 1;
        // EPERM means the child never started: on Windows that is a local
        // security agent refusing to let node spawn it, not a bad command. A
        // raw "spawnSync schtasks EPERM" leaves the user with nothing to do, so
        // say what happened and print the command to run by hand.
        if (error.code === "EPERM" && process.platform === "win32") {
          process.stderr.write(`  ${red("✗")} ${command} ${args.join(" ")}: blocked before it ran `
            + "(local security software refuses to let node start it)\n");
          process.stderr.write(`      Run it yourself in cmd.exe:\n`);
          process.stderr.write(`        ${manualCommand(command, args)}\n`);
        } else {
          process.stderr.write(`  ${red("✗")} ${command} ${args.join(" ")}: ${error.message.split("\n")[0]}\n`);
        }
      }
    }
  }
  for (const path of plan.remove) {
    try {
      unlinkSync(path);
    } catch {
      // already gone
    }
  }
  if (action !== "status") {
    print(`${failed ? red("✗") : green("✓")} ${action} ${failed ? "finished with errors" : "done"}`);
    for (const note of plan.notes) print(dim(`  ${note}`));
  }
  return failed ? 1 : 0;
}

// --------------------------------------------------------------------- up

function ask(question) {
  return new Promise((resolve) => {
    process.stdout.write(question);
    process.stdin.setEncoding("utf8");
    process.stdin.once("data", (data) => {
      process.stdin.pause();
      resolve(String(data).trim().toLowerCase());
    });
  });
}

function printChecks(checks) {
  for (const item of checks) {
    const mark = item.ok ? green("✓") : item.required ? red("✗") : violet("!");
    print(`${mark} ${item.text}${item.ok || !item.fix ? "" : dim(`  → ${item.fix}`)}`);
  }
}

/** veynod up: connect this computer, wire its agents, install the hub, run the doctor. */
async function up(options) {
  const { doctor, doctorOk, loadConfig, saveConfig, storeKey, upPlan } = await import("./up.mjs");
  const out = (line = "") => process.stdout.write(`${line}\n`);
  out();
  out(`  ${violet("◆")} ${bold("Veynod")} ${dim(`up · ${options.host}`)}`);
  const clients = selectedClients(options.clients, detectClients());
  if (!options.runTasks && !options.yes && process.stdin.isTTY && !options.dryRun) {
    const answer = await ask("  Let this computer run board tasks unattended? (y/N) ");
    options.runTasks = answer === "y" || answer === "yes";
  }
  const plan = upPlan(options, { clients });
  const script = fileURLToPath(import.meta.url);
  const service = servicePlan("install", { host: options.host, script, mode: "hub" });
  if (options.dryRun) {
    out(JSON.stringify({ ...plan, service: { files: service.files.map((file) => file.path),
      commands: service.commands } }, null, 2));
    return 0;
  }
  const previous = loadConfig();
  const identities = [];
  let missing = [];
  for (const wanted of plan.identities) {
    const kept = !options.fresh && previous?.host === options.host
      && previous.identities?.find((item) => item.role === wanted.role && existsSync(item.key_file));
    if (kept) {
      out(`  ${green("✓")} ${kept.agent_id} is already connected`);
      identities.push(kept);
    } else {
      missing.push(wanted);
    }
  }
  // One approval for every identity still missing; an older server mints one
  // per code, so the rest are asked for again, one code each.
  let legacy = false;
  while (missing.length) {
    const asked = legacy ? missing.slice(0, 1) : missing;
    out();
    for (const item of asked) out(`  ${bold(item.label)} (${item.wants_role}): ${item.why}.`);
    if (asked.length > 1) out(`  One approval in the console connects ${asked.length === 2 ? "both" : "all of them"}.`);
    else if (legacy) out(`  In the console, approve it with role ${bold(asked[0].wants_role)}.`);
    const result = await connect(options, { clients: asked[0].role === "computer" ? clients : [],
      label: asked[0].label, out,
      identities: legacy ? [] : asked.map((item) => ({ name: item.label, role_scope: item.wants_role, why: item.why })) });
    const got = [{ wanted: asked[0], agentId: result.approved.agent_signature, key: result.key }];
    for (const item of result.extra) {
      const wanted = asked.find((entry) => entry !== asked[0] && entry.label === item.name);
      if (wanted) got.push({ wanted, agentId: item.agent_id, key: item.key });
    }
    for (const { wanted, agentId, key } of got) {
      identities.push({ role: wanted.role, agent_id: agentId, key_file: storeKey(agentId, key),
        workspace: result.approved.workspace?.name ?? null });
      out(`  ${green("✓")} Approved: ${bold(agentId)} in ${bold(result.approved.workspace?.name ?? "the workspace")}`);
      if (wanted.role === "computer") wireClients(clients, result.mcpUrl, key, out);
      try {
        await call(options.host, "/v1/agents/register", { agent_id: agentId,
          capabilities: wanted.role === "review" ? ["review"] : [options.runtime] }, key);
      } catch {
        // already registered, or registration is not open to this token
      }
    }
    const left = missing.filter((item) => !got.some((entry) => entry.wanted === item));
    if (result.legacy || legacy) {
      legacy = true;
      missing = left;
    } else {
      // What the owner unticked stays unconnected; the doctor says what that costs.
      for (const item of left) out(`  ${yellow("!")} ${item.label} was not approved`);
      missing = [];
    }
  }
  saveConfig({ host: options.host, run_tasks: plan.run_tasks, runtime: options.runtime, exec: options.exec,
    slots: options.slots, repos: options.repos, identities });
  out();
  let serviceInstalled;
  if (options.service && isNpxCachePath(script)) {
    // A service pointed into npm's npx cache breaks the first time npm cleans
    // it, and it breaks silently (the launcher just restarts a missing file).
    out();
    out(`  ${red("✗")} The hub service was not installed: this copy runs from the npx cache,`);
    out(`      which npm may clean at any time. Install it once, then run this again:`);
    out(`        ${bold("npm install -g veynod")}`);
    out(`        ${bold("veynod up --run-tasks --repo owner/name")}`);
    out(`      ${dim("The identities approved above are kept, so this does not ask again.")}`);
    serviceInstalled = () => false;
  } else if (options.service) {
    const installed = applyServicePlan(service, "install") === 0;
    serviceInstalled = () => installed && service.files.every((file) => existsSync(file.path));
  } else {
    out(`  ${dim("No service installed (--no-service): run `veynod hub` under your own supervisor.")}`);
  }
  out();
  out(`  ${bold("Doctor")}`);
  const checks = await settledDoctor(() => doctor({ serviceInstalled }), { doctorOk });
  printChecks(checks);
  out();
  return doctorOk(checks) ? 0 : 1;
}

// The board checks a hub's hello turns green; nothing else is worth waiting for.
const AFTER_HELLO = new Set(["board:hosts", "board:builder", "board:reviewer"]);

/**
 * The hub just started: give it time to say hello before the board's checks
 * count. Only the checks its hello answers are waited on; anything else red
 * (on this computer, or automation on the board) is answered at once.
 */
export async function settledDoctor(run, { doctorOk, tries = 15, waitMs = 2_000 } = {}) {
  let checks = await run();
  for (let attempt = 1; attempt < tries && !doctorOk(checks); attempt += 1) {
    const red = checks.filter((item) => !item.ok && item.required);
    if (!red.every((item) => AFTER_HELLO.has(item.id))) break;
    await sleep(waitMs);
    checks = await run();
  }
  return checks;
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
  if (options.keyFile && !options.help) {
    try {
      options.key = readFileSync(options.keyFile, "utf8").trim();
    } catch (error) {
      if (options.command !== "service") {
        process.stderr.write(`${red("error")} cannot read --key-file: ${error.message}\n`);
        return 2;
      }
    }
  }
  if (options.command === "service" && !options.help) {
    try {
      return await service(options);
    } catch (error) {
      process.stderr.write(`\n  ${red("✗")} ${error.message}\n\n`);
      return 1;
    }
  }
  if (options.command === "runner" && !options.help) {
    try {
      if (!options.key) throw new Error("runner needs the runner's agent key: --key, --key-file or VEYNOD_KEY");
      const { runRunner } = await import("./runner.mjs");
      const say = (text) => process.stderr.write(`  ${dim(new Date().toISOString())} ${text}\n`);
      return await runRunner(options, { follow, version: VERSION, say });
    } catch (error) {
      process.stderr.write(`\n  ${red("✗")} ${error.message}\n\n`);
      return 1;
    }
  }
  if (["up", "hub", "doctor"].includes(options.command) && !options.help) {
    try {
      if (options.command === "up") return await up(options);
      const hubModule = await import("./up.mjs");
      if (options.command === "doctor") {
        const checks = await hubModule.doctor();
        printChecks(checks);
        return hubModule.doctorOk(checks) ? 0 : 1;
      }
      const { runRunner } = await import("./runner.mjs");
      const say = (text) => process.stderr.write(`  ${dim(new Date().toISOString())} ${text}\n`);
      return await hubModule.runHub(options, { follow, listen: (opts) => listen(opts), runRunner,
        version: VERSION, say });
    } catch (error) {
      process.stderr.write(`\n  ${red("✗")} ${error.message}\n\n`);
      return 1;
    }
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
