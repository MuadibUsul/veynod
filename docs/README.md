# Veynod Documentation Index

This directory holds the engineering, product and operating documents for
Veynod. The public product docs are served separately at `/docs/`; this folder
is the repository source of truth.

## Start here

| Document | What it answers |
| --- | --- |
| [README](../README.md) | What Veynod is, quick start, deployment, API/MCP overview |
| [PROJECT_ARCHITECTURE](PROJECT_ARCHITECTURE.md) | Architecture, directory tree, data model, request flows, permissions |
| [PRD_PROGRESS](PRD_PROGRESS.md) | Requirement coverage, implementation status, known gaps |
| [AGENT_GUIDE](AGENT_GUIDE.md) | Machine-readable first-call guide for agents |
| [ERROR_CODES](ERROR_CODES.md) | Error classes, retry policy and agent actions |

## Product requirements and specs

| Document | Status |
| --- | --- |
| [v3-requirements](v3-requirements.md) | Original v3 requirement set |
| [MCP_CONNECTION_LIFECYCLE](MCP_CONNECTION_LIFECYCLE.md) | Proposed: stable MCP connections, credential rotation, TTL and cleanup |
| [WAKE_AND_DISPATCH](WAKE_AND_DISPATCH.md) | Direction accepted (v3): one surface (`veynod up` + hub) for wake, dispatch and the Loop in production; desktop frozen; prior art survey |
| [HUMAN_DOCUMENT_LIBRARY_UX](HUMAN_DOCUMENT_LIBRARY_UX.md) | Proposed: search-first human document library and reading view |
| [SEO_TECHNICAL_PLAN](SEO_TECHNICAL_PLAN.md) | Implemented (Phase 0): English-first technical SEO, HTML/JSON content negotiation, legacy-domain removal |
| [desktop/README](../desktop/README.md) | **Frozen** (owner decision D-1, 2026-10-30): the Windows/desktop client is not developed further; every OS connects with the CLI (`veynod up` + hub) |

## Operations and security

| Document | What it covers |
| --- | --- |
| [DEPLOY_GATE](DEPLOY_GATE.md) | VPS deploy gate: install, checks G-01..G-05, owner switches, rollback — deferred, off the task path (see its roadmap section) |
| [SECURITY_AUDIT_2026-10-20](SECURITY_AUDIT_2026-10-20.md) | Attacker-style audit and remediation record |
| [design-system](design-system/FOUNDATION.md) | Tokens, typography, components and accessibility rules |
| [benchmarks](benchmarks/) | Retrieval and performance measurements |

## Doc conventions

- **Public product docs** live at `/docs/` and are generated from
  `app/docs_site.py`; do not duplicate them here.
- **Workspace documents** are customer content inside the product, not these
  files. The human Library UX spec is
  [HUMAN_DOCUMENT_LIBRARY_UX](HUMAN_DOCUMENT_LIBRARY_UX.md).
- **Proposed** means the document is a decision-ready spec; implementation
  status lives in [PRD_PROGRESS](PRD_PROGRESS.md).
- **Historical** documents (audits, feedback rounds, old requirement sets) keep
  their original dates and are not rewritten.
