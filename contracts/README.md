# Veynod Cross-Domain Contracts

Status: **Phase 1A foundation** — proposed, unit-tested, not yet wired into every service.

This package is the dependency-free vocabulary shared by:

- AgentGraph (Product Control);
- Agent Runtime (Phase 2, not built yet);
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

## Rules

1. IDs are opaque UUID strings; never reuse an ID for a different object.
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

- No service imports `contracts` in its request path yet.
- No event dispatcher/consumer uses the envelope yet.
- No contract test runs AgentGraph -> Kernel -> AgentGraph end to end.
- Phase 1B/3 must wire these definitions into both services and add
  consumer/projection tests.
