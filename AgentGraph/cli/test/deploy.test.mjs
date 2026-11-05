import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { composeServices, deployEnv, deploySettings, hookArgs, imageVar, runDeploy } from "../bin/deploy.mjs";
import { placementOf, workerRunnerOptions } from "../bin/runner.mjs";
import { parseArgs, VERSION } from "../bin/veynod.mjs";

const D = (char) => `sha256:${char.repeat(64)}`;
const ID = "plc_01M6K1BE6QH6VKWN9XAWMD";

test("a release becomes one image variable per service, digest pinned", () => {
  assert.equal(imageVar("kernel-api"), "VEYNOD_IMAGE_KERNEL_API");
  const env = deployEnv({ api: { image: "ghcr.io/a/api", digest: D("a") },
    kernel: { image: "ghcr.io/a/kernel", digest: D("b") } });
  assert.equal(env, `VEYNOD_IMAGE_API=ghcr.io/a/api@${D("a")}\nVEYNOD_IMAGE_KERNEL=ghcr.io/a/kernel@${D("b")}\n`);
  assert.throws(() => deployEnv({ api: { image: "x\nEVIL=1", digest: D("a") } }));
  assert.throws(() => deployEnv({ api: { image: "x", digest: "latest" } }));
});

test("changed release services map to their compose services once", () => {
  const manifest = { kernel: { compose: ["kernel-api", "kernel-gate"] }, api: {} };
  assert.deepEqual(composeServices(manifest, ["kernel", "api", "gone"]), ["kernel-api", "kernel-gate", "api"]);
  assert.throws(() => composeServices({ api: { compose: ["a;rm -rf"] } }, ["api"]));
});

test("a deploy poke names no task and the worker gets the deploy directory", () => {
  assert.deepEqual(placementOf({ placement_id: ID, step: "deploy", task_key: "ignored" }),
    { taskKey: null, placementId: ID, step: "deploy" });
  const options = workerRunnerOptions({ VEYNOD_RUNNER_OPTIONS: JSON.stringify(
    { deploy: { dir: "/srv/app", file: "compose.yml", before: "./migrate.sh" } }) });
  assert.deepEqual(deploySettings(options.deploy), { dir: "/srv/app", file: "compose.yml", before: "./migrate.sh" });
  assert.equal(deploySettings({ dir: "relative" }), null);
  assert.deepEqual(hookArgs("  ./scripts/migrate_all.sh  --yes "), ["./scripts/migrate_all.sh", "--yes"]);
});

test("veynod up --deploy needs an absolute compose directory and takes no repos", () => {
  const options = parseArgs(["up", "--deploy", "--compose-dir", "/srv/app", "--compose-file", "c.yml"]);
  assert.equal(options.deploy, true);
  assert.equal(options.runTasks, true);
  assert.throws(() => parseArgs(["up", "--deploy"]), /compose-dir/);
  assert.throws(() => parseArgs(["up", "--deploy", "--compose-dir", "rel"]), /absolute/);
  assert.throws(() => parseArgs(["up", "--deploy", "--compose-dir", "/srv", "--repo", "a/b"]), /no --repo/);
});

/** A fake board: the placement, and every report the deploy computer sends. */
function board(detail, { health = [200] } = {}) {
  const reports = [];
  let probes = 0;
  const fetchImpl = async (url, init = {}) => {
    const reply = (status, body) => ({ ok: status < 300, status, json: async () => body });
    if (url === "https://health.test/ok") {
      const status = health[Math.min(probes, health.length - 1)];
      probes += 1;
      return reply(status, {});
    }
    if (url.endsWith(`/v1/runner/placements/${ID}`)) {
      return reply(200, { placement_id: ID, step: "deploy", status: "offered", detail });
    }
    if (url.endsWith(`/v1/runner/placements/${ID}/deploy-report`)) {
      reports.push(JSON.parse(init.body));
      return reply(200, {});
    }
    return reply(404, {});
  };
  return { fetchImpl, reports };
}

function detailFor({ rollback = true } = {}) {
  return {
    run_id: "r1",
    release: { commit: "b".repeat(40), manifest: {
      api: { image: "ghcr.io/a/api", digest: D("b"), compose: ["api"] },
      kernel: { image: "ghcr.io/a/kernel", digest: D("c"), compose: ["kernel-api", "kernel-gate"] } } },
    previous: { commit: "a".repeat(40), manifest: {
      api: { image: "ghcr.io/a/api", digest: D("a"), compose: ["api"] },
      kernel: { image: "ghcr.io/a/kernel", digest: D("c"), compose: ["kernel-api", "kernel-gate"] } } },
    plan: { changed: ["api"], unchanged: ["kernel"], removed: [] },
    health: { url: "https://health.test/ok", timeout_s: 5 },
    auto_rollback: rollback,
  };
}

function fakeDocker({ failUp = 0 } = {}) {
  const calls = [];
  let ups = 0;
  const run = async (command, args, opts) => {
    calls.push({ command, args, env: opts.env });
    if (args.includes("up")) {
      ups += 1;
      if (ups <= failUp) return { code: 1, output: "container exited" };
    }
    return { code: 0, output: "ok" };
  };
  return { run, calls };
}

