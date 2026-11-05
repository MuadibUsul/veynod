# Contributing to Serendipity

These rules apply to every change under `serendipity/`, whether a person or an agent writes it.
They come from PLAN-001 §5–§8 and §12 (`doc://engineering-delivery-plan`).

## Setup and the one check

```bash
nvm use            # Node 22 (see .nvmrc); npm 10
npm ci
npm run check      # format check, lint, typecheck, tests — the same command CI runs
```

`npm run format` fixes formatting. Every script runs from `serendipity/`; nothing here needs the
rest of the Veynod repository.

## Where work comes from

- Every change belongs to one Veynod task in namespace `serendipity` (tree `serendipity-mvp`, task
  keys `ser-pNN-NN`). Claim the task before you start; one task, one holder.
- One task, one branch, one pull request. Fix a rejected task on the same task and branch; do not
  open a copy.
- Read the task's acceptance items before writing code; they are locked at the first claim, and
  review checks every one of them.

## Branches, commits and pull requests

- **Branch:** `ser/<task-key>-<short-slug>`, for example `ser/p04-01-date-state-machine`. An agent
  platform that assigns its own branch name may keep it; the PR title still carries the task key.
- **Commits:** imperative subject of at most 72 characters, a body that says why. Keep unrelated
  changes out.
- **PR title:** `[ser-pNN-NN] <what changes>`.
- **PR body** follows the PLAN-001 §7 task template:
  - _Goal_ — the single verifiable result.
  - _Inputs_ — the PRD / ARCH / PLAN sections and versions used.
  - _Acceptance_ — every acceptance item of the task, each with its evidence.
  - _Evidence_ — commands run and a summary of their output; screenshots for UI.
  - _Failure cases considered._
  - _Rollback_ — the feature flag, or the down-migration and how it was rehearsed.

## Merging

A pull request merges only when all of these hold; none of them is skipped to go faster:

1. The `node (serendipity)` CI job is green (`npm run check`).
2. At least one reviewer who did not write the change approved it.
3. Every acceptance item of the task has evidence in the PR.
4. Nobody merges automatically and nothing deploys from an unreviewed branch.

A task is `DONE` only when a person approves it in Veynod. An agent saying "done" is not evidence.

## Code ownership

These paths need review by the owner (or someone the owner names) in addition to the usual review:

| Path                                                                           | Why                                                          |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------ |
| `packages/domain/`                                                             | Date, payment and relationship state machines and invariants |
| `packages/consent/`                                                            | Who may read what, for which purpose                         |
| Payment modules (`services/api/src/payment/`, `services/worker/` payment jobs) | Money                                                        |
| `infra/migrations/`                                                            | Schema changes reach every environment                       |

Concurrent agents must not edit the same core file in parallel (PLAN-001 §5.1); split the task
instead. When the owner provides the GitHub handles, these paths go into the repository's
`.github/CODEOWNERS` and branch protection requires code-owner review.

## Never

- Commit secrets, keys or tokens, including in fixtures, logs or examples.
- Put real personal data in tests or fixtures. Factories generate fictional data only.
- Bypass age/identity verification, consent checks or payment confirmation, even for a demo.
- Let model output decide money, booking state or permissions; the domain layer re-checks
  everything a model proposes (ARCH-001 ADR-003, PLAN-001 §8.1).
- Let a private photo URL, raw voice, a transcript or a private answer reach another user or
  another user's agent.
- Write voice, photo or ID content, or secrets, into logs, traces, crash reports or analytics.
- Change Veynod's own code (`AgentGraph/`, `agent_runtime/`, `veynod_kernel/`, `contracts/`) from
  a Serendipity task.

## Code conventions

- TypeScript strict (see `tsconfig.base.json`); no `any`, no non-null assertions.
- Every `switch` over a union ends in `default: return assertNever(x)` (from
  `@serendipity/domain`); the linter also enforces exhaustiveness.
- Unit tests sit next to the code as `*.test.ts`; suites that span packages live in `tests/`.
- Dependencies are pinned to exact versions (`.npmrc` has `save-exact=true`); add one only with a
  reason in the PR.
- Code, identifiers and comments are in English; product copy and product documents may be Chinese.
- Times are stored in UTC; amounts are integers in minor units with a currency code.
