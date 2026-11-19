// The coding agents `npx veynod` recognises on this computer, and how each one
// is given Veynod's MCP server.
//
// Recognising is one thing and wiring another. An agent is recognised when its
// command is on PATH or its own configuration folder exists. It is wired
// automatically only where the file and the shape of an MCP entry are known
// (`mcp` below); for the others `npx veynod` says it found the agent and prints
// the entry to add by hand, rather than write a guess into someone's config.
//
// Folders are the ones each agent documents for its own settings. Wired agents
// take a remote (streamable HTTP) server with an Authorization header.

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir, platform } from "node:os";
import { dirname, join } from "node:path";

const bearer = (key) => ({ Authorization: `Bearer ${key}` });
const config = (home, ...parts) => join(home, ".config", ...parts);

/** VS Code's user folder, per system. */
export function vscodeUserDir(home = homedir(), os = platform(), env = process.env) {
  if (os === "darwin") return join(home, "Library", "Application Support", "Code", "User");
  if (os === "win32") return join(env.APPDATA || join(home, "AppData", "Roaming"), "Code", "User");
  return config(home, "Code", "User");
}

/**
 * id, name, how it is found (`bins` on PATH, `dirs` under the home folder) and,
 * where known, `mcp`: {file(home, os, env), key, entry(url, key)} for a JSON
 * file whose `key` object holds the servers, or {toml: true} for Codex.
 */
export const AGENT_CLIENTS = [
  { id: "codex", name: "Codex", bins: ["codex"], dirs: [".codex"],
    mcp: { toml: true, file: (home) => join(home, ".codex", "config.toml") } },
  { id: "gemini-cli", name: "Gemini CLI", bins: ["gemini"], dirs: [".gemini"],
    mcp: { file: (home) => join(home, ".gemini", "settings.json"), key: "mcpServers",
      entry: (url, key) => ({ httpUrl: url, headers: bearer(key) }) } },
  { id: "opencode", name: "OpenCode", bins: ["opencode"], dirs: [".config/opencode"],
    mcp: { file: (home) => config(home, "opencode", "opencode.json"), key: "mcp",
      entry: (url, key) => ({ type: "remote", url, enabled: true, headers: bearer(key) }) } },
  { id: "mimo-code", name: "MiMo Code", bins: ["mimocode", "mimo"], dirs: [".config/mimocode"] },
  { id: "deveco-code", name: "DevEco Code", bins: ["deveco"], dirs: [".config/deveco"] },
  { id: "pi", name: "Pi", bins: ["pi"], dirs: [".pi/agent"] },
  { id: "goose", name: "Goose", bins: ["goose"], dirs: [".config/goose"] },
  { id: "cursor-cli", name: "Cursor CLI", bins: ["cursor-agent"], dirs: [],
    // Cursor's command-line agent reads the same file as the editor.
    mcp: { file: (home) => join(home, ".cursor", "mcp.json"), key: "mcpServers",
      entry: (url, key) => ({ url, headers: bearer(key) }) } },
  { id: "copilot-cli", name: "Copilot CLI", bins: ["copilot"], dirs: [".copilot"],
    mcp: { file: (home) => join(home, ".copilot", "mcp-config.json"), key: "mcpServers",
      entry: (url, key) => ({ type: "http", url, headers: bearer(key), tools: ["*"] }) } },
  { id: "vscode", name: "VS Code", bins: ["code"], dirs: [],
    found: (home, os, env) => existsSync(vscodeUserDir(home, os, env)),
    mcp: { file: (home, os, env) => join(vscodeUserDir(home, os, env), "mcp.json"), key: "servers",
      entry: (url, key) => ({ type: "http", url, headers: bearer(key) }) } },
  { id: "crush", name: "Crush", bins: ["crush"], dirs: [".config/crush"],
    mcp: { file: (home) => config(home, "crush", "crush.json"), key: "mcp",
      entry: (url, key) => ({ type: "http", url, headers: bearer(key) }) } },
  { id: "deepseek-harness", name: "DeepSeek Harness", bins: ["dsh"], dirs: [".dsh"] },
  { id: "command-code", name: "Command Code", bins: ["commandcode", "cmd-code"], dirs: [".commandcode"] },
  { id: "fx", name: "fx", bins: [], dirs: [".fx"] },
  { id: "omp", name: "omp", bins: ["omp"], dirs: [".omp/agent"] },
  { id: "omo", name: "OmO", bins: ["omo"], dirs: [".omo/agent"] },
  { id: "devin", name: "Devin", bins: ["devin"], dirs: [".config/devin"] },
  { id: "hermes", name: "Hermes Agent", bins: ["hermes"], dirs: [".hermes"] },
  { id: "mister-morph", name: "Mister Morph", bins: ["morph"], dirs: [".morph"] },
  { id: "kimi-code", name: "Kimi Code", bins: ["kimi"], dirs: [".kimi"],
    mcp: { file: (home) => join(home, ".kimi", "mcp.json"), key: "mcpServers",
      entry: (url, key) => ({ url, transport: "http", headers: bearer(key) }) } },
  { id: "muse-code", name: "Muse Code", bins: ["muse"], dirs: [".config/muse"] },
  { id: "empryo", name: "Empryo", bins: ["empryo"], dirs: [".empryo"] },
  { id: "minimax-code", name: "MiniMax Code", bins: ["mcode"], dirs: [".minimax"] },
  { id: "droid", name: "Droid", bins: ["droid"], dirs: [".factory"],
    mcp: { file: (home) => join(home, ".factory", "mcp.json"), key: "mcpServers",
      entry: (url, key) => ({ type: "http", url, headers: bearer(key) }) } },
  { id: "grok-build", name: "Grok Build", bins: ["grok"], dirs: [".grok"] },
  { id: "zcode", name: "ZCode", bins: ["zcode"], dirs: [".zcode"] },
  { id: "openhanako", name: "OpenHanako", bins: ["hanako"], dirs: [".hanako"] },
  { id: "atomcode", name: "AtomCode", bins: ["atomcode"], dirs: [".atomcode"] },
  { id: "snow-cli", name: "Snow CLI", bins: ["snow"], dirs: [".snow"] },
  { id: "qwenpaw", name: "QwenPaw", bins: ["qwenpaw", "copaw"], dirs: [".qwenpaw", ".copaw"] },
  { id: "alma", name: "Alma", bins: ["alma"], dirs: [".alma"] },
];

