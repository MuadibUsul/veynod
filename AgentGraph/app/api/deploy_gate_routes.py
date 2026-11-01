"""VPS deploy gate routes (v3.50, agentgraph/spec-vps-auto-deploy-gate v1).

* ``POST /v1/deploy-gate/github`` — the GitHub webhook; only a valid
  ``X-Hub-Signature-256`` is read, and only a merge into the branch queues.
* ``/v1/deploy-gate/runner/*`` — the runner on the VPS, with its own bearer
  token (DEPLOY_GATE_RUNNER_TOKEN), never the admin token.
* ``/v1/admin/deploy-gate*`` — the operator: state, runs, audit, and the
  trigger / pause / resume / authorize / revoke switches.

Without DEPLOY_GATE_WEBHOOK_SECRET or DEPLOY_GATE_RUNNER_TOKEN the matching
routes answer 404, so an unconfigured server exposes nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request

from app.auth import extract_bearer_token, require_admin
from app.errors import ApiError, NotFound
from app.schemas_deploy_gate import (
    DeployGateClaim,
    DeployGateHeartbeat,
    DeployGateReason,
    DeployGateRunnerEnqueue,
    DeployGateRunUpdate,
    DeployGateTaskEvidence,
    DeployGateTriggerRequest,
)
from app.services import deploy_gate

# GitHub caps deliveries at 25 MB; a merge into main is far smaller.
WEBHOOK_MAX_BYTES = 5_000_000

# The webhook and runner routes serve GitHub and the VPS, not agents: keep them
# out of the OpenAPI document, the docs site and the capability listing.
public_router = APIRouter(prefix="/v1/deploy-gate", tags=["Deploy gate"],
                          include_in_schema=False)
admin_router = APIRouter(prefix="/v1/admin/deploy-gate", tags=["Deploy gate"],
                         dependencies=[Depends(require_admin)])


async def require_runner(request: Request) -> None:
    if not deploy_gate.runner_authorized(extract_bearer_token(request)):
        if not deploy_gate.settings.deploy_gate_runner_token:
            raise NotFound("deploy_gate", "the deploy gate runner is not configured")
        raise ApiError(401, "unauthorized", "invalid runner token",
                       headers={"WWW-Authenticate": "Bearer"})


runner_router = APIRouter(prefix="/v1/deploy-gate/runner", tags=["Deploy gate"],
                          include_in_schema=False, dependencies=[Depends(require_runner)])


@public_router.post("/github", summary="GitHub webhook: queue a merge into the deploy branch")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > WEBHOOK_MAX_BYTES:
        raise ApiError(413, "payload_too_large", "the webhook body is too large")
    body = await request.body()
    if len(body) > WEBHOOK_MAX_BYTES:
        raise ApiError(413, "payload_too_large", "the webhook body is too large")
    return await deploy_gate.receive_github(x_github_event, x_github_delivery, body,
                                            x_hub_signature_256)


# ------------------------------------------------------------------ runner


@runner_router.post("/heartbeat", summary="Runner heartbeat; returns the gate state")
async def runner_heartbeat(body: DeployGateHeartbeat) -> dict:
    return await deploy_gate.heartbeat(body.host, body.version, body.deployed_commit)


@runner_router.post("/runs", status_code=201,
                    summary="Queue a commit the runner's timer found on the branch")
async def runner_enqueue(body: DeployGateRunnerEnqueue) -> dict:
    return await deploy_gate.runner_enqueue(body.commit, body.host)


@runner_router.post("/claim", summary="Claim the newest queued run")
async def runner_claim(body: DeployGateClaim) -> dict:
    return await deploy_gate.claim(body.host)


@runner_router.post("/runs/{run_id}", summary="Report a claimed run's checks and outcome")
async def runner_update(run_id: str, body: DeployGateRunUpdate) -> dict:
    return await deploy_gate.update_run(run_id, body.host, body.model_dump())


@runner_router.post("/task-evidence",
                    summary="G-03: review verdicts and gates of the tasks a PR names")
async def runner_task_evidence(body: DeployGateTaskEvidence) -> dict:
    return await deploy_gate.task_evidence(body.task_keys)


# ---------------------------------------------------------------- operator


@admin_router.get("", summary="Deploy gate state and recent runs")
async def gate_overview(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    return await deploy_gate.overview(limit)


@admin_router.get("/runs/{run_id}", summary="One run with its checks and audit trail")
async def gate_run(run_id: str) -> dict:
    return await deploy_gate.get_run(run_id)


@admin_router.get("/audit", summary="The deploy gate audit log, newest first")
async def gate_audit(limit: int = Query(default=50, ge=1, le=200),
                     before: int | None = Query(default=None, ge=1)) -> dict:
    return await deploy_gate.list_audit(limit, before)


@admin_router.post("/trigger", status_code=201, summary="Run the gate once now")
async def gate_trigger(body: DeployGateTriggerRequest) -> dict:
    return await deploy_gate.trigger_manual(body.commit, body.reason)


@admin_router.post("/pause", summary="Pause the gate: nothing deploys until resumed")
async def gate_pause(body: DeployGateReason | None = None) -> dict:
    return await deploy_gate.set_paused(True, body.reason if body else None)


@admin_router.post("/resume", summary="Resume the gate")
async def gate_resume(body: DeployGateReason | None = None) -> dict:
    return await deploy_gate.set_paused(False, body.reason if body else None)


@admin_router.post("/authorize", summary="Allow automatic deploys after a passing gate")
async def gate_authorize(body: DeployGateReason | None = None) -> dict:
    return await deploy_gate.set_authorized(True, body.reason if body else None)


@admin_router.post("/revoke", summary="Revoke automatic deploys (manual triggers still work)")
async def gate_revoke(body: DeployGateReason | None = None) -> dict:
    return await deploy_gate.set_authorized(False, body.reason if body else None)
