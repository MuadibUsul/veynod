"""Internal Agent Runtime API (Phase 2C).

This service owns Agent Run state only.  It is protected by ``X-Runtime-Key``
and is not exposed to end users; AgentGraph will call it in Phase 2D.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from .delegation import DelegationManager
from .errors import (
    IdempotencyConflict,
    InvalidTransition,
    RunConflict,
    RunNotFound,
)
from .quota import QuotaPolicy
from .store import RunStore


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    product_task_id: str | None = Field(default=None, max_length=256)
    goal: str | None = Field(default=None, max_length=20_000)
    project_id: str | None = Field(default=None, max_length=128)
    repo_path: str | None = Field(default=None, max_length=1024)
    trace_id: str | None = Field(default=None, max_length=64)
    kernel: dict[str, Any] = Field(default_factory=dict)
    workflow_id: UUID | None = None
    workflow_version: int | None = Field(default=None, ge=1)
    budget: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=256)
    idempotency_key: str | None = Field(default=None, max_length=200)


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=8_000)
    idempotency_key: str | None = Field(default=None, max_length=200)


class CheckpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: dict[str, Any]
    idempotency_key: str | None = Field(default=None, max_length=200)


class DelegateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_task_id: str | None = Field(default=None, max_length=256)
    budget: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=8_000)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, max_length=200)


def create_app(
    database_url: str,
    api_key: str,
    *,
    schema: str = "agent_runtime",
    reset_schema: bool = False,
    quota: QuotaPolicy | None = None,
) -> FastAPI:
    quota_policy = quota or QuotaPolicy.from_env()
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = RunStore(database_url, schema=schema)
        await store.init_schema(reset=reset_schema)
        app.state.store = store
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="Veynod Agent Runtime", version="0.2.0", lifespan=lifespan)

    async def require_key(
        x_runtime_key: str | None = Header(default=None, alias="X-Runtime-Key"),
    ) -> None:
        if not x_runtime_key or not hmac.compare_digest(
            x_runtime_key.encode("utf-8"), api_key.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid X-Runtime-Key.",
            )

    def store() -> RunStore:
        return app.state.store

    def _error(exc: Exception) -> HTTPException:
        if isinstance(exc, RunNotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, (RunConflict, IdempotencyConflict, InvalidTransition)):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="runtime error")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "agent-runtime", "version": "0.2.0"}

    @app.post("/v1/runs", status_code=201, dependencies=[Depends(require_key)])
    async def create_run(body: RunCreate) -> dict:
        try:
            return await store().create_run(
                tenant_id=body.tenant_id,
                workspace_id=body.workspace_id,
                product_task_id=body.product_task_id,
                goal=body.goal,
                workflow_id=body.workflow_id,
                workflow_version=body.workflow_version,
                budget=body.budget,
                correlation_id=body.correlation_id,
                idempotency_key=body.idempotency_key,
                project_id=body.project_id,
                repo_path=body.repo_path,
                trace_id=body.trace_id,
                kernel=body.kernel,
            )
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get("/v1/runs/{run_id}", dependencies=[Depends(require_key)])
    async def get_run(run_id: UUID) -> dict:
        try:
            return await store().get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post(
        "/v1/runs/{run_id}/transition", dependencies=[Depends(require_key)]
    )
    async def transition(run_id: UUID, body: TransitionRequest) -> dict:
        try:
            return await store().transition(
                run_id,
                body.target,
                expected_version=body.expected_version,
                reason=body.reason,
                actor="api",
                idempotency_key=body.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post("/v1/runs/{run_id}/start", dependencies=[Depends(require_key)])
    async def start(run_id: UUID) -> dict:
        try:
            return await store().start_run(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post("/v1/runs/{run_id}/pause", dependencies=[Depends(require_key)])
    async def pause(run_id: UUID, body: ReasonRequest) -> dict:
        try:
            run = await store().get_run(run_id)
            await store().transition(
                run_id,
                "PAUSING",
                expected_version=body.expected_version or run["version"],
                reason=body.reason or "pause requested",
                actor="api",
            )
            run = await store().get_run(run_id)
            await store().transition(
                run_id,
                "PAUSED",
                expected_version=run["version"],
                reason=body.reason or "pause requested",
                actor="api",
                idempotency_key=body.idempotency_key,
            )
            return await store().get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post("/v1/runs/{run_id}/resume", dependencies=[Depends(require_key)])
    async def resume(run_id: UUID, body: ReasonRequest) -> dict:
        try:
            run = await store().get_run(run_id)
            await store().transition(
                run_id,
                "RESUMING",
                expected_version=body.expected_version or run["version"],
                reason=body.reason or "resume requested",
                actor="api",
            )
            run = await store().get_run(run_id)
            await store().transition(
                run_id,
                "RUNNING",
                expected_version=run["version"],
                reason=body.reason or "resume requested",
                actor="api",
                idempotency_key=body.idempotency_key,
            )
            return await store().get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(require_key)])
    async def cancel(run_id: UUID, body: ReasonRequest) -> dict:
        try:
            run = await store().get_run(run_id)
            return await store().transition(
                run_id,
                "CANCELLED",
                expected_version=body.expected_version or run["version"],
                reason=body.reason or "cancel requested",
                actor="api",
                idempotency_key=body.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post(
        "/v1/runs/{run_id}/checkpoint", dependencies=[Depends(require_key)]
    )
    async def checkpoint(run_id: UUID, body: CheckpointRequest) -> dict:
        try:
            return await store().checkpoint(
                run_id, body.state, idempotency_key=body.idempotency_key
            )
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post(
        "/v1/runs/{run_id}/delegate", dependencies=[Depends(require_key)]
    )
    async def delegate(run_id: UUID, body: DelegateRequest) -> dict:
        try:
            return await DelegationManager(store()).delegate(
                str(run_id),
                product_task_id=body.product_task_id,
                budget=body.budget,
                idempotency_key=body.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get(
        "/v1/runs/{run_id}/children", dependencies=[Depends(require_key)]
    )
    async def children(run_id: UUID) -> list[dict]:
        try:
            return await store().list_children(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post(
        "/v1/runs/{run_id}/collect", dependencies=[Depends(require_key)]
    )
    async def collect(run_id: UUID) -> dict:
        try:
            return await DelegationManager(store()).collect(str(run_id))
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post("/v1/delegations/reconcile", dependencies=[Depends(require_key)])
    async def reconcile_delegations() -> dict:
        try:
            return await DelegationManager(store()).reconcile_all()
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get("/v1/runs/{run_id}/steps", dependencies=[Depends(require_key)])
    async def steps(
        run_id: UUID,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict]:
        try:
            return await store().list_steps(run_id, limit)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get(
        "/v1/runs/{run_id}/external", dependencies=[Depends(require_key)]
    )
    async def external(run_id: UUID) -> list[dict]:
        try:
            return await store().list_external(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post(
        "/v1/runs/{run_id}/wakeups/apply", dependencies=[Depends(require_key)]
    )
    async def apply_wakeups(run_id: UUID) -> dict:
        del run_id  # wakeups are global; kept for API symmetry
        try:
            return await store().consume_wakeups()
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.post(
        "/v1/runs/{run_id}/wakeups", dependencies=[Depends(require_key)]
    )
    async def inject_wakeup(run_id: UUID, body: dict) -> dict:
        """Ops/test hook: deliver a submission result without the event feed."""
        try:
            submission_id = str(body.get("submission_id") or "")
            if not submission_id:
                raise ValueError("submission_id is required")
            inserted = await store().add_wakeup(
                kind="SUBMISSION_FINAL",
                ref_id=submission_id,
                kernel_event_seq=int(body.get("kernel_event_seq") or 0),
                payload=body.get("payload") or {},
            )
            return {"inserted": inserted, "run_id": str(run_id)}
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get(
        "/v1/runs/{run_id}/checkpoints", dependencies=[Depends(require_key)]
    )
    async def checkpoints(run_id: UUID) -> list[dict]:
        try:
            return await store().list_checkpoints(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get("/v1/runs/{run_id}/events", dependencies=[Depends(require_key)])
    async def events(
        run_id: UUID,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> list[dict]:
        try:
            return await store().list_events(run_id, after_seq, limit)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get("/v1/events/head", dependencies=[Depends(require_key)])
    async def global_events_head() -> dict:
        return {"latest_event_seq": await store().latest_event_seq()}

    @app.get("/v1/events", dependencies=[Depends(require_key)])
    async def global_events(
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> dict:
        try:
            rows = await store().list_events_global(after_seq, limit)
            next_seq = rows[-1]["event_seq"] if rows else after_seq
            return {"events": rows, "next_after_seq": next_seq}
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get("/v1/stats", dependencies=[Depends(require_key)])
    async def stats() -> dict:
        return {"statuses": await store().status_counts()}

    @app.get("/v1/usage/{tenant_id}", dependencies=[Depends(require_key)])
    async def usage(tenant_id: str) -> dict:
        try:
            return await store().tenant_usage(tenant_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc

    @app.get("/v1/quota/{tenant_id}", dependencies=[Depends(require_key)])
    async def quota_state(tenant_id: str) -> dict:
        try:
            usage_now = await store().tenant_usage(tenant_id)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc
        reason = quota_policy.exceeded(usage_now)
        return {
            "tenant_id": tenant_id,
            "policy": {
                "max_model_calls": quota_policy.max_model_calls,
                "max_prompt_tokens": quota_policy.max_prompt_tokens,
                "max_completion_tokens": quota_policy.max_completion_tokens,
                "max_cost_usd": quota_policy.max_cost_usd,
            },
            "usage": usage_now,
            "exceeded": reason is not None,
            "reason": reason,
        }

    @app.get("/v1/provider-health", dependencies=[Depends(require_key)])
    async def provider_health(
        window_minutes: int = Query(default=15, ge=1, le=1440),
    ) -> dict:
        try:
            groups = await store().invocation_health(window_minutes)
        except Exception as exc:  # noqa: BLE001
            raise _error(exc) from exc
        providers: dict[str, dict[str, Any]] = {}
        for row in groups:
            entry = providers.setdefault(
                row["kind"],
                {"calls": 0, "failed": 0, "failed_ratio": 0.0, "last_failure_at": None},
            )
            entry["calls"] += row["count"]
            if row["status"] == "FAILED":
                entry["failed"] += row["count"]
                if row["last_finished_at"] is not None:
                    entry["last_failure_at"] = row["last_finished_at"]
        for entry in providers.values():
            if entry["calls"]:
                entry["failed_ratio"] = round(entry["failed"] / entry["calls"], 4)
        model = providers.get("model")
        if model is None or not model["calls"]:
            status_value = "unknown"
        elif model["failed_ratio"] > 0.1:
            status_value = "degraded"
        else:
            status_value = "healthy"
        return {
            "window_minutes": window_minutes,
            "status": status_value,
            "providers": providers,
            "groups": groups,
        }

    return app


def _env_app() -> FastAPI:
    database_url = os.environ.get(
        "RUNTIME_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_dev",
    )
    api_key = os.environ.get("RUNTIME_API_KEY", "runtime-dev")
    return create_app(database_url, api_key)


app = _env_app()

__all__ = ["app", "create_app"]
