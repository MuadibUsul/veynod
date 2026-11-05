# 不期 Serendipity

An AI-native blind-date app. A voice conversation builds your profile, two scoped agents check the
match from both sides, and the two of you meet at a partner venue without photos or chat beforehand.
Each person pays their own share through a licensed payment provider, and you check in at the venue
before identities are revealed. Chat opens only if both of you want to continue.

This directory is the engineering monorepo. The product documents and the task board live in the
Veynod workspace, namespace `serendipity` (see [docs/](docs/README.md)).

## Status

Prototype phase, no money flow: gate G0 (PLAN-001 §4) has not passed, so nothing here touches real
payments, real photos or real users. The first deliverable is this repository skeleton
(`ser-p01-01`).

## Layout

```text
apps/       mobile, merchant-web, ops-web            → apps/README.md
services/   api, voice-gateway, worker               → services/README.md
packages/   domain, consent, schemas, ai-adapters, a2a-protocol, ui → packages/README.md
infra/      docker, migrations, monitoring           → infra/README.md
tests/      contracts, e2e, security, payment-chaos, model-evals → tests/README.md
docs/       ADRs, runbooks, compliance notes         → docs/README.md
```

## Develop

```bash
npm ci
npm run check   # format check + lint + typecheck + tests (what CI runs)
```

From the repository root, `scripts/test_all.sh serendipity` runs the same suite.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before the first change, and
[ADR-0001](docs/adr/0001-monorepo-location-and-toolchain.md) for why the code lives here and how it
moves to its own repository.
