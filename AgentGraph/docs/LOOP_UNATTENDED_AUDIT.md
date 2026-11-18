# Why the Loop does not run unattended: audit of 2026-11-11

Scope: the path from "a task is READY" to "it is DONE without a person", read
from the code (`scheduler.py`, `nerve_hub.py`, `tasks.py`, the CLI) and from the
live board on 2026-11-11 (doctor, hosts, placements, task list). Each finding
says what the evidence is and whether it is fixed on branch
`claude/charming-carson-kwew9r` (not merged, not deployed).

## The short answer

The Loop page says "automation is full, a supervisor looks after the Loop" and
the timeline shows a round every ten minutes. In fact **no supervise round has
run for at least 15 hours, and no task has been worked by a computer today**.
Three things stack up:

1. two defects make every dispatch after a computer's first one fail (A1, A2);
2. where a task stops, nothing ever picks it up again: stopping points are
   dead ends, not queues (B1–B4);
3. the board has one working computer, with one slot, and nobody who may
   review, analyse or take the specialised work (C1–C5).

Fixing 1 makes rounds and tasks run again. Unattended operation needs 2 and 3
as well: today a correct Loop would still drain into "waits for you" within a
few tasks.

## A. Defects that stop work today

### A1. Every supervise round is cancelled one second after it starts

Evidence: the last 90 supervise placements (01:31 to 16:34 UTC) all ended
`moved`, reason `nerve:AGENT_WORKER_BUSY`, 0.4–1.3 s after they were created,
on a computer that reports `running: []` and one free slot.

Cause: `poke_deliveries` is unique on `(workspace, event_seq, recipient,
channel)` and every nerve row was written with `event_seq = 0`. Only a
computer's first poke ever left a row. For every later placement the dispatch
loop (it runs each second and skips placements that have a row) found no row
and sent the same placement again. The second copy met the slot the first copy
had just taken, was refused as busy, and the refusal moved the placement. The
worker started by the first copy then read "placement is moved" and exited 0
without doing anything. This is the `skipped: placement is moved` in the hub
log.

Effect: the supervisor has never completed a round since this computer's first
poke. Everything that relies on it (moving stuck work, splitting failing tasks,
acknowledging alerts) has not happened.

Status: **fixed on the branch** (`ad37658`), with a test that runs ten rounds
through the real messenger. Not deployed.

### A2. A task that came back is never dispatched again

Evidence: `ser-p03-01a` on 2026-11-11: `lease_lost` at 02:01, then
`not_claimed` at 02:04, then `every_host_failed` until a person took it.

Cause: `dispatch_state` stayed `ACTIVE_LEASE` after the lease ended, so every
later dispatch was refused as "already dispatching"; the offer then timed out as
`not_claimed`, which counts against the computer.

Status: **fixed on the branch** (`79dec29`, migration `v3_99` also resets the
rows already stuck). Not deployed.

### A3. The timeline and the doctor report success that did not happen

`LOOP_ROUND` is written when a round is offered, and the console labels it
"made a supervisor round". `loop doctor` answers `ok: true` and "A supervisor
computer looks after the Loop" because a supervisor is *online*, not because a
round ever finished.

Status: round outcomes, the three delivery times and `GET /v1/runner/metrics`
are **on the branch**. The doctor is **not fixed**: it still judges by presence.
It should fail when the last N rounds did not end `finished`, or when no task
left READY in the last hours while tasks wait.

## B. Dead ends: where work stops and nothing restarts it

### B1. HANDOFF is a terminal state for the scheduler

After three failed rounds the scheduler moves a task to HANDOFF "and nobody is
pinged" (`run_pass`, `MAX_ROUNDS`). The scheduler only ever places READY tasks.
The only thing meant to look at stopped tasks is the supervisor (agenda item
`stopped`), and it has no tool that puts a task back: `move` and `withdraw` act
on open placements, and a HANDOFF task has none.

Evidence: 20 tasks are in HANDOFF, some since 2026-11-02.

