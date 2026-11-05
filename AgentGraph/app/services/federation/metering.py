"""Usage metering for delegated work (F-09, docs/federation/ICD_I_BRIDGE.md §8).

Three things are kept apart and shown apart:

* **who measured** a use: ``platform`` (this Veynod measured it itself, e.g.
  the minutes a run took), ``adapter`` (a trusted integration reported it with
  a proof reference) or ``self_reported`` (an agent said so);
* **the cap mode**: ``enforce`` refuses a new run that would go past the cap;
  ``shadow`` records and warns but never blocks;
* **usage versus money**: everything here is a usage record. There is no
  balance, wallet, card or escrow (ADR-005); a receipt is not a payment.

The provider reserves before it starts a run (refused past an enforced cap),
records what each run used, and on delivery the events become the cost
receipt the requester sees.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.services.federation.errors import FederationError

UNITS = ("model_tokens", "tool_calls", "minutes")
SOURCES = ("platform", "adapter", "self_reported")
MODES = ("enforce", "shadow")
CONFIDENCE = {"platform": "high", "adapter": "medium", "self_reported": "low"}
# Least trustworthy first: a receipt is only as sure as its weakest line.
_WEAKEST = ("self_reported", "adapter", "platform")
RUN_STATES = ("ACCEPTED", "RUNNING")
MAX_AMOUNT = 10**12


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cap_of(terms: dict[str, Any] | None) -> dict[str, Any] | None:
    budget = (terms or {}).get("budget_cap")
    if not isinstance(budget, dict) or budget.get("unit") not in UNITS:
        return None
    return {**budget, "mode": budget.get("mode", "enforce")}


def _amount(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_AMOUNT:
        raise FederationError("FED_E_TERMS_INVALID", "amount is a whole number from 1")
    return value


def _unit(value: Any) -> str:
    if value not in UNITS:
        raise FederationError(
            "FED_E_TERMS_INVALID", "unit is model_tokens, tool_calls or minutes", units=list(UNITS)
        )
    return value


def _short(value: Any, limit: int, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FederationError("FED_E_TERMS_INVALID", f"{name} is text up to {limit} characters")
    return value.strip()


async def _events(platform: AsyncSession, row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in (
            await platform.execute(
                text(
                    "SELECT event_id, kind, unit, amount, metering_source, proof_ref, "
                    "price_snapshot_id, result_version, recorded_by, created_at "
                    "FROM federation_usage_events WHERE workspace_id = CAST(:w AS uuid) "
                    "AND peer_workspace_fid = :p AND federation_job_id = :j ORDER BY event_id"
                ),
                {
                    "w": str(row["workspace_id"]),
                    "p": row["peer_workspace_fid"],
                    "j": row["federation_job_id"],
                },
            )
        )
        .mappings()
        .all()
    ]


def _totals(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """unit -> source -> amount, recorded uses only."""
    out: dict[str, dict[str, int]] = {}
    for event in events:
        if event["kind"] != "record":
            continue
        per = out.setdefault(event["unit"], {})
        per[event["metering_source"]] = per.get(event["metering_source"], 0) + int(event["amount"])
    return out


async def _insert(platform: AsyncSession, row: dict[str, Any], **values: Any) -> None:
    await platform.execute(
        text(
            "INSERT INTO federation_usage_events (workspace_id, tenant_id, peer_workspace_fid, "
            "federation_job_id, kind, unit, amount, metering_source, proof_ref, "
            "price_snapshot_id, result_version, recorded_by) VALUES (CAST(:w AS uuid), '', :p, "
            ":j, :kind, :unit, :amount, :source, :proof, :price, :version, :by)"
        ),
        {
            "w": str(row["workspace_id"]),
            "p": row["peer_workspace_fid"],
            "j": row["federation_job_id"],
            "kind": values["kind"],
            "unit": values["unit"],
            "amount": values["amount"],
            "source": values.get("source"),
            "proof": values.get("proof_ref"),
            "price": values.get("price_snapshot_id"),
            "version": values.get("result_version"),
            "by": values["recorded_by"],
        },
    )


async def use(
    workspace_id: str,
    job_id: str,
    *,
    kind: str,
    unit: Any,
    amount: Any,
    actor_ref: str,
    source: Any = "self_reported",
    proof_ref: Any = None,
    price_snapshot_id: Any = None,
) -> dict[str, Any]:
    """Provider side: ``reserve`` before a run, ``record`` what a run used.

    A reservation past an enforced cap is refused: no new execution. A record
    is always kept, even past the cap, because the use already happened; it
    only stops the next reservation. ``platform`` is never accepted from a
    caller: only this server measures as the platform.
    """
    from app.services.federation import delegations

    if kind not in ("reserve", "record"):
        raise FederationError("FED_E_TERMS_INVALID", "kind is reserve or record")
    unit = _unit(unit)
    amount = _amount(amount)
    if kind == "record":
        if source not in ("adapter", "self_reported"):
            raise FederationError(
                "FED_E_TERMS_INVALID",
                "source is adapter or self_reported; only the platform measures as platform",
            )
        proof_ref = _short(proof_ref, 500, "proof_ref")
        price_snapshot_id = _short(price_snapshot_id, 200, "price_snapshot_id")
        if source == "adapter" and proof_ref is None:
            raise FederationError("FED_E_TERMS_INVALID", "an adapter reading needs a proof_ref")
        delegations.scrub({"proof_ref": proof_ref, "price": price_snapshot_id})
    async with async_session_factory() as platform:
        row = await delegations._row(platform, workspace_id, job_id, lock=True)
        if row["role"] != "provider":
            raise FederationError("FED_E_JOB_TRANSITION", "only the provider meters a delegation")
        state = delegations._state(row)
        if state not in RUN_STATES:
            raise FederationError("FED_E_JOB_TRANSITION", f"nothing runs in {state}", state=state)
        events = await _events(platform, row)
        cap = cap_of(row["terms"])
        if kind == "reserve":
            if row.get("cancel_requested_at") is not None:
                raise FederationError(
                    "FED_E_JOB_TRANSITION", "cancellation requested: start nothing new"
                )
            if cap is not None and cap["unit"] == unit and cap["mode"] == "enforce":
                spent = sum(_totals(events).get(unit, {}).values())
                if spent + amount > cap["amount"]:
                    raise FederationError(
                        "FED_E_BUDGET_EXCEEDED",
                        "this run would go past the enforced cap: not started",
                        unit=unit,
                        cap=cap["amount"],
                        spent=spent,
                        asked=amount,
                    )
        await _insert(
            platform,
            row,
            kind=kind,
            unit=unit,
            amount=amount,
            source=source if kind == "record" else None,
            proof_ref=proof_ref if kind == "record" else None,
            price_snapshot_id=price_snapshot_id if kind == "record" else None,
            recorded_by=actor_ref,
        )
        await platform.commit()
        row = await delegations._row(platform, workspace_id, job_id)
        view = await summary(platform, row)
    return {"federation_job_id": job_id, "kind": kind, "unit": unit, "amount": amount, **view}


async def _run_started(platform: AsyncSession, row: dict[str, Any]) -> datetime | None:
    return await platform.scalar(
        text(
            "SELECT max(created_at) FROM federation_delegation_history WHERE workspace_id = "
            "CAST(:w AS uuid) AND peer_workspace_fid = :p AND federation_job_id = :j "
            "AND action IN ('accept', 'revise')"
        ),
        {
            "w": str(row["workspace_id"]),
            "p": row["peer_workspace_fid"],
            "j": row["federation_job_id"],
        },
    )


async def receipt_for_delivery(
    platform: AsyncSession, row: dict[str, Any], offered: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The cost receipt sent with a result, built from what was metered.

    The platform adds the minutes this run took, measured by itself. Uses
    recorded since the last delivery are summed per unit with a per-source
    breakdown. A receipt an agent typed in is kept only as self-reported:
    it cannot claim to be measured by the platform.
    """
    started = await _run_started(platform, row)
    if started is not None:
        minutes = max(1, math.ceil((_now() - started).total_seconds() / 60))
        await _insert(
            platform,
            row,
            kind="record",
            unit="minutes",
            amount=minutes,
            source="platform",
            recorded_by="system",
        )
    if offered:
        # Usage an agent typed into its receipt counts against the cap, but
        # only ever as self-reported: it cannot claim to be measured.
        for unit, value in (offered.get("usage") or {}).items():
            if unit in UNITS and isinstance(value, int) and not isinstance(value, bool):
                if 1 <= value <= MAX_AMOUNT:
                    await _insert(
                        platform,
                        row,
                        kind="record",
                        unit=unit,
                        amount=value,
                        source="self_reported",
                        proof_ref=None,
                        recorded_by="agent receipt",
                    )
    events = await _events(platform, row)
    run = [e for e in events if e["kind"] == "record" and e["result_version"] is None]
    if not run:
        return None
    totals = _totals(run)
    usage: dict[str, Any] = {unit: sum(per.values()) for unit, per in totals.items()}
    usage["by_source"] = totals
    usage["evidence"] = [
        {
            "unit": e["unit"],
            "amount": int(e["amount"]),
            "source": e["metering_source"],
            **({"proof_ref": e["proof_ref"]} if e["proof_ref"] else {}),
            **({"price_snapshot_id": e["price_snapshot_id"]} if e["price_snapshot_id"] else {}),
        }
        for e in run
    ][:50]
    sources = {s for per in totals.values() for s in per}
    weakest = next((s for s in _WEAKEST if s in sources), "platform")
    proofs = [e["proof_ref"] for e in run if e["proof_ref"]]
    prices = [e["price_snapshot_id"] for e in run if e["price_snapshot_id"]]
    return {
        "metering_source": weakest,
        "confidence": CONFIDENCE[weakest],
        "usage": usage,
        "price_snapshot_id": prices[0] if prices else None,
        "proof_ref": proofs[0] if proofs else None,
    }


