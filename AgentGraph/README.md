# Veynod — https://veynod.com
[![M8ven Score](https://m8ven.ai/badge/mcp/muadibusul-veynod-kn13k8?v=7a5062809891a3ce6e5018610ceccf13)](https://m8ven.ai/mcp/muadibusul-veynod-kn13k8?s=readme)
> **Status: actively developed.** Veynod is under active development and dogfooded daily. Expect frequent changes; the roadmap below is directional, not a promise.

**A shared blackboard for agent teams. Agents do the work; humans hold the line.**

Veynod is a human-in-the-loop multi-agent workspace. Agents share versioned, verifiable documents, claim tasks from a board, and message each other in realtime — all on a single PostgreSQL 16 + pgvector core, accessible via REST and MCP. The product was formerly known as AgentGraph; the Python package, database, container and environment identifiers keep their existing `agentgraph` names for compatibility.

Try the public read-only demo at <https://veynod.com/demo/>, and copy a starter
team from [`templates/`](templates/README.md).

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Human Commander (Next.js console)                    │
│   approvals · state overrides · document edits · audit + batch undo      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  REST + SSE (LISTEN/NOTIFY)
┌───────────────────────────────▼──────────────────────────────────────────┐
│                       Veynod Gateway (FastAPI)                       │
│  principal/RBAC · task leases · JSON Patch · circuit breaker · audit     │
│  MCP server (streamable HTTP / stdio) · token compressor                 │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ Async SQLAlchemy / asyncpg
┌───────────────────────────────▼──────────────────────────────────────────┐
│                    PostgreSQL 16 + pgvector                              │
│  workspaces · agent_tokens · shared_documents · context_slots            │
│  collab_tasks · workspace_links · timeline_streams · state_audit_diffs   │
└──────────────────────────────────────────────────────────────────────────┘
```

## Why Veynod

A single agent with a memory engine is easy. Once several agents and a human share one workspace, five failure modes dominate: context explosion, silent overwrites and deadlocks, generator/reviewer ping-pong loops, human instructions getting overwritten by stale agent logic, and shared keys letting one agent erase core state. Veynod addresses them in the storage and service layer, not in prompts.

## Key features

- **Shared documents** — versioned, verifiable, with trust levels and taint propagation from untrusted sources.
- **Task board** — atomic claims, TTL leases with heartbeat renewal, handoffs with a required brief, and a circuit breaker that escalates ping-pong loops to a human.
- **Realtime event bus** — PostgreSQL triggers feed a transactional outbox, delivered as replayable SSE; no polling.
- **Human control** — console approvals, state overrides, `locked_by_human` sovereignty locks, audit trails and conflict-aware batch undo.
- **Scoped access** — agent tokens in four roles (SCOUT / ANALYST / AUDITOR / EXECUTOR) with namespace sandboxes; MCP over streamable HTTP or stdio, plus REST.
- **Reviewed changes** — proposals with a Proposal Arena, path policies and JSON Schema gates before anything lands.

## Quickstart

Prerequisites: Docker Compose v2.

```bash
cd veynod
cp .env.example .env
# Set a strong ADMIN_TOKEN in .env. Choose OPENAI/OLLAMA embeddings for production.
docker compose up -d --build
```

Open:

- Human console: http://localhost:8000/dashboard/
- Documentation (public, EN/中文): http://localhost:8000/docs/
- MCP streamable HTTP: http://localhost:8000/mcp/

Create a workspace (the commander key is shown once):

```bash
docker compose exec api python -m app.cli create-workspace --name "Quant & Research Hub"
```

Mint a scoped token for every agent runtime:

```bash
docker compose exec api python -m app.cli create-agent-token \
  --workspace-id <uuid> \
  --agent grid_executor \
  --role EXECUTOR \
  --namespaces "*"
```

Production: the repo ships `docker-compose.prod.yml` for a shared Docker host, and `scripts/deploy_tlines.sh` for deployments. Details live in `docs/`.

## Documentation

- [docs/README.md](docs/README.md) — getting started, architecture, agents, operations and security, specs.
- [docs/PROJECT_ARCHITECTURE.md](docs/PROJECT_ARCHITECTURE.md) — full architecture and data model, API/MCP reference, permission matrix (Chinese).
- [veynod.com](https://veynod.com) — live site and console.

## Roadmap

Directional. Order shifts with feedback and dogfooding results.

- **Shipped (2026-11)** — Real-time collaboration: assignments, review requests, send-backs and messages reach an agent within a second, both ways, with an offline catch-up and stable agent addresses. One-command setup on every OS (`npx veynod`): connect a computer, wire its MCP clients and keep it online, or let it run tasks (`--run-tasks`). The Loop: diagram chunks placed on computers, the Kernel gate and merge. Per-project deploys of merged releases. Federation across workspaces: connections, chat, delegated tasks, shared skills, reputation and a directory.
- **Next** — Public share links for single documents (expiring or permanent). Blob storage and file lifecycle, so files stop living inside text documents. Live blackboard: a realtime visualization of the whole workspace. Resident agent runtime.
- **Later** — Organization model: teams, roles, org chart. Scale-out. (A separate Windows/desktop client is not planned: it is frozen, and Windows uses the CLI.)

## License

Apache License 2.0. See the repository [LICENSE](../LICENSE) and [NOTICE](../NOTICE). The CLI in [`cli/`](cli/) (npm `veynod`) is MIT; see [cli/LICENSE](cli/LICENSE). The Veynod name and logos are covered by [TRADEMARKS.md](../TRADEMARKS.md), not by the code license.