Needed: a supported way out. Either the supervisor may reopen a stopped task
(with a reason, counted), or stopped tasks are retried on a slow schedule after
the cause changed (new computer, new CLI version, new server release).

### B2. Failures are counted forever

`_work_failures` counts every failed work placement a task ever had. Two on one
computer exclude that computer for good; three in total stop the task. Nothing
resets the count: not a new CLI version, not a server release that fixed the
cause, not a successful claim in between.

Consequence for the rollout: **deploying the fixes for A1 and A2 will not bring
the stopped tasks back**, and a READY task with old failures is stopped again at
the first pass. The counts must be scoped (since the task was last reopened, or
since the computer's `runner_version` or the server release changed).

### B3. A task without a matching computer waits silently

The scheduler writes an `unplaced` placement with the reason and repeats it each
pass. Under full automation there is no next step. Live examples:

| Task | Reason | Why it never resolves |
| --- | --- | --- |
| `kernel-e2e-3` | `no_host_for_repo` | the task says `smoke`, the computer says `smoke/smoke` |
| `ser-p01-06` | `no_host_with_capabilities` | needs `frontend`; no computer declares capabilities |
| 4 READY tasks (`super-admin-dual-loop-arch`, `ser-p00-01/02/06`) | `no_host_for_role` | role ANALYST; every computer's token is EXECUTOR |

Needed: under full automation an EXECUTOR computer should be allowed to take
ANALYST work (it is research and writing; the role boundary that matters is
that nobody reviews their own work), capabilities should be inferred from the
runtime instead of required to be declared, and repository names should be
matched on the last path segment or normalised when the task is created.

### B4. Review always ends at a person

Reviews go to another computer that did not build the work. With one computer
there is none, so each finished task gets "No other computer can review it, so
it waits for your review" (five times today). Code tasks have the Kernel gate;
most tasks on the board have no repository, so the gate never applies.

Needed for unattended work without a repository: a review that does not need a
second machine. Options, cheapest first: a second identity on the same computer
that runs a different runtime or model as AUDITOR (the builder's session and
the reviewer's session share nothing but the task); or an acceptance check the
task author declares as AUTO and the platform runs. Today every acceptance item
on the board is MANUAL.

## C. Capacity: what the board actually has

### C1. One computer, one slot, one subscription

Three computers are registered; two have been offline since 2026-11-06/07 and
nothing brings them back or tells the owner. The remaining one runs Codex on a
Plus plan (weekly window 32% used). There is no second runtime to fall back to
when the quota runs out: the scheduler then answers `hosts_out_of_quota` and
waits.

### C2. The supervisor shares the only work slot

A supervise round and a task compete for one slot. Fixed on the branch
(supervise has a lane of its own from CLI 0.9.0). Not released.

### C3. Most "agents" are not workers

Of 11 agents in the directory, 9 are interactive sessions ("online only while
you have the conversation open"). The scheduler places work on runner hosts
only. Tasks that are left for "an agent to claim" are claimed only when a person
opens a session and says so, which is what happened with the four tasks of this
audit's own session.

### C4. An interactive session cannot hold a task

A claim lease lasts at most one hour and needs a heartbeat every 20 minutes. A
cloud session that is paused (usage limit, restart) loses the lease; the task
goes back, its failure count goes up, and after a few of these the scheduler
stops it (B1). This session lost its lease four times in one day for that
reason.

### C5. The Loop cannot ship its own fixes

The fixes for A1 and A2 sit on a branch. Merging, deploying the server and
publishing the CLI to npm are all done by a person. Until then the production
Loop keeps the defects, including the one that stops the supervisor.

## D. What "unattended" needs, in order

1. **Ship what is on the branch** (server deploy with `v3_99`/`v3_100`, CLI
   0.9.0): rounds run, returned tasks are placed again, failures are reported
   at once, outcomes are visible. Without this nothing below can be observed.
2. **Scope the failure counts and give HANDOFF a way out** (B1, B2). Otherwise
   the backlog stays stopped after the deploy.
3. **Make the doctor judge by results** (A3): red when rounds do not finish or
   when READY tasks do not move.
4. **Let the one computer do more kinds of work** (B3): ANALYST tasks on an
   EXECUTOR computer under full automation, inferred capabilities, repository
   name matching.
5. **A review path that needs no second machine** (B4).
6. **A second computer or a second runtime**, and an alert to the owner when a
   computer has been offline for a day (C1).

Items 2–4 are code changes in `scheduler.py` of moderate size. Item 5 is a
product decision first: who may approve work no person looks at.

## E. What a person still decides, by design

Money, identity, legal matters, deleting data and outside commitments go to the
owner (constitution, automation rules). Several waiting tasks are of this kind
by their own title (`ser-p00-02` "人决定", `ser-p00-05` "人签约"). An unattended
Loop should finish everything around them and present those decisions together,
not wait on each one.

## F. Changed after this audit (same branch)

| Finding | Change |
| --- | --- |
| B1 HANDOFF is a dead end | A task the scheduler stopped returns to READY once fewer than three of its failed rounds fall inside the failure window (`TASK_SCHEDULER_RESUMED`). A task an agent or a person handed off is not touched. |
| B2 failures counted forever | Only failures of the last `RUNNER_FAILURE_WINDOW_HOURS` (24) count, against the task and against the computer. A stopped task is therefore tried again about once a day. |
| A3 doctor judges by presence | New required check `rounds` (red when none of the last five ended rounds finished) and check `progress` (READY tasks waiting over six hours, stopped tasks). The supervisor line now says "online", not "looks after the Loop". |
| B3 role | Under full automation an EXECUTOR computer may be given an ANALYST task (`RUNNER_EXECUTOR_TAKES_ANALYST`, on). `required_role` stays a hard requirement. Only the computer it was placed on may claim it. |
| B3 capabilities | A computer with Claude Code or Codex ready has the general capabilities (frontend, backend, docs, research, ...) without declaring them. |
| B3 repository names | `smoke` matches `smoke/smoke`; case, `.git` and a GitHub prefix are ignored. |

Still open: B4 (a review path without a second machine), C1 (one computer, one
subscription), C5 (the Loop cannot ship its own fixes).

## G. Verified unattended (2026-11-12)

`tests/test_unattended_board.py` runs the real server and the real CLI hub on
one computer with a stand-in agent that only claims and hands off. Nobody calls
the API after the board is set up. Result: all nine tasks DONE in 53 s (plain,
ANALYST, a `frontend` task, a two-task chain, MANUAL acceptance, a risky one,
an agent that crashes once, a task stopped three times days ago); flags on
exactly the MANUAL, the risky and one sampled task; one supervisor round
started and finished, and no second round on the unchanged agenda.

The run found three more defects, fixed in the same commit:

- the scheduler inferred capabilities for Claude Code and Codex only, so a
  computer on a custom runtime never got a task naming a capability;
- the claim check judged capabilities by a narrower rule than the scheduler, so
  the agent a task was placed on was refused, failed twice and was stopped;
- an unchanged agenda started a supervisor round every interval, one agent
  session each (about 144 a day at the default), for nothing new.

Still open: B4 for tasks with a repository on a one-computer board, C1, C5,
and steps 3–4 of `LOOP_SIMPLE_MODEL.md`. Tasks with a repository were not part
of the unattended run (no Kernel here).

## H. Each workspace is scheduled on its own (2026-11-18)

The scheduler used to be one sweep for the whole platform: every 15 s, one
server process (a platform-wide advisory lock) ran a pass over every workspace
with a computer, one after another. A pass is about 60 ms, so the sweep grew
with the number of users: 12 s at 200 workspaces, about 60 s at 1,000. A task
created by anyone waited for everyone else's workspaces, up to the sweep plus
the interval (27 s at 200, about 75 s at 1,000). One user's latency depended on
how many other users the platform had.

Now:

- **A change schedules its own workspace at once** (`scheduler.kick`): a task
  created, released, handed off or finished (the committed `collab_tasks`
  change on the workspace event bus), a computer saying hello, a seat coming
  free (the nerve ping), a placement reported, a worker exiting. Kicks are
  merged per workspace (0.2 s, at most one pass per 0.3 s per workspace) and
  run in slots of their own, never queued behind the sweep. Measured with 456
  other workspaces on the board: a new task is offered 0.29–0.32 s after it is
  created.
- **What only time brings about** (an offer not taken, a lease running out, a
  computer gone quiet, a stopped task whose failures aged, a supervise round)
  is written per workspace as `runner_hosts.next_pass_at` at the end of each
  pass: one reaper interval when something is on a computer, four when a task
  found no computer, otherwise the idle recheck (10 min, never later than the
  next supervise round).
- **The sweep takes only workspaces that are due**, by moving their time a
  minute on in one statement, so any number of server processes share it with
  no platform-wide lock, and a pass that dies is retried a minute later. Due
  passes run side by side (`scheduler_concurrency`, 4). Finding nothing due is
  one indexed query (8 ms with 456 workspaces), so the sweep looks every 2 s.
  All of them due at once (a restart) takes about 15 s for 456 workspaces and
  delays nobody's new task.

`tests/test_scheduler_kicks.py` checks the kick path with the sweep out of the
way; `tests/test_phase0_server.py` checks that two processes never take the
same workspace.

## I. Implicit state machines (2026-11-18)

Every status column was a state machine only by convention. `collab_tasks.status`
alone was written from about thirty places in eight files, by the ORM and by raw
SQL in background loops; the database refused only three things (leaving a
finished task, `IN_PROGRESS` without a lease, `REVIEWING` without a brief). The
same held for 52 other columns. What was found, and what changed:

- **One declaration for all 53 machines**: `app/state_machine_catalog.py` (states,
  moves, the reason for each move); the federation market's own table is
  derived, not copied. `docs/STATE_MACHINES.md` is generated from it, and
  `GET /v1/state-machines[/{name}]` returns it to people and agents.
- **The database holds every writer**: one trigger function
  (`agentgraph_guard_transition`) on each column checks the move against
  `state_transitions`, copied from the catalog at every start. It holds the
  application's own connections (`application_name = veynod`). A person or tool
  on the database directly (an operator repairing a row) is never blocked, only
  recorded; an undo (`veynod.state_override = 'undo'`) may restore any state.
- **Rollout without surprises**: `STATE_GUARD_MODE` is `enforce` in tests and
  development and `record` in production until set. Recorded moves are at
  `GET /v1/admin/state-machines/violations`, marked by whether the application
  made them; set `enforce` once that list holds nothing from the application.
- **How the moves were found**: a recording trigger on every status column through
  the whole test suite, then the suite again in record mode against the draft.
  It found two moves the code makes that a reading of the code missed (an
  escalation or peer review finished straight from `READY`; the scheduler stopping
  a `READY` task). The record mode itself first upserted one counter row per move,
  which made two writers of the same move wait on each other: it is append-only.
- **Errors that name the way out**: a trigger's refusal used to reach the generic
  database handler and come back as 503 `database_unavailable`, which tells a
  caller to retry a request that can never succeed. Refusals are now a 409
  `invalid_state_transition` with `details.machine`, `current`, `requested` and
  `allowed`; the locked-acceptance and finished-task refusals get their own codes.
- **Nothing left implicit by accident**: `tests/test_state_machines.py` fails when
  a status-like column is neither declared nor listed in `NOT_MACHINES` with the
  reason it is not a lifecycle (a setting, a once-written log, a mirror), and when
  the declared states differ from the schema's own list.

Same pass, the other "one loop for everything" risk: the lease reaper ran about
twenty upkeep steps inside one `try`, so a step that raised skipped every step
after it, round after round, with nothing naming the step. Each step now runs on
its own and a failure is logged by name (`reaper.last_errors`).
