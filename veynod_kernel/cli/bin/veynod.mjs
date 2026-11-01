#!/usr/bin/env node
/**
 * Veynod Kernel v4.0 operator CLI.
 *
 *   veynod serve [--host 0.0.0.0] [--port 8000]
 *   veynod workspace --repo /path/to/bare.git [--name demo] [--branch main]
 *   veynod token [--label runner-01]
 *   veynod tasks --workspace <uuid> [--status READY]
 *   veynod dag [--workspace <uuid>]
 *   veynod runner [runner.mjs args...]
 */

import { spawn } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..', '..');
const [, , command, ...rest] = process.argv;

const apiBaseUrl = (
  process.env.VEYNOD_API_URL || 'http://127.0.0.1:8000'
).replace(/\/$/, '');
const adminKey = process.env.VEYNOD_ADMIN_KEY || '';

function usage() {
  console.log(`Veynod Kernel v4.0

Commands:
  serve       Start the FastAPI kernel (uvicorn app.main:app)
  workspace   Create a workspace (POST /v1/admin/workspaces)
  token       Issue a runner token (POST /v1/admin/tokens)
  tasks       List tasks for a workspace
  dag         Print the read-only DAG topology
  runner      Start the sandboxed runner daemon

Environment:
  VEYNOD_API_URL     default http://127.0.0.1:8000
  VEYNOD_ADMIN_KEY   X-Admin-Key for workspace/token/tasks
  VEYNOD_AGENT_TOKEN runner bearer token
  VEYNOD_REPO_ROOT   runner's bare/mirror Git repository

Examples:
  VEYNOD_ADMIN_KEY=change-me node cli/bin/veynod.mjs workspace --repo /srv/demo.git
  VEYNOD_ADMIN_KEY=change-me node cli/bin/veynod.mjs token --label runner-01
  VEYNOD_AGENT_TOKEN=vnk_... VEYNOD_REPO_ROOT=/srv/demo.git node cli/bin/runner.mjs
`);
}

function parseFlags(args) {
  return parseArgs({
    args,
    allowPositionals: true,
    options: {
      api: { type: 'string' },
      'admin-key': { type: 'string' },
      repo: { type: 'string' },
      name: { type: 'string' },
      branch: { type: 'string' },
      label: { type: 'string' },
      workspace: { type: 'string' },
      status: { type: 'string' },
      host: { type: 'string' },
      port: { type: 'string' },
      json: { type: 'boolean', default: false },
    },
  }).values;
}

function requireAdmin(flags) {
  const key = flags['admin-key'] || adminKey;
  if (!key) {
    console.error('Missing admin key. Pass --admin-key or set VEYNOD_ADMIN_KEY.');
    process.exit(2);
  }
  return key;
}

async function request(path, { method = 'GET', body, admin = false, key } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (admin) headers['X-Admin-Key'] = key;
  if (process.env.VEYNOD_AGENT_TOKEN) {
    headers.Authorization = `Bearer ${process.env.VEYNOD_AGENT_TOKEN}`;
  }
  const response = await fetch(`${(flags.api || apiBaseUrl).replace(/\/$/, '')}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { raw: text };
  }
  if (!response.ok) {
    console.error(`HTTP ${response.status}: ${text}`);
    process.exit(1);
  }
  return parsed;
}

function print(value, flags) {
  if (flags.json) {
    console.log(JSON.stringify(value, null, 2));
    return;
  }
  console.log(JSON.stringify(value, null, 2));
}

function runChild(commandName, args, env = {}) {
  const child = spawn(commandName, args, {
    cwd: repoRoot,
    stdio: 'inherit',
    env: { ...process.env, ...env },
  });
  child.on('exit', (code) => process.exit(code ?? 0));
}

if (command === 'runner') {
  runChild('node', [resolve(here, 'runner.mjs'), ...rest], process.env);
  // Keep this process alive until the runner's exit handler fires.
  await new Promise(() => {});
}

let flags = {};
try {
  flags = parseFlags(rest);
} catch (error) {
  console.error(error.message);
  process.exit(2);
}

switch (command) {
  case 'serve': {
    const host = flags.host || process.env.VEYNOD_HOST || '0.0.0.0';
    const port = flags.port || process.env.VEYNOD_PORT || '8000';
    runChild('python', [
      '-m',
      'uvicorn',
      'app.main:app',
      '--host',
      String(host),
      '--port',
      String(port),
    ]);
    break;
  }

  case 'workspace': {
    const key = requireAdmin(flags);
    if (!flags.repo) {
      console.error('Missing --repo (path to a bare/mirror Git repository).');
      process.exit(2);
    }
    request('/v1/admin/workspaces', {
      method: 'POST',
      admin: true,
      key,
      body: {
        name: flags.name || 'veynod',
        repo_path: flags.repo,
        main_branch: flags.branch || 'main',
      },
    }).then((value) => print(value, flags));
    break;
  }

  case 'token': {
    const key = requireAdmin(flags);
    request('/v1/admin/tokens', {
      method: 'POST',
      admin: true,
      key,
      body: { label: flags.label || 'runner' },
    }).then((value) => print(value, flags));
    break;
  }

  case 'tasks': {
    const key = requireAdmin(flags);
    if (!flags.workspace) {
      console.error('Missing --workspace <uuid>.');
      process.exit(2);
    }
    const query = new URLSearchParams({ workspace_id: flags.workspace });
    if (flags.status) query.set('status', flags.status);
    request(`/v1/admin/tasks?${query}`, { admin: true, key }).then((value) =>
      print(value, flags)
    );
    break;
  }

  case 'dag': {
    const query = flags.workspace
      ? `?workspace_id=${encodeURIComponent(flags.workspace)}`
      : '';
    request(`/v1/dag${query}`).then((value) => print(value, flags));
    break;
  }

  case undefined:
  case '-h':
  case '--help':
  case 'help':
    usage();
    break;

  default:
    console.error(`Unknown command: ${command}`);
    usage();
    process.exit(2);
}
