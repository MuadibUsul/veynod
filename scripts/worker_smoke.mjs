#!/usr/bin/env node
// The worker as people get it, on this machine's OS (CI runs it on Linux,
// macOS and Windows): pack the CLI, install it into a fresh directory the way
// `npx veynod` does, then check what only a real install can break.
//   1. The installed CLI answers with its own version.
//   2. `up --dry-run` plans a background service in this OS's own format.
//   3. The copy the service runs from (~/.veynod/cli/<version>/) loads every
//      module and can look for agents, from outside the npx directory. In
//      0.6.0 that copy could not load its dependencies on Windows.
// Usage: node scripts/worker_smoke.mjs

import assert from "node:assert/strict";
import { execFileSync, execSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
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
const tarball = execSync(`npm pack "${cliDir}" --pack-destination "${work}" --silent`,
  { encoding: "utf8", env }).trim().split(/\r?\n/).at(-1);
const npxDir = join(work, "_npx", "smoke");
mkdirSync(npxDir, { recursive: true });
writeFileSync(join(npxDir, "package.json"), "{}\n");
execSync(`npm install --no-audit --no-fund --silent "${join(work, tarball)}"`, { cwd: npxDir, env, stdio: "inherit" });
const script = join(npxDir, "node_modules", "veynod", "bin", "veynod.mjs");
step(`packed ${tarball} and installed it into a fresh directory`);

assert.match(node([script, "--version"]), new RegExp(version.replace(/\./g, "\\.")));
step(`the installed CLI answers ${version}`);

const output = node([script, "up", "--dry-run", "--yes", "--no-open", "--run-tasks", "--runtime", "custom",
  "--exec", "echo hi", "--host", "https://veynod.example"]);
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

console.log("worker smoke passed");
