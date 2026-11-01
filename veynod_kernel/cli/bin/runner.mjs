#!/usr/bin/env node
/**
 * Veynod Kernel v4.0 - Sandboxed Worker Runner
 *
 * - pulls a disjoint-write-set task bundle from the kernel
 * - creates a detached Git worktree at the task's base commit
 * - mounts the 6 MCP syscalls into the coding agent through .mcp.json
 * - enforces a local write-set guard, runs local tests, submits the diff
 * - listens to the SSE cursor stream and preempts the agent on TASK_ABORT
 */

import { spawn, execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  rmSync,
  readdirSync,
  statSync,
} from 'node:fs';
import { hostname, tmpdir } from 'node:os';
import { delimiter, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

const { values } = parseArgs({
  options: {
    api: { type: 'string' },
    token: { type: 'string' },
    'runner-id': { type: 'string' },
    repo: { type: 'string' },
    branch: { type: 'string' },
    agent: { type: 'string' },
    'agent-args': { type: 'string' },
    'local-test-cmd': { type: 'string' },
    'write-test-cmd': { type: 'string' },
    workspace: { type: 'string' },
    once: { type: 'boolean', default: false },
    poll: { type: 'string' },
    'max-context-tokens': { type: 'string' },
    'supported-types': { type: 'string' },
    'submission-timeout': { type: 'string' },
  },
  allowPositionals: true,
});

const config = {
  apiBaseUrl: (values.api || process.env.VEYNOD_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, ''),
  agentToken: values.token || process.env.VEYNOD_AGENT_TOKEN || '',
  runnerId: values['runner-id'] || process.env.VEYNOD_RUNNER_ID || `${hostname()}-${process.pid}`,
  repoRoot: (() => {
    const raw = values.repo || process.env.VEYNOD_REPO_ROOT || '';
    return raw ? resolve(raw) : '';
  })(),
  branch: values.branch || process.env.VEYNOD_MAIN_BRANCH || 'main',
  agent: values.agent || process.env.VEYNOD_AGENT_CMD || 'claude',
  agentArgs: values['agent-args'] || process.env.VEYNOD_AGENT_ARGS || '',
  localTestCmd: values['local-test-cmd'] || process.env.VEYNOD_LOCAL_TEST_CMD || 'pytest -q --tb=short',
  writeTestCmd:
    values['write-test-cmd'] ||
    process.env.VEYNOD_WRITE_TEST_CMD ||
    'pytest --collect-only -q',
  workspace: values.workspace || process.env.VEYNOD_WORKSPACE_ID || '',
  once: Boolean(values.once),
  pollMs: Math.max(500, Number(values.poll || process.env.VEYNOD_POLL_SECONDS || 3) * 1000),
  maxContextTokens: Number(values['max-context-tokens'] || process.env.VEYNOD_MAX_CONTEXT_TOKENS || 6000),
  submissionTimeoutMs: Math.max(
    5000,
    Number(
      values['submission-timeout'] || process.env.VEYNOD_SUBMISSION_TIMEOUT_SECONDS || 900
    ) * 1000
  ),
  supportedTypes: (values['supported-types'] || process.env.VEYNOD_SUPPORTED_TYPES || '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean),
};

if (!config.agentToken) {
  console.error('Missing runner token. Pass --token or VEYNOD_AGENT_TOKEN.');
  process.exit(2);
}
if (!config.repoRoot || !statSafe(config.repoRoot)) {
  console.error('Missing or invalid --repo / VEYNOD_REPO_ROOT.');
  process.exit(2);
}

const sandboxRoot = mkdtempSync(
  join(tmpdir(), `veynod-runner-sandbox-${config.runnerId.replace(/[^A-Za-z0-9_-]/g, '_')}-`)
);
const sandboxDir = join(sandboxRoot, 'wt');
let sandboxReady = false;

function statSafe(path) {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

function log(...parts) {
  console.log(`[${new Date().toISOString()}] [runner:${config.runnerId}]`, ...parts);
}

function sleep(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}

function git(args, cwd, { allowFailure = false, timeout = 120000 } = {}) {
  try {
    const out = execFileSync('git', args, {
      cwd,
      encoding: 'utf8',
      timeout,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    return out;
  } catch (error) {
    if (allowFailure) return '';
    const stderr = error?.stderr?.toString?.() || '';
    throw new Error(`git ${args.join(' ')} failed: ${stderr || error.message}`);
  }
}

async function api(path, body, { method = 'POST', signal, headers = {} } = {}) {
  const response = await fetch(`${config.apiBaseUrl}${path}`, {
    method,
    signal,
    headers: {
      Authorization: `Bearer ${config.agentToken}`,
      'Content-Type': 'application/json',
      ...headers,
    },
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
    const error = new Error(`HTTP ${response.status}: ${text.slice(0, 2000)}`);
    error.status = response.status;
    error.body = parsed;
    error.retryAfter = Number(response.headers.get('retry-after') || 0) || 0;
    throw error;
  }
  return parsed;
}

function runShell(command, cwd, { timeout = 900000, signal } = {}) {
  return new Promise((resolvePromise) => {
    const child = spawn(command, {
      cwd,
      shell: true,
      env: {
        ...process.env,
        PYTHONPATH:
          cwd + (process.env.PYTHONPATH ? `${delimiter}${process.env.PYTHONPATH}` : ''),
      },
      signal,
    });
    let output = '';
    child.stdout?.on('data', (chunk) => {
      output += chunk.toString();
      process.stdout.write(chunk);
    });
    child.stderr?.on('data', (chunk) => {
      output += chunk.toString();
      process.stderr.write(chunk);
    });
    const timer = setTimeout(() => {
      output += `\n[veynod] command timed out after ${timeout}ms`;
      child.kill('SIGKILL');
    }, timeout);
    child.on('error', (error) => {
      clearTimeout(timer);
      resolvePromise({ ok: false, code: -1, output: `${output}\n${error.message}` });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolvePromise({ ok: code === 0, code: code ?? -1, output });
    });
  });
}

const activeTasks = new Map(); // task_id -> { abort, child, preempted, reason }
let stopping = false;
let streamController = null;
let lastEventId = 0;

// ---------------------------------------------------------------------------
// SSE preemption listener
// ---------------------------------------------------------------------------
function handleSSEBlock(block) {
  let eventType = 'message';
  const dataLines = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) eventType = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
  }
  if (eventType !== 'TASK_ABORT' || dataLines.length === 0) return;

  let payload;
  try {
    payload = JSON.parse(dataLines.join('\n'));
  } catch {
    return;
  }
  const records = payload.aborted_leases || [];
  for (const record of records) {
    const taskId = String(record.task_id);
    const entry = activeTasks.get(taskId);
    if (!entry) continue;
    entry.preempted = true;
    entry.reason = payload.reason || 'preempted';
    log(`PREEMPT task ${taskId}: ${entry.reason}`);
    try {
      entry.abort.abort();
      entry.child?.kill('SIGTERM');
      setTimeout(() => {
        try {
          entry.child?.kill('SIGKILL');
        } catch {
          /* already gone */
        }
      }, 5000).unref?.();
    } catch {
      /* best effort */
    }
  }
}

function startPreemptionListener() {
  streamController = new AbortController();
  (async () => {
    while (!stopping) {
      try {
        const query = new URLSearchParams();
        if (config.workspace) query.set('workspace_id', config.workspace);
        if (lastEventId) query.set('since', String(lastEventId));
        const suffix = query.toString() ? `?${query}` : '';
        const response = await fetch(`${config.apiBaseUrl}/v1/events/stream${suffix}`, {
          headers: {
            Authorization: `Bearer ${config.agentToken}`,
            ...(lastEventId ? { 'Last-Event-ID': String(lastEventId) } : {}),
          },
          signal: streamController.signal,
        });
        if (!response.ok || !response.body) {
          throw new Error(`SSE HTTP ${response.status}`);
        }
        log('SSE preemption stream connected.');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (!stopping) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let index;
          while ((index = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, index);
            buffer = buffer.slice(index + 2);
            handleSSEBlock(block);
          }
        }
      } catch (error) {
        if (stopping) break;
        log(`SSE stream error: ${error.message}; reconnecting in 3s`);
        await sleep(3000);
      }
    }
  })();
}

// ---------------------------------------------------------------------------
// Agent + workspace
// ---------------------------------------------------------------------------
function promptFor(bundle, taskFile, mcpConfig) {
  return `You are a Veynod Kernel v4.0 worker agent.

Task bundle (read this first, it is the source of truth):
${taskFile}

Repository worktree (your cwd): only modify paths listed in the bundle's write_set.

Hard rules:
1. Implement exactly the specification for this task. Keep every frozen contract
   signature and docstring unchanged. If the contract is defective, call the MCP
   tool escalate_contract_defect instead of changing the signature.
2. Do not run "git commit", "git push", or rewrite history. The kernel gate
   commits for you.
3. Do not call the MCP tool submit_patch. The runner verifies and submits your
   diff after you exit.
4. You may call query_symbol_signature to inspect upstream signatures. If the
   task is clearly larger than ~300 LOC, call request_task_split. If a missing
   component blocks you, call inject_dependency_task.
5. Run the local pre-flight for this task type before finishing: WRITE_TEST
   requires the new test to collect (it is expected to be red against the stub);
   IMPLEMENT_BODY/FIX_INTEGRATION require the associated tests to pass;
   DESIGN_CONTRACT/REVISE_CONTRACT are typechecked by the kernel.
6. Keep the change minimal and inside the write set. Unauthorized files are
   reverted and the submission is rejected.

MCP config (already mounted): ${mcpConfig}
Task id: ${bundle.task_id}
Write set: ${JSON.stringify(bundle.write_set)}
`;
}

function agentInvocation(prompt, mcpConfig) {
  const extra = config.agentArgs
    ? config.agentArgs.split(/\s+/).filter(Boolean)
    : [];
  const executable = config.agent;
  const base = executable.split(/[\\/]/).pop() || executable;

  if (base.startsWith('claude')) {
    return {
      command: executable,
      args: [
        '--print',
        '--mcp-config',
        mcpConfig,
        '--strict-mcp-config',
        ...extra,
        prompt,
      ],
    };
  }
  return { command: executable, args: [...extra, prompt] };
}

function runAgent(worktree, prompt, mcpConfig, signal, taskFile, taskId) {
  return new Promise((resolvePromise, rejectPromise) => {
    const { command, args } = agentInvocation(prompt, mcpConfig);
    const child = spawn(command, args, {
      cwd: worktree,
      signal,
      stdio: 'inherit',
      env: {
        ...process.env,
        VEYNOD_API_URL: config.apiBaseUrl,
        VEYNOD_AGENT_TOKEN: config.agentToken,
        VEYNOD_MCP_CONFIG: mcpConfig,
        VEYNOD_TASK_FILE: taskFile,
        VEYNOD_TASK_ID: taskId,
      },
    });
    child.on('error', (error) => rejectPromise(error));
    child.on('close', (code, signalName) =>
      resolvePromise({ code: code ?? -1, signal: signalName })
    );
  });
}

function removeCaches(root) {
  const cacheNames = new Set([
    '__pycache__',
    '.pytest_cache',
    '.mypy_cache',
    '.ruff_cache',
    '.pyright',
    '.tox',
  ]);
  const walk = (dir) => {
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const full = join(dir, entry.name);
      if (cacheNames.has(entry.name)) {
        rmSync(full, { recursive: true, force: true });
      } else if (entry.name !== '.git') {
        walk(full);
      }
    }
  };
  walk(root);
}

function shellQuotePath(path) {
  return `"${String(path).replace(/"/g, '""')}"`;
}

function appendTestFiles(command, files) {
  const first = (command.trim().split(/\s+/)[0] || '').split(/[\\/]/).pop().toLowerCase();
  if (!first.startsWith('pytest') || files.length === 0) return command;
  return `${command} ${files.map(shellQuotePath).join(' ')}`;
}

function localCheckCommand(bundle) {
  const filesOf = (views) =>
    [...new Set((views || []).map((view) => String(view.symbol_path).split('::')[0]))];
  const taskType = bundle.task_type;
  if (taskType === 'WRITE_TEST') {
    return appendTestFiles(config.writeTestCmd, filesOf(bundle.mutable_stubs));
  }
  if (['DESIGN_CONTRACT', 'REVISE_CONTRACT', 'DESIGN_ARCHITECTURE'].includes(taskType)) {
    return null;
  }
  return appendTestFiles(config.localTestCmd, filesOf(bundle.frozen_tests));
}

function allowedFileSet(writeSet) {
  return new Set(writeSet.map((lock) => lock.split('::')[0]));
}

function isAllowed(file, allowedFiles) {
  for (const allowed of allowedFiles) {
    if (file === allowed || file.startsWith(`${allowed}/`)) return true;
  }
  return false;
}

async function releaseLease(bundle, reason, retryable = true) {
  try {
    return await api('/v1/runner/release', {
      task_id: bundle.task_id,
      lease_token: bundle.lease_token,
      reason: reason.slice(0, 4000),
      retryable,
    });
  } catch (error) {
    log(`release failed: ${error.message}`);
    return null;
  }
}

function startHeartbeat(bundle) {
  const expires = Date.parse(bundle.lease_expires_at || '');
  const remaining = Number.isFinite(expires) ? expires - Date.now() : 300000;
  const every = Math.max(30000, Math.min(Math.floor(remaining / 3), 300000));
  const timer = setInterval(async () => {
    try {
      await api('/v1/runner/heartbeat', {
        task_id: bundle.task_id,
        lease_token: bundle.lease_token,
        extend_seconds: 300,
      });
    } catch (error) {
      log(`heartbeat failed: ${error.message}`);
      const entry = activeTasks.get(bundle.task_id);
      if (entry) {
        entry.preempted = true;
        entry.reason = 'lease lost during heartbeat';
        entry.abort.abort();
      }
    }
  }, every);
  timer.unref?.();
  return timer;
}

function ensureSandbox(baseCommit) {
  if (!sandboxReady) {
    git(['worktree', 'add', '--detach', sandboxDir, baseCommit], config.repoRoot);
    sandboxReady = true;
    return;
  }
  git(['reset', '--hard', 'HEAD'], sandboxDir, { allowFailure: true });
  git(['clean', '-fdx'], sandboxDir, { allowFailure: true });
  git(['checkout', '--detach', '--force', baseCommit], sandboxDir);
  git(['reset', '--hard', baseCommit], sandboxDir);
}

function cleanupSandbox() {
  if (!sandboxRoot) return;
  try {
    if (sandboxReady) {
      git(['worktree', 'remove', '--force', sandboxDir], config.repoRoot, {
        allowFailure: true,
      });
    }
    git(['worktree', 'prune'], config.repoRoot, { allowFailure: true });
  } catch {
    /* best effort */
  }
  rmSync(sandboxRoot, { recursive: true, force: true });
  sandboxReady = false;
}

function sha256Hex(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

async function acceptPatchSubmission(bundle, diff, idempotencyKey) {
  const body = {
    task_id: String(bundle.task_id),
    lease_token: bundle.lease_token,
    git_diff: diff,
    touched_symbols: bundle.write_set,
    local_test_passed: true,
  };
  let lastError = null;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    try {
      return await api('/v2/syscalls/submit_patch', body, {
        headers: { 'Idempotency-Key': idempotencyKey },
      });
    } catch (error) {
      lastError = error;
      if ([403, 409, 410, 422].includes(error.status)) throw error;
      // The response may have been lost after the server persisted the row.
      // Look it up by the same idempotency key before retrying the POST.
      try {
        const found = await api(
          `/v2/submissions/find?idempotency_key=${encodeURIComponent(idempotencyKey)}`,
          undefined,
          { method: 'GET' }
        );
        if (found?.submission_id) {
          log(`recovered lost submission response id=${found.submission_id}`);
          return found;
        }
      } catch (lookupError) {
        if (lookupError.status && lookupError.status !== 404) {
          log(`submission lookup failed: ${lookupError.message}`);
        }
      }
      await sleep(Math.min(5000, 500 * 2 ** attempt));
    }
  }
  throw lastError || new Error('submission accept failed');
}

async function waitForSubmission(accepted) {
  if (accepted.final) return accepted;
  const statusUrl = accepted.status_url || `/v2/submissions/${accepted.submission_id}`;
  const deadline = Date.now() + config.submissionTimeoutMs;
  let delay = 250;
  while (Date.now() < deadline) {
    try {
      const status = await api(statusUrl, undefined, { method: 'GET' });
      if (status.final) return status;
    } catch (error) {
      if (error.status !== 404) {
        log(`submission status poll failed: ${error.message}`);
      }
    }
    await sleep(delay);
    delay = Math.min(2000, Math.round(delay * 1.5));
  }
  throw new Error(
    `submission ${accepted.submission_id} did not reach a final state within ${config.submissionTimeoutMs}ms`
  );
}

async function runCycle(bundle) {
  const taskId = String(bundle.task_id);
  const tempRoot = mkdtempSync(join(tmpdir(), `veynod-task-${taskId}-`));
  const worktree = sandboxDir;
  const configDir = join(tempRoot, 'config');
  mkdirSync(configDir, { recursive: true });
  const taskFile = join(configDir, 'task-bundle.json');
  const mcpConfig = join(configDir, 'mcp.json');
  const abort = new AbortController();
  const entry = { abort, child: null, preempted: false, reason: '' };
  activeTasks.set(taskId, entry);

  let heartbeat = null;
  let submissionAttempted = false;
  try {
    writeFileSync(taskFile, JSON.stringify(bundle, null, 2));
    writeFileSync(
      mcpConfig,
      JSON.stringify(
        {
          mcpServers: {
            'veynod-kernel': {
              type: 'http',
              url: `${config.apiBaseUrl}/mcp`,
              headers: { Authorization: `Bearer ${config.agentToken}` },
            },
          },
        },
        null,
        2
      )
    );

    git(['fetch', '--all', '--prune'], config.repoRoot, { allowFailure: true });
    ensureSandbox(bundle.base_commit_sha);
    log(`leased ${taskId} (${bundle.task_type}); worktree ${worktree}`);

    heartbeat = startHeartbeat(bundle);
    const prompt = promptFor(bundle, taskFile, mcpConfig);
    const agentResult = await runAgent(
      worktree,
      prompt,
      mcpConfig,
      abort.signal,
      taskFile,
      taskId
    );
    log(`agent exited code=${agentResult.code} signal=${agentResult.signal || '-'}`);

    if (entry.preempted) {
      log(`task ${taskId} preempted (${entry.reason}); discarding worktree.`);
      return;
    }

    let status;
    try {
      status = await api(`/v1/runner/tasks/${taskId}`, undefined, { method: 'GET' });
    } catch (error) {
      log(`status check failed: ${error.message}`);
      return;
    }
    if (status.status !== 'LEASED') {
      log(`task ${taskId} is now ${status.status}; the agent/MCP already completed it.`);
      return;
    }

    removeCaches(worktree);

    const checkCommand = localCheckCommand(bundle);
    let testResult = { ok: true, code: 0, output: 'local test phase not required' };
    if (checkCommand) {
      log(`local pre-flight: ${checkCommand}`);
      testResult = await runShell(checkCommand, worktree, {
        timeout: 900000,
        signal: abort.signal,
      });
    }
    if (!testResult.ok) {
      log(`local pre-flight failed (exit ${testResult.code}); releasing for retry.`);
      await releaseLease(
        bundle,
        `Local pre-flight failed (exit ${testResult.code}):\n${testResult.output.slice(-4000)}`,
        true
      );
      return;
    }

    removeCaches(worktree);
    git(['add', '-A'], worktree, { allowFailure: true });
    const diff = git(['diff', '--cached', '--binary', 'HEAD'], worktree, {
      allowFailure: true,
    });
    if (!diff.trim()) {
      await releaseLease(bundle, 'Agent produced an empty patch.', true);
      return;
    }
    if (diff.length > 500000) {
      await releaseLease(
        bundle,
        `Patch is ${diff.length} bytes, above the 500000 syscall limit. Split the task.`,
        true
      );
      return;
    }

    // Local write-set guard: revert anything outside the leased files.
    const changed = git(['diff', '--cached', '--name-only', 'HEAD'], worktree, {
      allowFailure: true,
    })
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    const allowed = allowedFileSet(bundle.write_set || []);
    const violations = changed.filter((file) => !isAllowed(file, allowed));
    if (violations.length > 0) {
      for (const file of violations) {
        git(['reset', '--', file], worktree, { allowFailure: true });
        git(['checkout', '--', file], worktree, { allowFailure: true });
        rmSync(join(worktree, file), { force: true });
      }
      log(`local write-set guard removed unauthorized files: ${violations.join(', ')}`);
      await releaseLease(
        bundle,
        `Local write-set violation: ${violations.join(', ')}. Allowed files: ${[
          ...allowed,
        ].join(', ')}`,
        true
      );
      return;
    }

    const patchSha = sha256Hex(diff);
    const idempotencyKey = sha256Hex(
      `${taskId}
${bundle.lease_token}
${patchSha}`
    );
    submissionAttempted = true;
    const accepted = await acceptPatchSubmission(bundle, diff, idempotencyKey);
    log(
      `submission accepted id=${accepted.submission_id} status=${accepted.status} ` +
        `sha=${patchSha.slice(0, 12)}`
    );
    const final = await waitForSubmission(accepted);
    log(
      `submission final: ${final.status} task=${final.task_status || '-'} ` +
        `${final.result_commit_sha || ''}`
    );
    for (const gate of final.gate_results || []) {
      if (!gate.passed) {
        log(
          `gate FAILED ${gate.stage} exit=${gate.exit_code}: ${String(
            gate.diagnostics || ''
          ).slice(-1200)}`
        );
      }
    }
    if (final.status !== 'COMMITTED') {
      log(
        `submission failed code=${final.error_code || 'unknown'} ` +
          `${final.error_detail ? final.error_detail.slice(-1200) : ''}`
      );
    }
    if (final.task_status === 'LEASED') {
      // The async worker normally releases a gate-rejected task.  This is a
      // safe no-op if the task is already READY or held by a newer lease.
      await releaseLease(
        bundle,
        'Kernel gate rejected the patch; task released for a retry with diagnostics.',
        false
      );
    }
  } catch (error) {
    log(`cycle error: ${error.stack || error.message}`);
    const validationRejected = [403, 422].includes(error.status);
    // Once a submit request may have been persisted, the durable submission
    // worker owns the lease. Releasing it here would race the worker and can
    // turn a queued submission into lease_lost.
    if (!entry.preempted && (!submissionAttempted || validationRejected)) {
      await releaseLease(
        bundle,
        `Runner error: ${error.message}`,
        !validationRejected
      );
    }
  } finally {
    if (heartbeat) clearInterval(heartbeat);
    activeTasks.delete(taskId);
    rmSync(tempRoot, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------
async function pull() {
  return api('/v1/syscalls/pull_task_bundle', {
    worker_hub_id: config.runnerId,
    supported_task_types: config.supportedTypes.length
      ? config.supportedTypes
      : undefined,
    max_context_tokens: config.maxContextTokens,
  });
}

async function main() {
  log(`api=${config.apiBaseUrl} repo=${config.repoRoot} agent=${config.agent}`);
  startPreemptionListener();

  process.on('SIGINT', () => {
    stopping = true;
    streamController?.abort();
    for (const entry of activeTasks.values()) {
      entry.preempted = true;
      entry.reason = 'runner shutdown';
      entry.abort.abort();
    }
    log('SIGINT; shutting down.');
    setTimeout(() => process.exit(0), 1000);
  });

  while (!stopping) {
    let bundle;
    try {
      bundle = await pull();
    } catch (error) {
      log(`pull failed: ${error.message}`);
      if (config.once) process.exitCode = 1;
      const retryMs = error.retryAfter
        ? Math.max(config.pollMs, error.retryAfter * 1000)
        : config.pollMs;
      await sleep(retryMs);
      continue;
    }

    if (bundle?.admission_denied || bundle?.retry_after_seconds) {
      const retryMs = Math.max(
        config.pollMs,
        Number(bundle.retry_after_seconds || 0) * 1000
      );
      log(
        `admission paused (${JSON.stringify(bundle.queue_depth || {})}); ` +
          `retrying in ${Math.round(retryMs / 1000)}s`
      );
      if (config.once) break;
      await sleep(retryMs);
      continue;
    }

    if (bundle?.leased) {
      await runCycle(bundle);
      if (config.once) break;
      continue;
    }

    if (config.once) break;
    await sleep(config.pollMs);
  }

  stopping = true;
  streamController?.abort();
  cleanupSandbox();
  log('runner stopped.');
}

process.on('exit', () => {
  try {
    cleanupSandbox();
  } catch {
    /* best effort */
  }
});

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
