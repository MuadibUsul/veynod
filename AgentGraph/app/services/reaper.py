"""Background upkeep: leases, timeouts, retention and the other periodic steps.

Each step runs on its own. They used to share one ``try``: a step that raised
skipped every step after it, round after round, and nothing said which one was
missed. Now a failing step is logged by name and the others still run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete

from app.config import get_settings
from app.database import async_session_factory
from app.models import WorkspaceEvent
from app.services.common import db_now

logger = logging.getLogger("agentgraph.reaper")
settings = get_settings()


async def _leases() -> None:
    from app.services.messaging import reap_message_escalations
    from app.services.slots import reap_expired_slot_leases
    from app.services.tasks import reap_expired_task_leases, reap_task_timeouts

    async with async_session_factory() as session:
        tasks = await reap_expired_task_leases(session)
        slots = await reap_expired_slot_leases(session)
        timed_out = await reap_task_timeouts(session)
        escalated = await reap_message_escalations(session)
        await session.commit()
    if timed_out or escalated:
        logger.info("timeout upkeep: %s task(s) degraded, %s message(s) escalated",
                    timed_out, escalated)
    if tasks or slots:
        logger.info("lease reaper reclaimed %s task(s) and %s slot(s)", tasks, slots)


async def _old_events() -> None:
    async with async_session_factory() as session:
        cutoff = await db_now(session) - timedelta(days=settings.event_retention_days)
        deleted = await session.execute(
            delete(WorkspaceEvent).where(WorkspaceEvent.created_at < cutoff))
        await session.commit()
    if deleted.rowcount:
        logger.info("pruned %s old workspace event(s)", deleted.rowcount)


async def _idempotency() -> None:
    from app.services.idempotency import purge_expired

    await purge_expired()


async def _directory() -> None:
    from app.services.directory import sweep_offline

    await sweep_offline()


async def _rights() -> None:
    from app.services.rights import upkeep

    await upkeep()


async def _escalations() -> None:
    from app.services.collab import escalation_upkeep

    async with async_session_factory() as session:
        await escalation_upkeep(session)


async def _provenance() -> None:
    from app.services.provenance import prune_reads

    await prune_reads()


async def _resources() -> None:
    from app.services.resources import reap

    async with async_session_factory() as session:
        await reap(session)


async def _exports() -> None:
    from app.services.export_schedule import run_due

    await run_due()


async def _retrieval_probe() -> None:
    if settings.retrieval_probe_enabled:
        from app.services.retrieval_eval import probe_due

        await probe_due()


async def _logs() -> None:
    from app.services.logs import rebuild_due

    await rebuild_due()


async def _delegated_tokens() -> None:
    from app.services.token_tree import purge_stale_delegated

    stale = await purge_stale_delegated()
    if stale:
        logger.info("purged %s long-expired delegated token(s)", stale)


async def _connections() -> None:
    from app.services.connections import upkeep

    await upkeep()


async def _presence() -> None:
    from app.services.pokes import sweep_presence

    await sweep_presence()


async def _taint() -> None:
    from app.services.taint import retighten_internal_taint

    untainted = await retighten_internal_taint()
    if untainted:
        logger.info("cleared taint from %s document(s) that only cited internal drafts",
                    untainted)


async def _tenants() -> None:
    from app.services.tenants import purge_due_tenants

    async with async_session_factory() as session:
        purged = await purge_due_tenants(session)
        await session.commit()
    if purged:
        logger.info("purged %s tenant(s) after the deletion grace period", purged)


async def _state_violations() -> None:
    """Undeclared state moves are kept 90 days (app/state_machines.py)."""
    from sqlalchemy import text

    async with async_session_factory() as session:
        await session.execute(text(
            "DELETE FROM state_transition_violations WHERE at < NOW() - INTERVAL '90 days'"))
        await session.commit()


STEPS: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
    ("leases", _leases),
    ("old_events", _old_events),
    ("idempotency", _idempotency),
    ("directory", _directory),
    ("rights", _rights),
    ("escalations", _escalations),
    ("provenance", _provenance),
    ("resources", _resources),
    ("scheduled_exports", _exports),
    ("retrieval_probe", _retrieval_probe),
    ("logs", _logs),
    ("delegated_tokens", _delegated_tokens),
    ("connections", _connections),
    ("presence", _presence),
    ("taint", _taint),
    ("tenants", _tenants),
    ("state_violations", _state_violations),
)
#: The last error of each step that failed in its latest round.
last_errors: dict[str, str] = {}
#: Per step in this process: last success, failures in a row, since when, last error.
health: dict[str, dict[str, Any]] = {}
#: Failed rounds in a row before the operator is told (one bad round is noise).
ALERT_AFTER = 3


async def _tell_operator(name: str, state: dict[str, Any]) -> None:
    """One platform alert per outage of a step (GET /v1/admin/alerts, and mail
    when PLATFORM_ADMIN_EMAIL is set). It never stops the round."""
    from app.services import alerts

    since = state["failing_since"]
    try:
        await alerts.raise_alert(
            "upkeep_step_failing",
            f"Upkeep step {name} failed {state['failures']} rounds in a row: "
            f"{state['last_error']}"[:500],
            severity="warning",
            details={"step": name, "failing_since": since.isoformat(),
                     "failures": state["failures"], "error": state["last_error"],
                     "see": "/v1/admin/upkeep"},
            # One outage, one alert: the key is when this outage began.
            dedupe_key=f"upkeep:{name}:{since.isoformat()}")
    except Exception:  # noqa: BLE001 - the database may be what is failing
        logger.exception("could not raise the upkeep alert for %s", name)


async def run_once() -> list[str]:
    """One round of every step; the names of the steps that failed."""
    failed = []
    for name, step in STEPS:
        state = health.setdefault(name, {"last_ok_at": None, "failing_since": None,
                                         "failures": 0, "last_error": None})
        try:
            await step()
            last_errors.pop(name, None)
            state.update(last_ok_at=datetime.now(timezone.utc), failing_since=None,
                         failures=0, last_error=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one step must not stop the others
            failed.append(name)
            last_errors[name] = f"{type(exc).__name__}: {exc}"[:300]
            state["failures"] += 1
            state["failing_since"] = state["failing_since"] or datetime.now(timezone.utc)
            state["last_error"] = last_errors[name]
            logger.exception("upkeep step %s failed", name)
            if state["failures"] == ALERT_AFTER:
                await _tell_operator(name, state)
    return failed


def status() -> dict[str, Any]:
    """Every step's health in this process (each server process runs its own)."""
    def out(value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value

    steps = []
    for name, _ in STEPS:
        state = health.get(name) or {"last_ok_at": None, "failing_since": None,
                                     "failures": 0, "last_error": None}
        steps.append({"step": name, "ok": state["failures"] == 0,
                      **{key: out(value) for key, value in state.items()}})
    return {"ok": all(item["ok"] for item in steps), "steps": steps,
            "interval_s": settings.lease_reaper_interval_seconds,
            "alert_after_failures": ALERT_AFTER}


async def lease_reaper_loop() -> None:
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("lease reaper iteration failed")
        await asyncio.sleep(settings.lease_reaper_interval_seconds)
