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
