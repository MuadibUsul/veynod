"""Federation P6 (R1-R3): simulated resource rights and the Agent Poker Arena.

Contract: docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md.

NO ECONOMIC VALUE: DEMO_M and ARENA_CHIP cannot be bought, withdrawn or
exchanged. Every route answers 404 while FEDERATION_ENABLED,
FEDERATION_RESOURCE_RIGHTS_ENABLED (and, for the arena,
FEDERATION_ARENA_ENABLED) are not all true; all are off by default.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.federation_routes import _actor, _call, _enabled, _workspace_id
from app.auth import Principal, get_principal, require_admin
from app.errors import ApiError
from app.services.federation import arena, rights

router = APIRouter(prefix="/v1/federation", tags=["Federation"])
admin_router = APIRouter(
    prefix="/v1/admin/federation",
    tags=["Federation"],
    dependencies=[Depends(require_admin)],
)

_FID_WS = r"^fed:workspace:[a-z2-7]{26}$"
_SPEC = r"^frs_[a-z2-7]{26}$"
_VOUCHER = r"^frv_[a-z2-7]{26}$"
_MATCH = r"^fam_[a-z2-7]{26}$"
_OB = r"^fob_[a-z2-7]{26}$"
_JOB = r"^fjob_[a-z2-7]{26}$"


def _rr() -> None:
    _enabled()
    if not rights.enabled():
        raise ApiError(
            404,
            "federation_disabled",
            "resource rights are off on this deployment",
            details={"federation_code": "FED_E_RR_DISABLED"},
        )


def _arena() -> None:
    _rr()
    if not arena.enabled():
        raise ApiError(
            404,
            "federation_disabled",
            "the arena is off on this deployment",
            details={"federation_code": "FED_E_ARENA_DISABLED"},
        )


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecDeclare(_Body):
    resource_class: Literal[
        "MOCK_DEMO",
        "SELF_REPORTED_SUBSCRIPTION",
        "CONTROLLED_PREPAID",
        "AUTHORIZED_PROVIDER_SERVICE",
    ]
    model_family: str = Field(min_length=1, max_length=80)
    allowed_use: str = Field(min_length=1, max_length=500)
    declared_tokens: int = Field(gt=0, le=10**10)
    measured_source: Literal["mock", "self_reported", "adapter", "platform"]
    confidence: Literal["low", "medium", "high"] = "low"
    license_basis_ref: str | None = Field(default=None, max_length=500)
    max_concurrency: int = Field(default=1, ge=1, le=100)
    expires_days: int = Field(default=90, ge=1, le=365)


class VoucherIssue(_Body):
    spec_id: str = Field(pattern=_SPEC)
    beneficiary: str = Field(pattern=_FID_WS)
    amount: int = Field(strict=True, gt=0, description="Tokens; 1 DEMO_M = 1,000,000.")
    unit: str | None = Field(default=None, description="DEMO_M only; anything else is refused.")
    usage_scope: str = Field(min_length=5, max_length=500)
    expires_days: int = Field(default=30, ge=1, le=365)


class Reserve(_Body):
    federation_job_id: str = Field(pattern=_JOB)
    amount: int = Field(strict=True, gt=0)
    unit: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)


class QueueJoin(_Body):
    spec_id: str = Field(pattern=_SPEC)
    stake: int = Field(strict=True, gt=0, description="Tokens of simulated capacity.")
    unit: str | None = None


class Authorize(_Body):
    contract_hash: str = Field(min_length=64, max_length=64)
    agent_play: bool = False


class Commit(_Body):
    commitment: str = Field(min_length=64, max_length=64)


class Reveal(_Body):
    seed: str = Field(min_length=64, max_length=64)


class Act(_Body):
    seq: int = Field(strict=True, ge=0)
    type: Literal["fold", "check", "call", "bet", "raise", "all_in"]
    amount: int | None = Field(default=None, strict=True)


class Reason(_Body):
    reason: str = Field(min_length=5, max_length=2000)


class Statement(_Body):
    statement: str = Field(min_length=5, max_length=2000)


class Compensate(_Body):
    spec_id: str = Field(pattern=_SPEC)


class MatchResolve(_Body):
    outcome: Literal["uphold", "void"]
    note: str = Field(min_length=5, max_length=2000)
    operator: str = Field(min_length=2, max_length=120)


class ObligationDecision(_Body):
    decision: Literal["confirm", "excuse", "resolve"]
    note: str = Field(min_length=5, max_length=2000)
    operator: str = Field(min_length=2, max_length=120)


async def _me(principal: Principal) -> str:
    from app.database import async_session_factory

    async with async_session_factory() as session:
        return await _call(rights.fid_of(session, _workspace_id(principal)))


# ── resource rights (R1) ──────────────────────────────────────────────────


@router.get("/rights/wallet", summary="This workspace's DEMO_M position (simulated, no value)")
async def rr_wallet(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _rr()
    return await _call(rights.wallet(await _me(principal)))


@router.post("/rights/specs", status_code=201, summary="Declare a resource (an owner)")
async def rr_declare(
    body: SpecDeclare, principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _rr()
    return await _call(
        rights.declare_spec(_workspace_id(principal), **body.model_dump(), actor=_actor(principal))
    )


@router.get("/rights/specs", summary="Resources this workspace declared")
async def rr_specs(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _rr()
    return {"items": await _call(rights.list_specs(_workspace_id(principal)))}


@router.post("/rights/specs/{spec_id}/revoke", summary="No new vouchers from a resource (owner)")
async def rr_revoke_spec(
    spec_id: str = Path(pattern=_SPEC), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _rr()
    return await _call(
        rights.revoke_spec(_workspace_id(principal), spec_id, actor=_actor(principal))
    )


@router.post("/rights/vouchers", status_code=201, summary="Grant a simulated right (a person)")
async def rr_issue(
    body: VoucherIssue, principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _rr()
    return await _call(
        rights.issue_voucher(_workspace_id(principal), **body.model_dump(), actor=_actor(principal))
    )


@router.get("/rights/vouchers", summary="Vouchers this workspace holds or issued")
async def rr_vouchers(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _rr()
    return await _call(rights.list_vouchers(_workspace_id(principal)))


@router.get("/rights/vouchers/{voucher_id}", summary="One voucher and its reservations")
async def rr_voucher(
    voucher_id: str = Path(pattern=_VOUCHER), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _rr()
    return await _call(rights.get_voucher_for(_workspace_id(principal), voucher_id))


@router.post(
    "/rights/vouchers/{voucher_id}/reserve",
    summary="Bind part of a voucher to a delegation sent to its issuer",
)
async def rr_reserve(
    body: Reserve,
    voucher_id: str = Path(pattern=_VOUCHER),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _rr()
    return await _call(
        rights.reserve(
            _workspace_id(principal),
            voucher_id=voucher_id,
            **body.model_dump(),
            actor=_actor(principal),
        )
    )


@router.post("/rights/vouchers/{voucher_id}/revoke", summary="Issuer takes back the unused part")
async def rr_revoke(
    voucher_id: str = Path(pattern=_VOUCHER), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _rr()
    return await _call(
        rights.revoke_voucher(_workspace_id(principal), voucher_id, actor=_actor(principal))
    )


@router.post("/rights/vouchers/{voucher_id}/waive", summary="Beneficiary gives up the unused part")
async def rr_waive(
    voucher_id: str = Path(pattern=_VOUCHER), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _rr()
    return await _call(
        rights.waive_voucher(_workspace_id(principal), voucher_id, actor=_actor(principal))
    )


# ── arena (R2) ────────────────────────────────────────────────────────────


@router.post("/arena/queue", summary="Queue for a heads-up match (a person; no economic value)")
async def arena_join(
    body: QueueJoin, principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.join_queue(_workspace_id(principal), **body.model_dump(), actor=_actor(principal))
    )


@router.delete("/arena/queue", summary="Leave the queue")
async def arena_leave_queue(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _arena()
    return await _call(arena.leave_queue(_workspace_id(principal), actor=_actor(principal)))


@router.get("/arena/matches", summary="Matches this workspace plays")
async def arena_matches(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _arena()
    return {"items": await _call(arena.list_matches(_workspace_id(principal)))}


@router.get("/arena/matches/{match_id}", summary="A seat's view: own cards only")
async def arena_match(
    match_id: str = Path(pattern=_MATCH), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _arena()
    return await _call(arena.get_match(_workspace_id(principal), match_id))


@router.post("/arena/matches/{match_id}/authorize", summary="Authorize the contract (an owner)")
async def arena_authorize(
    body: Authorize,
    match_id: str = Path(pattern=_MATCH),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.authorize(
            _workspace_id(principal), match_id, **body.model_dump(), actor=_actor(principal)
        )
    )


@router.post("/arena/matches/{match_id}/commit", summary="Publish sha256(seed)")
async def arena_commit(
    body: Commit,
    match_id: str = Path(pattern=_MATCH),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.commit_seed(
            _workspace_id(principal), match_id, commitment=body.commitment, actor=_actor(principal)
        )
    )


@router.post("/arena/matches/{match_id}/reveal", summary="Reveal the committed seed")
async def arena_reveal(
    body: Reveal,
    match_id: str = Path(pattern=_MATCH),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.reveal_seed(
            _workspace_id(principal), match_id, seed=body.seed, actor=_actor(principal)
        )
    )


@router.post("/arena/matches/{match_id}/act", summary="One action at action number seq")
async def arena_act(
    body: Act,
    match_id: str = Path(pattern=_MATCH),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _arena()
    action: dict[str, Any] = {"type": body.type}
    if body.amount is not None:
        action["amount"] = body.amount
    return await _call(
        arena.act(
            _workspace_id(principal), match_id, seq=body.seq, action=action, actor=_actor(principal)
        )
    )


@router.post("/arena/matches/{match_id}/leave", summary="Abort before play, concede in play")
async def arena_leave(
    match_id: str = Path(pattern=_MATCH), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _arena()
    return await _call(arena.leave(_workspace_id(principal), match_id, actor=_actor(principal)))


@router.post("/arena/matches/{match_id}/dispute", summary="Hold an unsettled result (a person)")
async def arena_dispute(
    body: Reason,
    match_id: str = Path(pattern=_MATCH),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.dispute(
            _workspace_id(principal), match_id, reason=body.reason, actor=_actor(principal)
        )
    )


@router.get("/arena/matches/{match_id}/verify", summary="Replay and check a finished match")
async def arena_verify(
    match_id: str = Path(pattern=_MATCH), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _arena()
    await _call(arena.get_match(_workspace_id(principal), match_id))  # a seat only
    return await _call(arena.verify(match_id))


# ── obligations (R3) ──────────────────────────────────────────────────────


@router.get("/arena/obligations", summary="Delivery obligations owed by or to this workspace")
async def arena_obligations(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _arena()
    return await _call(arena.list_obligations(_workspace_id(principal)))


@router.get("/arena/obligations/{obligation_id}", summary="One obligation, facts and history")
async def arena_obligation(
    obligation_id: str = Path(pattern=_OB), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _arena()
    return await _call(arena.get_obligation_for(_workspace_id(principal), obligation_id))


@router.post("/arena/obligations/{obligation_id}/statement", summary="Answer the facts (obligor)")
async def arena_statement(
    body: Statement,
    obligation_id: str = Path(pattern=_OB),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.state_case(
            _workspace_id(principal),
            obligation_id,
            statement=body.statement,
            actor=_actor(principal),
        )
    )


@router.post(
    "/arena/obligations/{obligation_id}/compensate",
    summary="Make good with simulated capacity: the restriction lifts (obligor's owner)",
)
async def arena_compensate(
    body: Compensate,
    obligation_id: str = Path(pattern=_OB),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.compensate(
            _workspace_id(principal), obligation_id, spec_id=body.spec_id, actor=_actor(principal)
        )
    )


@router.get("/arena/credit/{fid}", summary="Separate delivery counts for a workspace (no score)")
async def arena_credit(
    fid: str = Path(pattern=_FID_WS), principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    _arena()
    return await _call(arena.credit(fid))


# ── operators ─────────────────────────────────────────────────────────────


@admin_router.get("/rights/ledger/check", summary="Operator: the DEMO_M ledger balances")
async def rr_ledger_check() -> dict[str, Any]:
    _rr()
    return await rights.is_balanced()


@admin_router.get("/arena/matches", summary="Operator: matches by state")
async def arena_admin_matches(state: str | None = Query(default=None, max_length=16)) -> dict:
    _arena()
    return {"items": await arena.admin_matches(state)}


@admin_router.post("/arena/matches/{match_id}/resolve", summary="Operator: uphold or void")
async def arena_admin_resolve(
    body: MatchResolve, match_id: str = Path(pattern=_MATCH)
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.resolve(match_id, outcome=body.outcome, note=body.note, operator=body.operator)
    )


@admin_router.post(
    "/arena/obligations/{obligation_id}/decide",
    summary="Operator: confirm a default (restricts new matches), excuse, or resolve",
)
async def arena_admin_decide(
    body: ObligationDecision, obligation_id: str = Path(pattern=_OB)
) -> dict[str, Any]:
    _arena()
    return await _call(
        arena.decide(obligation_id, decision=body.decision, note=body.note, operator=body.operator)
    )


@admin_router.get("/arena/integrity", summary="Operator: pairs to review (no automatic action)")
async def arena_admin_integrity() -> dict[str, Any]:
    _arena()
    return {"items": await arena.integrity_flags()}
