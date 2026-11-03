"""Separate process for the polling loops (ADR-019).

``ENABLE_INLINE_BACKGROUND_LOOPS=0`` strips the sweepers from the API process
so request and nerve-socket latency stop competing with them for the shared
pool. This module is the other half: the same loops, in a process that serves
nothing.

The nerve reaper stays in the API process — it closes sockets that process
owns — together with the presence sweep, because that process is what holds the
presence rows.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.config import get_settings
from app.database import apply_schema

logger = logging.getLogger("agentgraph.worker")
settings = get_settings()


async def main() -> None:
    if settings.auto_init_db:
        await apply_schema()
    from app.services.access_log import flush as flush_access_log
    from app.services.access_log import flush_loop as access_log_loop
    from app.services.backups import backup_scheduler_loop
    from app.services.blobs import blob_gc_loop
    from app.services.incidents import incident_check_loop
    from app.services.mailer import mail_worker_loop
    from app.services.presence import presence_loop
    from app.services.scheduler import scheduler_loop
    from app.services.situation import evaluation_loop as situation_evaluation_loop
    from app.services.situation import situation_loop
    from app.services.token_budget import flush as flush_token_usage
    from app.services.token_budget import flush_loop as token_usage_loop

    tasks = [
        asyncio.create_task(access_log_loop(), name="worker-access-log"),
        asyncio.create_task(token_usage_loop(), name="worker-token-usage"),
        asyncio.create_task(situation_loop(), name="worker-metrics"),
        asyncio.create_task(situation_evaluation_loop(), name="worker-situation"),
        asyncio.create_task(mail_worker_loop(), name="worker-mailer"),
        asyncio.create_task(incident_check_loop(), name="worker-incident-sla"),
        asyncio.create_task(blob_gc_loop(), name="worker-blob-gc"),
        asyncio.create_task(presence_loop(), name="worker-presence"),
        asyncio.create_task(scheduler_loop(), name="worker-scheduler"),
    ]
    if settings.code_projection_enabled:
        from app.services.code_engine import code_projection_loop

        tasks.append(asyncio.create_task(code_projection_loop(), name="worker-code-projection"))
    if settings.event_dispatch_enabled:
        from app.services.event_dispatch import event_dispatch_loop

        tasks.append(asyncio.create_task(event_dispatch_loop(), name="worker-event-dispatch"))
    if settings.agent_runtime_projection_enabled:
        from app.services.agent_runtime import agent_run_projection_loop

        tasks.append(asyncio.create_task(
            agent_run_projection_loop(), name="worker-runtime-projection"))
    if settings.backup_dir and settings.backup_scheduler_enabled:
        tasks.append(asyncio.create_task(backup_scheduler_loop(), name="worker-backups"))
    if settings.diagram_reconcile_enabled:
        from app.services.diagram_reconcile import reconcile_loop

        tasks.append(asyncio.create_task(
            reconcile_loop(settings.diagram_reconcile_interval_seconds),
            name="worker-diagram-reconcile"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop.set())

    logger.info("agentgraph-worker up with %s loop(s)", len(tasks))
    await stop.wait()
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    for flush in (flush_access_log, flush_token_usage):
        try:
            await flush()
        except Exception:  # noqa: BLE001 - shutdown best effort
            pass
    logger.info("agentgraph-worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=settings.log_level.upper())
    asyncio.run(main())
