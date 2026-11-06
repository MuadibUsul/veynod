#!/usr/bin/env node
// The worker as people get it, on this machine's OS (CI runs it on Linux,
// macOS and Windows): pack the CLI, install it into a fresh directory the way
// `npx veynod` does, then check what only a real install can break.
//   1. The installed CLI answers with its own version.
//   2. `up --dry-run` plans a background service in this OS's own format.
//   3. The copy the service runs from (~/.veynod/cli/<version>/) loads every
//      module and can look for agents, from outside the npx directory. In
//      0.6.0 that copy could not load its dependencies on Windows.
//   4. The launcher the service starts runs that copy, and the copy updates
//      itself: npm installs a newer veynod (packed here, one patch version up)
//      beside it, and the launcher starts the new one.
// Usage: node scripts/worker_smoke.mjs

import assert from "node:assert/strict";
import { execFileSync, execSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { platform, tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const cliDir = fileURLToPath(new URL("../AgentGraph/cli/", import.meta.url));
const version = JSON.parse(readFileSync(join(cliDir, "package.json"), "utf8")).version;
const work = mkdtempSync(join(tmpdir(), "veynod-smoke-"));
const home = join(work, "home");
mkdirSync(home);
const env = { ...process.env, HOME: home, USERPROFILE: home, VEYNOD_HOST: "", VEYNOD_KEY: "" };
const node = (args, options = {}) => execFileSync(process.execPath, args,
  { encoding: "utf8", env, timeout: 120_000, ...options });
const step = (text) => console.log(`- ${text}`);

console.log(`worker smoke on ${platform()} ${process.arch}, Node ${process.versions.node}, veynod ${version}`);

// npm is npm.cmd on Windows, so it goes through the shell everywhere.
const pack = (dir) => execSync(`npm pack "${dir}" --pack-destination "${work}" --silent`,
  { encoding: "utf8", env }).trim().split(/\r?\n/).at(-1);
const tarball = pack(cliDir);
const npxDir = join(work, "_npx", "smoke");
mkdirSync(npxDir, { recursive: true });
writeFileSync(join(npxDir, "package.json"), "{}\n");
execSync(`npm install --no-audit --no-fund --silent "${join(work, tarball)}"`, { cwd: npxDir, env, stdio: "inherit" });
const script = join(npxDir, "node_modules", "veynod", "bin", "veynod.mjs");
step(`packed ${tarball} and installed it into a fresh directory`);

assert.match(node([script, "--version"]), new RegExp(version.replace(/\./g, "\\.")));
step(`the installed CLI answers ${version}`);

// CI machines have no MCP client to wire (Claude Code, Cursor, Claude Desktop): print instead.
const output = node([script, "up", "--dry-run", "--yes", "--no-open", "--client", "print", "--run-tasks",
  "--runtime", "custom", "--exec", "echo hi", "--host", "https://veynod.example"]);
const plan = JSON.parse(output.slice(output.indexOf("{")));
assert.equal(plan.run_tasks, true);
assert.equal(plan.hub.mode, "runner");
const service = plan.service.files.map((file) => (typeof file === "string" ? file : file.path)).join(" ");
const expected = { linux: /\.config[\\/]systemd[\\/]user[\\/].+\.service/, darwin: /Library[\\/]LaunchAgents[\\/].+\.plist/,
  win32: /\.veynod[\\/].+\.vbs/ }[platform()];
assert.ok(expected, `no service format for ${platform()}`);
assert.match(service, expected);
step(`up --dry-run plans its background service as ${service}`);

const { installStableCopy } = await import(pathToFileURL(join(npxDir, "node_modules", "veynod", "bin", "up.mjs")).href);
const copy = installStableCopy(script, { version, home });
assert.ok(!copy.includes("_npx"), "the service copy lives outside the npx directory");
assert.match(node([copy, "--version"]), new RegExp(version.replace(/\./g, "\\.")));
const runner = pathToFileURL(join(copy, "..", "runner.mjs")).href;
const found = JSON.parse(node(["--input-type=module", "-e",
  `const m = await import(${JSON.stringify(runner)}); console.log(JSON.stringify(await m.inventory({ details: false })));`]));
assert.deepEqual(found.map((item) => item.id), ["claude", "codex"]);
step(`the service copy at ${copy} loads and looks for agents: ${found.map((item) => `${item.id} ${item.available ? "found" : "absent"}`).join(", ")}`);

// What `up` does next, then what the running hub does when npm has a newer veynod.
const update = await import(pathToFileURL(join(copy, "..", "update.mjs")).href);
const cliRoot = join(home, ".veynod", "cli");
const launcher = update.installLauncher(version, cliRoot);
assert.match(node([launcher, "--version"]), new RegExp(version.replace(/\./g, "\\.")));
assert.ok(update.confirmRunning(version, cliRoot));
step(`the launcher at ${launcher} starts ${version}`);

const [major, minor, patch] = version.split(".").map(Number);
const next = `${major}.${minor}.${patch + 1}`;
const bumped = join(work, "next");
cpSync(cliDir, bumped, { recursive: true, filter: (path) => !/[\\/]node_modules([\\/]|$)/.test(path) });
const manifest = JSON.parse(readFileSync(join(bumped, "package.json"), "utf8"));
writeFileSync(join(bumped, "package.json"), `${JSON.stringify({ ...manifest, version: next }, null, 2)}\n`);
const source = readFileSync(join(bumped, "bin", "veynod.mjs"), "utf8");
assert.ok(source.includes(`const VERSION = "${version}";`), "veynod.mjs names its version");
writeFileSync(join(bumped, "bin", "veynod.mjs"), source.replace(`const VERSION = "${version}";`, `const VERSION = "${next}";`));
const nextTarball = join(work, pack(bumped));
const result = await update.checkForUpdate({ version, root: cliRoot, launched: launcher,
  fetchImpl: async () => ({ ok: true, json: async () => ({ version: next }) }),
  install: (wanted, root) => update.npmInstall(wanted, root, { spec: nextTarball }) });
assert.deepEqual(result, { installed: next, text: `installed ${next}` });
assert.match(node([launcher, "--version"]), new RegExp(next.replace(/\./g, "\\.")));
assert.ok(update.confirmRunning(next, cliRoot));
assert.deepEqual(readdirSync(cliRoot).sort(), [version, next, "state.json", "veynod.mjs"].sort());
step(`npm installed ${next} beside it and the launcher now starts ${next}, keeping ${version} for a rollback`);

console.log("worker smoke passed");
