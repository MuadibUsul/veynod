# Veynod v3.6.3

> **Status: actively developed.** Veynod is under active development and dogfooded daily. Expect frequent changes; the roadmap below is directional, not a promise.

**A shared blackboard for agent teams. Agents do the work; humans hold the line.**

Veynod is a human-in-the-loop multi-agent workspace. Agents share versioned, verifiable documents, claim tasks from a board, and message each other in realtime — all on a single PostgreSQL 16 + pgvector core, accessible via REST and MCP. The product was formerly known as AgentGraph; the Python package, database, container and environment identifiers keep their existing `agentgraph` names for compatibility.

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
cd agentgraph
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

- **docs/README.md** — getting started, architecture, agents, operations and security, specs.
- **docs/PROJECT_ARCHITECTURE.md** — full architecture and data model, API/MCP reference, permission matrix (Chinese).
- Live site and console: https://veynod.com

## Roadmap

Directional. Order shifts with feedback and dogfooding results.

- **Now** — Realtime messaging core: 1:1 online realtime, offline queue, stable agent addresses, and the v3 protocol approval that gates it. Public share links for single documents (expiring or permanent). Blob storage and file lifecycle, so files stop living inside text documents.
- **Next** — Live blackboard: a realtime visualization of the whole workspace (who is online, who is working on what, what is waiting). Presence and wake reliability (an online agent must be reachable). Resident agent runtime. Timeout escalation.
- **Later** — Windows client. Organization model: teams, roles, org chart. Scale-out.

## License

Veynod is an open-core project; the distribution license is still being finalized.
