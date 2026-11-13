# A lighter Loop: one worker role, three task states, trust by default

Status: **confirmed by the owner (2026-11-12), all five decisions as proposed.**
Steps 1, 2 and 4 are implemented (schema 3.102.0); step 3 is not yet.
Background: `LOOP_UNATTENDED_AUDIT.md`.

## The model

1. **One worker role.** Any computer or agent that works takes any task. The
   only rule kept between workers: whoever built something does not review it.
2. **Three states a worker sees.** `READY`, `IN_PROGRESS`, `DONE` (plus
   `CANCELLED`). Everything else becomes a field on the task, not a state.
3. **Trust by default.** A hand-off ends the task. A person or a second agent
   looks only when something asks for it.

## What changes, concretely

### Roles

- `assigned_role` no longer filters who may claim or be placed. `EXECUTOR`,
  `ANALYST` and `SCOUT` become one worker role; existing tokens keep working.
- `AUDITOR` stays as "may approve other people's work", used only by the
  attention queue below. `SUPER_ADMIN` and `COMMANDER` are unchanged.
- `required_role` (a hard requirement the author set on purpose) is kept.

### States

| Today | Becomes |
| --- | --- |
| `TODO` (dependencies open) | `READY` with `blocked_by` listing them; not placed until empty |
| `HANDOFF`, agent passes work on | `READY`, with its continuation pack (already stored on the task) |
| `HANDOFF`, scheduler gave up | `READY` with `stopped_until` (retried after the failure window) |
| `HANDOFF`, approved, waits for deploy | `DONE` with `awaiting_deploy` (already a field) |
| `REVIEWING` | `DONE`, or `DONE` with `needs_attention` (see below) |
| `AWAITING_HUMAN`, `WAITING_HUMAN`, `ESCALATED_TO_HUMAN` | one flag `needs_owner` with the question; the task stays where it was |
| `FAILED` | `READY` with `stopped_until`, or `CANCELLED` |

The old values stay in the database enum and in the API for one release, mapped
on read, so existing clients do not break.

### Trust

A hand-off marks the task `DONE` at once and releases what depends on it,
unless one of these is true; then it is `DONE` with `needs_attention` and a
reason, listed for a person or an AUDITOR:

- the agent set `risk_flag`, or listed an acceptance item as not met;
- an acceptance item of kind `AUTO` failed (see next section);
- the task is one of the owner's topics: money, identity, legal, deleting data,
  an outside commitment (already defined in the automation rules);
- the task is sampled (a small share, so quality is measured, not assumed).

`needs_attention` does not hold anything back by default: dependents are
released. A reviewer who rejects uses the recall that already exists
(`recall_document`, taint): the work and what was built on it are marked, and
the task goes back to `READY`.

This is the quality-control model already in the code (tiers 0–2, gates,
sampling, the circuit breaker that closes a domain after bad releases) with the
default moved from tier 0 (a person releases everything) to tier 2 (release on
hand-off), and with the gates made optional for tasks that have no repository.

### Checks instead of reviews

Acceptance items already have a kind, `AUTO` or `MANUAL`; every item on the
board today is `MANUAL`, which is why every task waits for a person. An `AUTO`
item gets a `check` the platform can run on hand-off:

- `document_exists`: a named document was written;
- `command`: a command exits 0 on the computer that did the work (tests);
- `http`: a URL answers with a status;
- `gate`: the Kernel gate passed (code tasks, as today).

A `MANUAL` item is the author saying "a person must look", and puts the task in
the attention queue. New tasks default to no acceptance items, which means
trusted.

### What keeps its gate

Irreversible things are not tasks' hand-offs and stay as they are: a deploy
(the deploy gate and its verification), spending, deleting data, messages to
the outside. Those go to the owner together, as now.

## What it costs

- Wrong work is found later, after other work was built on it. The backstop is
  recall and the circuit breaker; both exist, neither has been exercised at
  this scale.
- The console's task views, the MCP tool descriptions, the agent guide and
  about 60 tests name the states that go away.
- Agents that were told "hand off to REVIEWING, never DONE" get a different
  contract; old clients keep working through the mapping.

