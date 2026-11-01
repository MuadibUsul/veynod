# Agent Runtime — Phase 2A

Status: **foundation only**.  This package persists Agent Run state; it does
not call models or tools yet, and it is not wired into AgentGraph.

## What is real

- PostgreSQL schema `agent_runtime` with versioned migrations:
  - `001_init`, `002_invocations_usage` in `agent_runtime/migrations/`;
  - `schema_migrations` ledger.
- Internal FastAPI service (`agent_runtime/api.py`, `X-Runtime-Key`):
  - create/get run;
  - transition/pause/resume/cancel;
  - checkpoint/list checkpoints;
  - list events;
  - `/healthz`.
- Run state machine (`states.py`) with explicit allowed transitions.
- `RunStore`:
  - create run (optional idempotency key);
  - optimistic `transition(expected_version)` with row lock + event/outbox;
  - checkpoint with idempotency key;
  - step and invocation persistence;
  - usage accounting;
  - read run/events/checkpoints.
- `LoopController` (`loop.py`):
  - one persisted step = model decision -> optional tool -> checkpoint;
  - tool idempotency key reuse prevents a second side effect;
  - budget pre-check and post-model check;
  - cancellation check before model and before tool;
  - restart from PostgreSQL checkpoint.
- `RuntimeWorker` (`worker.py`):
  - claim one RUNNING run under a fenced lease;
  - heartbeat/renew lease;
  - guard before every write; lost lease stops the step;
  - release after one step; next worker/loop can claim the next step.
- HTTP adapters (`gateway.py`):
  - `HTTPModelGateway`: OpenAI-compatible `/chat/completions`, JSON action,
    retries on 429/5xx/timeout, usage accounting;
  - `HTTPToolRuntime`: allowlist, timeout, `Idempotency-Key`, retries with the
    same key.
- `ContextBuilder` (`context.py`):
  - system policy / product task / checkpoint / recent steps / extra refs;
  - priority order and token budget;
  - truncates or omits low-priority sections, keeps refs.
- `DelegationManager` (`delegation.py`):
  - parent RUNNING → child Run(s);
  - parent → WAITING_CHILDREN;
  - idempotent delegation key;
  - `collect` resumes parent only when every child is terminal;
  - `reconcile` resumes a parent with no children or all-terminal children;
  - API: delegate / children / collect / delegations-reconcile.
- `RuntimeWorkerPool` (`pool.py`):
  - N supervised worker loops; crashed loop restarted;
  - snapshot of workers/ids;
  - `worker_main` starts a pool.
- `RoutingModelGateway` (`router.py`): preferred/allowed provider order with fallback.
- `QuotaPolicy` (`quota.py`): tenant-level call/token/cost ceiling, checked before model calls.
- `ReconciliationLoop` (`maintenance.py`): periodic delegation reconciliation.
- Worker process entry (`worker_main.py`): env-configured RuntimeWorkerPool.

## Operations endpoints

- `GET /v1/quota/{tenant_id}` — quota policy, current usage, `exceeded`/`reason`.
  AgentGraph calls it before creating a Run and returns 429 when exceeded.
- `GET /v1/provider-health?window_minutes=15` — invocation outcomes per
  `kind` (`model`/`tool`), failure ratio and a `healthy`/`degraded`/`unknown`
  status from the `invocations` table.
- Worker `GET /metrics` on `RUNTIME_WORKER_METRICS_PORT` (default 8031) — pool
  workers, ids, stopping flag.
- `scripts/runtime_supervisor.py` — restarts the worker process with backoff
  and optional metrics-based health restarts.

## What is not done

- No autoscaling/leader election; every worker points at the same database.
- No production API key guard beyond `X-Runtime-Key` (deploy behind a private
  network or mTLS).
- No production deployment (compose/K8s manifest); the supervisor script and
  a systemd/NSSM unit are the current answer.
- No multi-run concurrency/lease-expiry fault-injection test yet.

## Run the internal API

```bash
cd E:/Codes/veynod
RUNTIME_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_dev' \
RUNTIME_API_KEY='dev-runtime' \
python -m uvicorn agent_runtime.api:app --host 127.0.0.1 --port 8002
```

Endpoints require `X-Runtime-Key`. This is an internal service, not a product
entry point.

## Test

```bash
cd E:/Codes/veynod
RUNTIME_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test' \
python -m pytest -q agent_runtime/tests
```

The test fixture drops/creates the `agent_runtime` schema in the target test
database.  Never point it at production.
