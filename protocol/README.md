# @veynod/protocol

Ingress schemas for the Veynod v0.2 role rules
(`doc://veynod-role-rules` §5.1). One package for the API, MCP server and the
console: a payload that cannot be executed is rejected before it reaches the
database.

```bash
npm ci --prefix protocol
npm test --prefix protocol      # tsc build + node --test
```

## Exports

| Export | Purpose |
| --- | --- |
| `TriState(schema)` | `VALUE` / `PENDING` (target + reason) / `NA` (reason). A missing or `null` value is not a legal fourth state. |
| `ProjectAttributionSchema` | `project`, `repo`, `local_path`, `deploy_target` — each tri-state. |
| `ContextDocRefSchema` | `doc://<doc_key>` with a pinned positive `version`; a namespace prefix is invalid. |
| `AcceptanceItemSchema` | `acc-*` id, `AUTO`/`MANUAL`, a checkable statement; `AUTO` needs an `auto_runner_id`. |
| `CreateTaskPayloadSchema` | The create-task payload, including `requires_code_delivery`. |
| `HandoffBriefingSchema` | The EXECUTOR handoff: fencing token, output location, optional `code_delivery`, summary, metrics, unverified hypotheses, risk flag. |
| `ReviewSubmissionSchema` | Per-`acceptance_id` verdicts; a `FAIL` carries the rework target, standard and reverify owner. |
| `parseCreateTaskPayload` / `parseHandoffBriefing` / `parseReviewSubmission` | Parse or throw `ProtocolValidationError` with `status = 422`. |

## Storage invariants (schema 3.41)

The spec is written against a greenfield `tasks` table. The platform's table is
`collab_tasks`, so the migration maps:

| Spec | Platform |
| --- | --- |
| `tasks` | `collab_tasks` |
| `is_locked` | `acceptance_locked` |
| `fencing_token` | `lease_epoch` |
| `acceptance`, `context_docs` | same columns |

`sql/migrations/v3_41_task_invariants.sql` adds:

- `chk_acceptance_is_array` — acceptance is always a JSON array.
- `chk_in_progress_requires_lease` — `IN_PROGRESS` carries a lease, a fencing
  generation and a holder.
- `chk_reviewing_requires_briefing` — `REVIEWING` carries the handoff briefing.
- `trg_enforce_task_immutability` — the first claim locks acceptance and pinned
  context documents; `DONE`/`FAILED` tasks never change status, acceptance or
  description. Violations raise `ERR_ACCEPTANCE_LOCKED`,
  `ERR_CONTEXT_DOCS_LOCKED` or `ERR_TERMINAL_STATE_IMMUTABLE`.

The creator/human acceptance edit implemented in `app/services/quality.py` is
preserved: that transaction sets the transaction-local GUC
`veynod.acceptance_change = 'authorized'` after its own authorization and audit
record. Any other writer is rejected by the trigger.

## Optional project and repo columns (schema 3.42)

REST `POST /v1/tasks`, MCP `create_task`, and handoff `create_next` accept
optional nullable `project` and `repo` strings and store them on
`collab_tasks` (`sql/migrations/v3_42_task_project_repo.sql`). Omitting them
leaves NULL, so claim, handoff and review keep working for older tasks. A
blank string is rejected.

These columns are not a substitute for `ProjectAttributionSchema`. The v0.2
create payload still requires tri-state `attribution.project` and
`attribution.repo` (`VALUE` / `PENDING` / `NA`). A null column only means the
caller did not send the optional routing labels.

## Scope notes

- `task_key` kebab-case is enforced by this ingress layer, not by a database
  constraint: legacy rows contain slashes and upper case, and the current REST
  and MCP create paths still accept them.
- The package is private to the repository; nothing is published to npm.
