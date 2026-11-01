"""Standalone deterministic-gate worker process.

Run the API with no gate workers; run this process with database credentials:

    python -m app.worker --workers 8 \
      --database-url postgresql+asyncpg://...

It claims durable patch submissions under a renewable work lease.  Agent code
is executed by app.services.gate_sandbox (Docker recommended in production).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal

from app.config import settings
from app.database import dispose_database, get_engine, init_db
from app.services.edge_rebuild import edge_rebuild_service
from app.services.gate_worker import GateWorker
from app.services.merge_coordinator import MergeCoordinator
from app.services.outbox import outbox_dispatcher
from app.services.runtime_metrics import get_runtime_metrics
from app.services.worktree_pool import worktree_pool_manager

logger = logging.getLogger("veynod.worker")


async def _metrics_server(host: str, port: int) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.read(8192)
            request_line = raw.split(b"\r\n", 1)[0].decode(
                "latin-1", errors="replace"
            )
            parts = request_line.split()
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"
            if path == "/metrics":
                body = json.dumps(
                    get_runtime_metrics().snapshot(
                        get_engine().sync_engine.pool
                    ),
                    default=str,
                ).encode("utf-8")
                status = "200 OK"
            elif path == "/metrics/reset" and method == "POST":
                get_runtime_metrics().reset()
                body = b'{"reset": true}'
                status = "200 OK"
            else:
                body = b'{"error": "not found"}'
                status = "404 Not Found"
            headers = (
                f"HTTP/1.1 {status}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(headers.encode("ascii") + body)
            await writer.drain()
        except Exception:  # noqa: BLE001 - metrics must not kill the worker
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)


async def run(args: argparse.Namespace) -> int:
    if args.database_url:
        settings.database_url = args.database_url
    if args.work_lease_seconds is not None:
        settings.work_lease_seconds = args.work_lease_seconds
    if args.max_attempts is not None:
        settings.work_max_attempts = args.max_attempts
    if args.poll_seconds is not None:
        settings.worker_poll_seconds = args.poll_seconds
    if args.sandbox:
        settings.gate_sandbox = args.sandbox
    if args.sandbox_image:
        settings.gate_sandbox_image = args.sandbox_image

    from app.security import validate_worker_production_settings

    validate_worker_production_settings(settings)
    await init_db()
    metrics_server = await _metrics_server("127.0.0.1", args.metrics_port)
    await worktree_pool_manager.warmup()
    if edge_rebuild_service.enabled:
        await edge_rebuild_service.start()
    await outbox_dispatcher.start()

    stop = asyncio.Event()

    def _signal(_signum, _frame) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _signal)
        except (ValueError, OSError):
            pass

    coordinator = MergeCoordinator(
        coordinator_id=f"merge-{args.worker_id or 'pool'}-{os.getpid()}"
    )
    coordinator_task = asyncio.create_task(coordinator.run_forever())
    workers = [
        GateWorker(worker_id=f"gate-{args.worker_id or 'pool'}-{index}")
        for index in range(max(1, args.workers))
    ]
    tasks = [asyncio.create_task(worker.run_forever()) for worker in workers]
    logger.info(
        "gate workers started: count=%s lease=%ss max_attempts=%s sandbox=%s",
        len(workers),
        settings.work_lease_seconds,
        settings.work_max_attempts,
        settings.gate_sandbox,
    )
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for worker in workers:
            worker.stop()
        coordinator.stop()
        coordinator_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(coordinator_task, *tasks, return_exceptions=True)
        metrics_server.close()
        await metrics_server.wait_closed()
        await worktree_pool_manager.close_all()
        await edge_rebuild_service.stop()
        await outbox_dispatcher.stop()
        await dispose_database()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Veynod deterministic gate worker")
    parser.add_argument("--workers", type=int, default=settings.submission_worker_concurrency)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--work-lease-seconds", type=int, default=None)
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--sandbox", choices=("local", "docker"), default=None)
    parser.add_argument("--sandbox-image", default=None)
    parser.add_argument("--metrics-port", type=int, default=8030)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