export const AGENT_CLIENT_IDS = AGENT_CLIENTS.map((item) => item.id);
export const agentClient = (id) => AGENT_CLIENTS.find((item) => item.id === id) || null;

/** The agents of the list above that are on this computer. */
export function detectAgentClients({ home = homedir(), os = platform(), env = process.env,
  onPath = () => false, exists = existsSync } = {}) {
  return AGENT_CLIENTS.filter((item) => item.bins.some((bin) => onPath(bin))
    || item.dirs.some((dir) => exists(join(home, ...dir.split("/"))))
    || (item.found ? item.found(home, os, env) : false)).map((item) => item.id);
}

function readJson(path) {
  if (!existsSync(path)) return {};
  const text = readFileSync(path, "utf8").trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`${path} is not plain JSON (comments?); add the entry by hand`);
  }
}

/** Put one server under `key` in a JSON config, keeping everything else and a .bak. */
export function mergeAt(path, key, name, entry) {
  const current = readJson(path);
  const servers = current[key] && typeof current[key] === "object" ? current[key] : {};
  const next = { ...current, [key]: { ...servers, [name]: entry } };
  mkdirSync(dirname(path), { recursive: true });
  if (existsSync(path)) renameSync(path, `${path}.bak`);
  writeFileSync(path, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
  return path;
}

/**
 * Codex's config.toml with `[mcp_servers.<name>]` set to this server: an
 * earlier section of that name (and its sub-tables) is replaced, the rest of
 * the file is left as it is.
 */
export function codexTomlWith(text, name, url, key) {
  const lines = String(text || "").split("\n");
  const kept = [];
  let skipping = false;
  for (const line of lines) {
    const header = /^\s*\[\[?([^\]]+)\]\]?\s*(#.*)?$/.exec(line);
    if (header) {
      const table = header[1].trim();
      skipping = table === `mcp_servers.${name}` || table.startsWith(`mcp_servers.${name}.`);
    }
    if (!skipping) kept.push(line);
  }
  const body = kept.join("\n").replace(/\s+$/, "");
  const section = [`[mcp_servers.${name}]`, `url = ${JSON.stringify(url)}`,
    `http_headers = { Authorization = ${JSON.stringify(`Bearer ${key}`)} }`].join("\n");
  return `${body ? `${body}\n\n` : ""}${section}\n`;
}

/**
 * Give one agent Veynod's server. {wired: path} when its file was written,
 * {manual: entry} when the agent is known but its MCP file is not: the caller
 * shows the entry to add by hand.
 */
export function wireAgentClient(id, url, key, { home = homedir(), os = platform(), env = process.env,
  name = "veynod" } = {}) {
  const client = agentClient(id);
  if (!client) throw new Error(`unknown client ${id}`);
  if (!client.mcp) return { manual: { url, headers: bearer(key) } };
  const file = client.mcp.file(home, os, env);
  if (client.mcp.toml) {
    const before = existsSync(file) ? readFileSync(file, "utf8") : "";
    mkdirSync(dirname(file), { recursive: true });
    if (before) writeFileSync(`${file}.bak`, before, { mode: 0o600 });
    writeFileSync(file, codexTomlWith(before, name, url, key), { mode: 0o600 });
    return { wired: file };
  }
  return { wired: mergeAt(file, client.mcp.key, name, client.mcp.entry(url, key)) };
}