## Order of work

1. Roles: drop the role filter in the scheduler and on claim (small).
2. Trust: hand-off ends the task under full automation; `needs_attention`
   queue; default tier (medium; this is what makes the Loop flow).
3. States: fields for stopped / blocked / needs-owner, the read mapping, the
   console (large; mostly renaming once 2 is in).
4. `AUTO` checks (medium; independent of 3).

## Decisions for the owner

1. **Is `needs_attention` advisory or blocking?** Proposed: advisory
   (dependents are released). Blocking is safer and slower.
2. **Does trust-by-default apply everywhere, or per namespace?** Proposed: per
   namespace, on by default, so a sensitive namespace can stay reviewed.
3. **Sampling rate for a look by a person or AUDITOR.** Proposed: 10%.
4. **May one computer check its own work with a second runtime?** Proposed:
   yes for the attention queue when no other computer exists.
5. **Remove the old states from the API after one release, or keep the
   mapping for good?** Proposed: keep the mapping.

## As implemented (steps 1–2)

- Settings: `WORKER_ROLES_UNIFIED` (on), `TRUST_BY_DEFAULT` (on),
  `TRUST_REVIEWED_NAMESPACES` (empty), `TRUST_SAMPLE_RATE` (0.10).
- Trust applies under full automation to work without a repository, outside
  diagrams, when quality control is not enforced. The agent still calls
  `agent_handoff_task(REVIEWING)`; the server finishes the task.
- `needs_attention` + `attention_reasons` on the task; `GET /v1/tasks?needs_attention=true`;
  the Loop page lists them under "Needs you" with a one-click "Looks fine";
  `POST /v1/tasks/{key}/attention` with `ok`, or `redo` and a note (opens a
  follow-up task; a finished task is not reopened).
- One judge of fit: when the scheduler placed a task on an agent, that agent's
  claim is not refused for capabilities it did not declare.
- Verified by `tests/test_unattended_board.py`: one computer, the real CLI hub,
  nobody touching the board; nine tasks of the kinds that stopped in production
  finish in under a minute and one supervisor round runs and is not repeated.

## As implemented (step 4)

An `AUTO` acceptance item takes a `check`:

```json
{"id": "plan", "text": "the plan is written", "kind": "AUTO",
 "check": {"type": "document_exists", "doc_key": "plans/q4", "contains": "Friday"}}
{"id": "ci", "text": "CI is green", "kind": "AUTO", "check": {"type": "gate", "gate": "CI"}}
{"id": "tests", "text": "the tests pass", "kind": "AUTO",
 "check": {"type": "command", "run": "pytest -q", "timeout_s": 300}}
```

- `document_exists` and `gate` are judged by the server when the agent hands
  off. `command` is run by the runner on the computer that did the work, in its
  work directory, after the agent's turn, and reported with
  `POST /v1/tasks/{key}/checks` (only that computer's agent or a person may).
- Results are on the task (`check_results`) and in `acceptance_state`
  (PASS / FAIL). A failed check flags the task (`check_failed: <id>: <why>`);
  under trust it is DONE all the same, and the flag holds nothing back.
- A task that came in through federation runs no command on this workspace's
  computers.
- Not built: an `http` check. The server fetching a URL a task names is a
  server-side request risk and needs an allowlist first.
- Verified in `tests/test_unattended_board.py` (a command check that passes on
  the computer, a document check that fails and flags its task).

## Code tasks (tasks with a repository)

They are not finished at their hand-off: the Kernel's verification gate and
merge coordinator judge them. Before this, nothing moved the board task on
after a merge, so on a one-computer board every code task waited in REVIEWING
for a person. Now each scheduling pass under full automation reads the Kernel's
evidence for code tasks in review: a merged change with a passed gate is
finished on trust (`TASK_MERGED_ON_TRUST`), its dependents released, flagged
when the brief flagged a risk or a check failed. A failed or escalated gate is
left to the runner's revision turn and to people.

Verified with stamped Kernel evidence (`tests/test_trust_by_default.py`); not
yet against a running Kernel.