test("a deploy pulls and restarts only the changed services, then reports deployed", async () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-deploy-"));
  const { fetchImpl, reports } = board(detailFor());
  const docker = fakeDocker();
  const result = await runDeploy({ host: "https://board.test", key: "k",
    deploy: { dir, file: "docker-compose.unified.yml", before: "./scripts/migrate_all.sh" } }, ID,
  { fetchImpl, run: docker.run, sleep: async () => {} });
  assert.equal(result.status, "deployed");
  assert.deepEqual(docker.calls.map((call) => [call.command, call.args.slice(-4)]), [
    ["./scripts/migrate_all.sh", []],
    ["docker", ["--env-file", join(dir, ".veynod", "deploy.env"), "pull", "api"]],
    ["docker", ["up", "-d", "--no-deps", "api"]],
  ]);
  assert.equal(docker.calls[0].env.VEYNOD_DEPLOY_ENV, join(dir, ".veynod", "deploy.env"));
  const env = readFileSync(join(dir, ".veynod", "deploy.env"), "utf8");
  assert.match(env, new RegExp(`VEYNOD_IMAGE_API=ghcr.io/a/api@${D("b")}`));
  assert.match(env, new RegExp(`VEYNOD_IMAGE_KERNEL=ghcr.io/a/kernel@${D("c")}`));
  assert.deepEqual(reports.map((item) => item.status), ["running", "deployed"]);
  assert.deepEqual(reports[1].services, { api: "restarted" });
  assert.equal(reports[1].health, "ok");
});

test("an unhealthy deploy rolls back to the previous images and says so", async () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-deploy-"));
  // Unhealthy until the rollback is up.
  let rolledBack = false;
  const docker = fakeDocker();
  const original = docker.run;
  docker.run = async (command, args, opts) => {
    if (args.includes("up") && readFileSync(join(dir, ".veynod", "deploy.env"), "utf8").includes(D("a"))) {
      rolledBack = true;
    }
    return original(command, args, opts);
  };
  const { fetchImpl: base, reports } = board(detailFor());
  const fetchImpl = async (url, init) => (url === "https://health.test/ok"
    ? { ok: rolledBack, status: rolledBack ? 200 : 503, json: async () => ({}) } : base(url, init));
  const realNow = Date.now;
  let clock = realNow();
  Date.now = () => clock;
  try {
    const result = await runDeploy({ host: "https://board.test", key: "k",
      deploy: { dir, file: "c.yml" } }, ID,
    { fetchImpl, run: docker.run, sleep: async () => { clock += 3000; } });
    assert.equal(result.status, "rolled_back");
  } finally {
    Date.now = realNow;
  }
  assert.deepEqual(reports.map((item) => item.status), ["running", "rolled_back"]);
  assert.match(reports[1].note, /health check failed/);
  assert.match(readFileSync(join(dir, ".veynod", "deploy.env"), "utf8"), new RegExp(`api@${D("a")}`));
});

test("a failed restart without rollback reports failed; no deploy directory reports failed", async () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-deploy-"));
  const one = board(detailFor({ rollback: false }));
  const result = await runDeploy({ host: "https://board.test", key: "k", deploy: { dir } }, ID,
    { fetchImpl: one.fetchImpl, run: fakeDocker({ failUp: 1 }).run, sleep: async () => {} });
  assert.equal(result.status, "failed");
  assert.deepEqual(one.reports.map((item) => item.status), ["running", "failed"]);
  assert.equal(one.reports[1].note, "docker compose up failed");

  const two = board(detailFor());
  const none = await runDeploy({ host: "https://board.test", key: "k" }, ID,
    { fetchImpl: two.fetchImpl, run: fakeDocker().run, sleep: async () => {} });
  assert.equal(none.status, "failed");
  assert.deepEqual(two.reports.map((item) => item.status), ["failed"]);
});

// TASK-BB795CF9: the console's Settings → Deploys shows this command; it must
// parse with this CLI. The dashboard is not in the public CLI repository.
const PANEL = new URL("../../dashboard/app/components/deploys-panel.tsx", import.meta.url);
test("the deploy computer command the console shows parses", { skip: !existsSync(PANEL) }, () => {
  const source = readFileSync(PANEL, "utf8");
  const dir = source.match(/const DEPLOY_DIR = "([^"]+)"/)[1];
  const command = source.match(/DEPLOY_COMMAND =\s*`([^`]+)`/)[1].replace("${DEPLOY_DIR}", dir);
  const [npx, pkg, ...argv] = command.split(" ");
  assert.equal(npx, "npx");
  assert.equal(pkg, "veynod@latest");
  const options = parseArgs(argv);
  assert.equal(options.command, "up");
  assert.equal(options.deploy, true);
  assert.equal(options.composeDir, dir);
  assert.equal(options.composeFile, "docker-compose.yml");
  const minimum = source.match(/const DEPLOY_CLI = "([^"]+)"/)[1].split(".").map(Number);
  const [major, minor, patch] = VERSION.split(".").map(Number);
  assert.ok(major > minimum[0] || (major === minimum[0] && (minor > minimum[1]
    || (minor === minimum[1] && patch >= minimum[2]))), `this CLI (${VERSION}) is older than the panel says`);
});
