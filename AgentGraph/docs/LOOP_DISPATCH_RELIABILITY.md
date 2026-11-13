# Loop dispatch: delivered, started, finished

Status: implemented (server schema 3.99.0, CLI 0.9.0). Not yet released or
deployed; the rollout checks at the end are still to be done on real computers.

A placement passes three points. Each has its own record and none stands in for
the next.

| Point | Means | Who records it | Where |
| --- | --- | --- | --- |
| `ack.net` | the computer read the poke | hub, on `task.ack.net` | `poke_deliveries.net_ack_at`, `net_rtt_ms` |
| `worker.ready` | a worker process is up and can take it | hub, on `task.ack.exec` | `exec_ack_at`, `exec_startup_ms`, `warm` |
| `execution.started` | the placement was checked and work on it began | board, on the worker's `start` call | `exec_started_at`, `exec_started_ms`, `runner_placements.started_at` |

Then the runner reports `finished` or `failed`. A supervise round is a success
only when its placement reports `finished`. `LOOP_ROUND` on the timeline only
says a round was offered; `LOOP_ROUND_FINISHED`, `LOOP_ROUND_FAILED`,
`LOOP_ROUND_MOVED` and `LOOP_ROUND_SKIPPED` say what became of it.

## What was wrong

1. **A task that came back could never be placed again.** A successful dispatch
   set `collab_tasks.dispatch_state` to `ACTIVE_LEASE` and nothing ever reset it.
   When the task returned to READY, every later dispatch was refused as
   `TASK_ALREADY_DISPATCHING`, the offer timed out (`not_claimed`), and after
   three rounds the task stopped in HANDOFF. Fixed by a trigger that keeps the
   state true to the lease, a reset of the rows already stuck, and a dispatcher
   that judges by what really holds the task.
2. **Only a computer's first poke was recorded.** `poke_deliveries` is unique on
   `(workspace, event_seq, recipient, channel)` and every nerve row used
   `event_seq = 0`. Later placements left no row, so the dispatch loop sent them
   again each second and their receipts could not be stamped.
3. **A failed run held its task for the rest of the lease** (15 minutes by
   default). A `failed` report on a task the computer took now releases it at
   the next pass.
4. **A worker that could not start was only found by a timeout.** The spawn's
   async `error` event was unhandled (it could take the hub down), and an early
   exit or the watchdog said nothing to the hub. All three now send
   `task.busy.reject` with the reason at once.
5. **"Ready" was sent before the placement was read**, so a worker could come up,
   find its placement moved, write a local `skipped` receipt and exit 0. It now
   asks the board to start first; a placement that moved on records
   `reported = skipped` with the reason.
6. **The worker ended with `process.exit()`** the moment its promise settled. It
   now sets the exit code, closes its channel to the messenger and lets Node
   finish.

## Protocol additions

- Poke payload: `orientation_timeout_ms` (the messenger's watchdog) and
  `exec_ack_timeout_ms` (the hub's wait). One setting moves both:
  `NERVE_EXEC_ACK_TIMEOUT_SECONDS` (default 5; the watchdog is 500 ms less).
- `sys.ping` payload: `supervise_slots`, `supervising`, `draining`, `standby`.
- `task.busy.reject` reasons: `WORKER_ALREADY_BUSY`, `HUB_DRAINING`,
  `WORKER_START_FAILED: …`, `WORKER_EXITED_BEFORE_READY: code=…`,
  `ORIENTATION_TIMEOUT: …`, `RUNNER_MISSING: …`.
- `task.worker.exit` `{placement_id, code, signal, reported}`: a worker ended. If
  the placement is open and has no report it is marked `failed` with the reason.
- `lease.revoked` carries `placement_id`, so a step without a task (a supervise
  round) can be named.
- `POST /v1/runner/placements/{id}/start` `{lease_epoch?}` →
  `{start: true, detail}` or `{start: false, skip_reason}`.
- `GET /v1/runner/metrics?hours=24` (MCP: `loop(action="metrics")`).

An older CLI keeps working against the new server (no lane, no `start` call: it
reads the placement as before). The new CLI against an older server falls back
to reading the placement when `start` answers 404.

## Capacity

- **Standby workers.** The hub keeps one prepared worker per work slot and one
  for the supervise lane (`VEYNOD_WARM_WORKERS=0` turns it off). A prepared
  worker has Node and the runner loaded and takes its placement over IPC. Each is
  replaced after ten minutes. Three failures in a row turn the pool off and
  placements start cold as before.
- **Supervise lane.** A supervise round runs beside the work slots, one at a
  time. It is not counted in a host's load, and a full work slot does not refuse
  it.

## Upgrades

An update is a switch between two processes: the new version is installed and
must start (`veynod.mjs --version`) before the launcher points at it; the old hub
drains (takes nothing new, lets its workers finish) and only then exits. Each
hub leaves a marker `~/.veynod/cli/.running/<pid>` with its version; a version a
live hub still runs is never pruned. A hub whose `runner.mjs` is gone refuses
with `RUNNER_MISSING` and exits so the service starts the installed version.

## Reading the numbers

`GET /v1/runner/metrics` returns, for the window:

- `timings.delivered_ms`, `worker_ready_ms`, `execution_started_ms`,
  `execution_started_warm_ms`: count, p50, p95, p99, max;
- `meets_targets`: delivered p95 ≤ 1 s, prepared-worker start p95 ≤ 2 s and
  p99 ≤ 5 s;
- `pokes.worker_start_failure_rate` and `not_delivered_reasons`;
- `supervise`: `finished`, `failed`, `skipped`, `not_delivered`,
  `finished_rate`, `finished_in_a_row`;
- `stopped`: every task the scheduler gave up on, with what each of its rounds
  failed on.

The time the model takes for the whole task is not in these numbers.

## Rollout checks still to do

These need real computers and cannot be done from the test suite.

1. **Windows, Node 22 against Node 24.15.0.** Run the same batch of at least 20
   supervise rounds on each, with CLI 0.9.0. Watch `hub.log` for
   `Assertion failed` and for `WORKER_EXITED_BEFORE_READY`, and
   `GET /v1/runner/metrics` for `not_delivered_reasons`. If the assertion still
   appears on Node 24 and not on 22, pin the hub's service to Node 22 until it
   does not. The removal of `process.exit()` is expected to help but this is
   unproven until that run.
2. **One computer, real placements.** A few real tasks and rounds, then read
   `supervise.finished_in_a_row` (target: at least 10) and `meets_targets`.
3. **24 hours.** `supervise.skipped` and `not_delivered` are reported on their
   own and never inside `finished`. Watch `worker_start_failure_rate`.
4. **Rollback.** Keep the previous CLI version directory and the placement rows.
   A rolled-back hub reads placements as before, and `start` refuses a stale
   lease epoch, so a retry cannot run a placement twice.

## Tasks stopped in HANDOFF

Do not reset them in bulk. `metrics.stopped` lists each one with the reasons of
its rounds. `not_claimed` on a task whose earlier round was `returned` or
`lease_lost` is the first defect above and can be put back to READY. Reasons
such as `no_host_for_role`, `no_host_with_capabilities`, `no_kernel_binding` or
`no_host_for_repo` need the role, capability, Kernel binding or repository name
fixed first.