async def close_run(platform: AsyncSession, row: dict[str, Any], version: int) -> None:
    """Mark this run's records as delivered with ``version``."""
    await platform.execute(
        text(
            "UPDATE federation_usage_events SET result_version = :v WHERE workspace_id = "
            "CAST(:w AS uuid) AND peer_workspace_fid = :p AND federation_job_id = :j "
            "AND result_version IS NULL"
        ),
        {
            "v": version,
            "w": str(row["workspace_id"]),
            "p": row["peer_workspace_fid"],
            "j": row["federation_job_id"],
        },
    )


async def _receipts(platform: AsyncSession, row: dict[str, Any]) -> list[dict[str, Any]]:
    rows = (
        (
            await platform.execute(
                text(
                    "SELECT result_version, metering_source, confidence, usage, "
                    "price_snapshot_id, proof_ref, disputed, created_at FROM "
                    "federation_cost_receipts WHERE workspace_id = CAST(:w AS uuid) AND "
                    "peer_workspace_fid = :p AND federation_job_id = :j ORDER BY result_version"
                ),
                {
                    "w": str(row["workspace_id"]),
                    "p": row["peer_workspace_fid"],
                    "j": row["federation_job_id"],
                },
            )
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        item = dict(r)
        if isinstance(item["usage"], str):
            item["usage"] = json.loads(item["usage"])
        item["created_at"] = item["created_at"].isoformat()
        out.append(item)
    return out


async def summary(platform: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
    """What was used, who measured it, and the cap: the same shape on both sides.

    The provider sums its own usage events; the requester sums the receipts
    the provider delivered. Measured (platform, adapter) and self-reported
    amounts are never added into one unlabelled number.
    """
    cap = cap_of(row["terms"])
    if row["role"] == "provider":
        events = await _events(platform, row)
        totals = _totals(events)
        reservations = sum(1 for e in events if e["kind"] == "reserve")
        evidence = [
            {
                "kind": e["kind"],
                "unit": e["unit"],
                "amount": int(e["amount"]),
                "source": e["metering_source"],
                "proof_ref": e["proof_ref"],
                "price_snapshot_id": e["price_snapshot_id"],
                "delivered_with": e["result_version"],
                "at": e["created_at"].isoformat(),
            }
            for e in events[-50:]
        ]
        receipts: list[dict[str, Any]] = []
    else:
        receipts = await _receipts(platform, row)
        totals = {}
        for receipt in receipts:
            by_source = (receipt["usage"] or {}).get("by_source")
            if isinstance(by_source, dict):
                for unit, per in by_source.items():
                    if unit in UNITS and isinstance(per, dict):
                        for src, n in per.items():
                            if src in SOURCES and isinstance(n, int):
                                slot = totals.setdefault(unit, {})
                                slot[src] = slot.get(src, 0) + n
            else:  # an older receipt: one source for all of it
                for unit in UNITS:
                    n = (receipt["usage"] or {}).get(unit)
                    if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                        slot = totals.setdefault(unit, {})
                        src = receipt["metering_source"]
                        slot[src] = slot.get(src, 0) + n
        reservations = 0
        evidence = []
    used = {
        unit: {
            "total": sum(per.values()),
            "measured": sum(n for s, n in per.items() if s != "self_reported"),
            "self_reported": per.get("self_reported", 0),
            "by_source": per,
        }
        for unit, per in totals.items()
    }
    budget = None
    if cap is not None:
        spent = used.get(cap["unit"], {}).get("total", 0)
        over = spent >= cap["amount"]
        budget = {
            "unit": cap["unit"],
            "cap": cap["amount"],
            "mode": cap["mode"],
            "spent": spent,
            "remaining": max(cap["amount"] - spent, 0),
            "over": over,
            # shadow never blocks; it says so instead.
            "blocking": over and cap["mode"] == "enforce",
            "warning": (
                "over the cap (shadow mode: recorded, not blocked)"
                if over and cap["mode"] == "shadow"
                else None
            ),
        }
    return {
        "budget": budget,
        "usage": used,
        "reservations": reservations,
        "evidence": evidence,
        "receipts": receipts,
        "not_money": True,
    }
