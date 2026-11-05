# ADR-0001: Monorepo location and toolchain

- **Status:** accepted for the prototype phase; the owner can revisit the location at any time
- **Date:** 2026-11-05
- **Task:** `ser-p01-01` (PLAN-001 P01-01)

## Context

PLAN-001 §6 asks for a monorepo with `apps/`, `services/`, `packages/`, `infra/`, `tests/` and
`docs/`, with type checking, formatting, lint, tests and an effective CI before any feature work.
ARCH-001 §2 picks TypeScript throughout (React Native + Expo, Next.js, NestJS).

When the work started, the only GitHub repository this project's agents can reach is
`MuadibUsul/veynod`, the Veynod platform itself. Creating a repository is a person's decision.

## Decision

1. **Location.** The code lives in `serendipity/` inside `MuadibUsul/veynod`, fully self-contained:
   its own `package.json`, lockfile, configs and docs, and no imports from or into the Veynod code.
   Veynod's Docker images copy only their own directories and the deploy plan ignores paths no
   service claims (`.veynod/deploy.yml`), so a Serendipity change rebuilds no Veynod image.
2. **One CI.** The repository has one CI (`docs/README.md`, checked by
   `contracts/tests/test_repository.py`). Serendipity is the `serendipity` suite of
   `scripts/test_all.sh` and a row of the `node` matrix in `.github/workflows/ci.yml`; the GitHub
   check is `node (serendipity)`.
3. **Package manager.** npm workspaces (`apps/*`, `services/*`, `packages/*`), as the rest of the
   repository uses npm; no corepack or pnpm to install. `.npmrc` pins exact versions and enforces
   the Node engine.
4. **Toolchain** (exact pins, each release at least two weeks old when chosen):
   - Node 22 (`.nvmrc`), the version the repository's CI already uses.
   - TypeScript 6.0.3, `strict` plus `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes` and
     `verbatimModuleSyntax`. Not 7.x: typescript-eslint 8.72 supports TypeScript below 6.1.
   - ESLint 10 flat config with typescript-eslint `strictTypeChecked` + `stylisticTypeChecked` and
     `switch-exhaustiveness-check`.
   - Prettier 3.9 for formatting.
   - Vitest 4.1 for tests, with **Vite pinned to 7.3.7 through `overrides`**: npm 10.9 crashes
     (`Cannot read properties of null (reading 'edgesOut')`) while resolving the optional peer set
     `@vitest/mocker → msw → vite@^6‖^7‖^8`, which picks the newest Vite 8.x, whose optional
     `@vitejs/devtools-vitest` peer asks for `vitest@*`. Remove the override only together with a
     working `npm install` on the new versions.
   - One command, `npm run check`, runs format check, lint, typecheck and tests, locally and in CI.
5. **Shared packages are consumed as TypeScript source** (`exports` points at `src/index.ts`);
   Vitest and `tsc` read them directly. The first deployable service decides how it builds (NestJS
   needs decorator metadata, so that service will carry its own compiler settings).

## Consequences

- Serendipity PRs run the whole repository CI, including Veynod's Python suites. That is a few
  minutes of CI per PR, and a red Veynod suite can block a Serendipity merge (and the reverse).
  If that coupling hurts, it is the signal to split out (below).
- Veynod's release workflow runs after CI on `main` for every merge. For a Serendipity-only merge
  it builds no image (every image is retagged with an unchanged digest), but it still posts a
  release for that commit, exactly as a docs-only merge does today.
- History stays with the code: a later split keeps every commit.

## Moving to a dedicated repository

When the owner creates a repository for Serendipity:

```bash
git subtree split --prefix=serendipity -b serendipity-split
git push <new-remote> serendipity-split:main
```

Then copy the `serendipity` CI steps into the new repository's workflow, remove the suite from
`scripts/test_all.sh` and the `node` matrix here, and delete `serendipity/` from this repository.

## Open decisions for the owner

- A dedicated repository, and when (see above).
- The license of the Serendipity code. The repository is Apache-2.0 for Veynod's listed parts;
  `serendipity/` declares no license until the owner decides.
- GitHub handles for the code-ownership paths in `CONTRIBUTING.md`, and branch protection that
  requires the `node (serendipity)` check and code-owner review.
