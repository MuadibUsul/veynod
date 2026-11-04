"""Federation API (round 2): this workspace's identity, resolving others, and
connections with other workspaces.

Contracts: doc://federation-i-id-identity-icd §7.2, docs/federation/ICD_I_GRANT.md.
Everything here answers 404 ``federation_disabled`` while FEDERATION_ENABLED
is false (the default). These are local endpoints for this workspace's people
and agents; peers never call them (they only exchange signed envelopes).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Principal, capability_role, get_principal
from app.errors import ApiError
from app.services import federation
from app.services.federation import grants, identity
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor

router = APIRouter(prefix="/v1/federation", tags=["Federation"])

T = TypeVar("T")
_FID = r"^fed:(subject|workspace|agent):[a-z2-7]{26}$"
_CID = r"^fcn_[a-z2-7]{26}$"
Scope = Literal["chat", "delegate", "presence"]

# One registered API code per kind of refusal; the precise federation code
# (ICD error tables) travels in details.federation_code.
_API_CODE = {
    403: "federation_forbidden",
    404: "federation_not_found",
    409: "federation_conflict",
    410: "federation_not_found",
    413: "federation_invalid",
    422: "federation_invalid",
    503: "federation_unavailable",
}


async def _call(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except FederationError as exc:
        code = (
            "federation_disabled"
            if exc.code == "FED_E_DISABLED"
            else _API_CODE.get(exc.status, "federation_invalid")
        )
        raise ApiError(
            exc.status,
            code,
            exc.message,
            details={"federation_code": exc.code, **exc.detail},
        ) from exc


def _enabled() -> None:
    if not federation.is_enabled():
        raise ApiError(404, "federation_disabled", "federation is disabled on this deployment")


def _actor(principal: Principal) -> Actor:
    """People act as themselves; every other credential is an agent."""
    if principal.is_human:
        return Actor(
            "user",
            str(principal.user_id or principal.agent_signature),
            is_owner=capability_role(principal) == "OWNER",
        )
    return Actor("agent", principal.agent_signature)


def _workspace_id(principal: Principal) -> str:
    return str(principal.workspace.id)


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peer_workspace: str = Field(pattern=_FID, description="The other workspace's federation id.")
    outbound_scopes: list[Scope] = Field(
        default_factory=list, description="What this workspace may do to the peer."
    )
    inbound_scopes: list[Scope] = Field(
        default_factory=list, description="What the peer may do to this workspace."
    )
    reason: str = Field(min_length=5, max_length=500)
    ttl_days: int = Field(default=grants.DEFAULT_TTL_DAYS, ge=1, le=grants.MAX_TTL_DAYS)


class ScopeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outbound_scopes: list[Scope] | None = None
    inbound_scopes: list[Scope] | None = None


class AliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=64)


@router.get("/me", summary="This workspace's federation identity (null until enabled)")
async def me(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _enabled()
    return {"identity": await _call(identity.local_identity(_workspace_id(principal)))}


@router.post("/enable", summary="Create this workspace's federation identity (owner only)")
async def enable(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _enabled()
    actor = _actor(principal)
    if not actor.is_owner:
        raise ApiError(
            403,
            "federation_forbidden",
            "only an owner-level person can enable federation for a workspace",
            details={"federation_code": "FED_E_APPROVAL_NOT_HUMAN"},
        )
    view = await _call(identity.enable_workspace(_workspace_id(principal), actor=actor.ref))
    return {"identity": view}


@router.put("/me/alias", summary="Publish or rename this workspace's handle (owner only)")
async def set_alias(
    body: AliasRequest, principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _enabled()
    actor = _actor(principal)
    if not actor.is_owner:
        raise ApiError(
            403,
            "federation_forbidden",
            "only an owner-level person can publish a handle",
            details={"federation_code": "FED_E_APPROVAL_NOT_HUMAN"},
        )
    mine = await _call(identity.local_identity(_workspace_id(principal)))
    if mine is None:
        raise ApiError(
            404,
            "federation_not_found",
            "enable federation for this workspace first",
            details={"federation_code": "FED_E_IDENTITY_UNKNOWN"},
        )
    view = await _call(identity.set_alias(mine["federation_id"], body.handle, actor=actor.ref))
    return {"identity": view}


@router.get("/identities/{federation_id}", summary="Resolve a federation id")
async def get_identity(
    federation_id: str = Path(pattern=_FID),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _enabled()
    return {"identity": await _call(identity.resolve(federation_id))}


@router.get("/resolve", summary="Resolve a handle (ambiguity is reported, never guessed)")
async def resolve(
    subject: str | None = Query(default=None, max_length=64),
    workspace: str | None = Query(default=None, max_length=64),
    agent: str | None = Query(default=None, max_length=64),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _enabled()
    return await _call(identity.resolve_alias(subject=subject, workspace=workspace, agent=agent))


@router.get("/connections", summary="This workspace's connections with other workspaces")
async def list_connections(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _enabled()
    return {"items": await _call(grants.list_connections(_workspace_id(principal)))}


@router.post(
    "/connections", status_code=201, summary="Ask another workspace to connect (owner only)"
)
async def request_connection(
    body: ConnectionRequest, principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _enabled()
    connection = await _call(
        grants.request(
            _workspace_id(principal),
            peer_workspace=body.peer_workspace,
            outbound_scopes=list(body.outbound_scopes),
            inbound_scopes=list(body.inbound_scopes),
            reason=body.reason,
            ttl_days=body.ttl_days,
            actor=_actor(principal),
        )
    )
    return {"connection": connection}


@router.get("/connections/{connection_id}/history", summary="One connection's history on this side")
async def connection_history(
    connection_id: str = Path(pattern=_CID),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _enabled()
    return {"items": await _call(grants.history(_workspace_id(principal), connection_id))}


@router.post(
    "/connections/{connection_id}/{action}",
    summary="Approve (owner), reject, revoke, freeze, unfreeze (owner) or block a connection",
)
async def act_on_connection(
    body: ScopeChange | None = None,
    connection_id: str = Path(pattern=_CID),
    action: Literal["approve", "reject", "revoke", "freeze", "unfreeze", "block"] = Path(),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _enabled()
    workspace_id, actor = _workspace_id(principal), _actor(principal)
    if action == "approve":
        body = body or ScopeChange()
        result = grants.approve(
            workspace_id,
            connection_id,
            actor=actor,
            outbound_scopes=None if body.outbound_scopes is None else list(body.outbound_scopes),
            inbound_scopes=None if body.inbound_scopes is None else list(body.inbound_scopes),
        )
    else:
        result = getattr(grants, action)(workspace_id, connection_id, actor=actor)
    return {"connection": await _call(result)}


@router.patch("/connections/{connection_id}/scopes", summary="Narrow a connection's scopes")
async def narrow_scopes(
    body: ScopeChange,
    connection_id: str = Path(pattern=_CID),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _enabled()
    connection = await _call(
        grants.change_scopes(
            _workspace_id(principal),
            connection_id,
            actor=_actor(principal),
            outbound_scopes=list(body.outbound_scopes or []),
            inbound_scopes=list(body.inbound_scopes or []),
        )
    )
    return {"connection": connection}
