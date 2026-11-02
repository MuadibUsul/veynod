# Veynod Cross-Domain Contracts

Status: **Phase 1 contract** — WorkScope is enforced by the Kernel task, lease and submission paths. The event envelope and unified execution-owner transition remain contract definitions pending later phases.

This package is the dependency-free vocabulary shared by:

- AgentGraph (Product Control);
- Agent Runtime;
- Veynod Kernel (Code Execution).

It contains no database access and no framework code.  The goal is to stop each
service from inventing its own identifier, event and error names.

## Modules

| Module | Purpose |
|---|---|
| `identifiers.py` | `IdentityChain`, UUID/commit validation, correlation IDs |
| `events.py` | versioned `EventEnvelope`, event type registry, producer registry |
| `errors.py` | stable cross-domain `ErrorCode` and retryability |
| `idempotency.py` | canonical SHA-256 keys and key normalization |
| `work_scope.py` / `work-scope.schema.json` | canonical path/symbol boundary and content hash |
| `work-item.schema.json` | identity, attempt and execution-owner fields |
| `event-envelope.schema.json` | versioned cross-domain event shape |

## Rules

1. UUID-backed objects use UUIDs; existing tenant IDs, Diagram Chunk keys, Product Task keys and placement IDs are opaque strings. Never reuse an ID for a different object.
2. A missing downstream ID is not the same as a different ID.
3. Events are at-least-once; consumers deduplicate by `event_id` and reject
   older `aggregate_version`.
4. Large bodies use `payload_ref`; `payload` stays small.
5. Error codes are stable; services may add `details` but not rename a class.
6. This package does not replace the Kernel/AgentGraph schemas.  Database
   ownership and migrations remain separate.

## Test

```bash
cd E:/Codes/veynod
python -m pytest -q contracts/tests
```

## Not done yet

- Kernel imports `contracts` to parse and enforce WorkScope. AgentGraph and Agent Runtime carry the Diagram scope through compatibility adapters without parsing its patterns.
- No event dispatcher/consumer uses the new envelope yet.
- No contract test runs AgentGraph -> Kernel -> AgentGraph end to end.
- Phase 1B/3 must wire these definitions into both services and add
  consumer/projection tests.
