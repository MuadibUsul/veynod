"""Runtime Worker process entry point (Phase 2E).

Configure external endpoints through environment variables; this process does
not contain model provider credentials in code.

    RUNTIME_DATABASE_URL=postgresql+asyncpg://...
    RUNTIME_MODEL_URL=https://provider.example/v1
    RUNTIME_MODEL_API_KEY=...
    RUNTIME_MODEL_NAME=...
    RUNTIME_TOOL_URL=https://tools.example
    RUNTIME_TOOL_API_KEY=...
    RUNTIME_TOOL_ALLOWLIST=read_file,write_file,run_tests
    RUNTIME_WORKER_LEASE_SECONDS=60
    RUNTIME_WORKER_POLL_SECONDS=1

Run:
    python -m agent_runtime.worker_main
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal

from .context import ContextBuilder
from .gateway import (
    HTTPModelGateway,
    HTTPToolRuntime,
    ModelGatewayConfig,
    ToolGatewayConfig,
)
from .maintenance import ReconciliationLoop
from .pool import RuntimeWorkerPool
from .quota import QuotaPolicy
from .store import RunStore

logger = logging.getLogger("agent_runtime.worker_main")


async def _metrics_server(port: int, pool: RuntimeWorkerPool) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.read(4096)
            request_line = raw.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            path = request_line.split()[1] if len(request_line.split()) > 1 else "/"
            if path == "/metrics":
                body = json.dumps(pool.snapshot(), default=str).encode("utf-8")
                status = "200 OK"
            else:
                body = b'{"error":"not found"}'
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

    return await asyncio.start_server(handle, "127.0.0.1", port)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


async def main() -> int:
    store = RunStore(_required("RUNTIME_DATABASE_URL"))
    await store.init_schema()

    model = HTTPModelGateway(
        ModelGatewayConfig(
            base_url=_required("RUNTIME_MODEL_URL"),
            api_key=_required("RUNTIME_MODEL_API_KEY"),
            model=_required("RUNTIME_MODEL_NAME"),
            timeout_seconds=float(os.environ.get("RUNTIME_MODEL_TIMEOUT_SECONDS", "30")),
            max_retries=int(os.environ.get("RUNTIME_MODEL_MAX_RETRIES", "2")),
        )
    )
    allowlist = frozenset(
        item.strip()
        for item in os.environ.get("RUNTIME_TOOL_ALLOWLIST", "").split(",")
        if item.strip()
    )
    if not allowlist:
        raise SystemExit("RUNTIME_TOOL_ALLOWLIST must list at least one tool")
    tools = HTTPToolRuntime(
        ToolGatewayConfig(
            base_url=_required("RUNTIME_TOOL_URL"),
            api_key=os.environ.get("RUNTIME_TOOL_API_KEY", ""),
            allowed_tools=allowlist,
            timeout_seconds=float(os.environ.get("RUNTIME_TOOL_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.environ.get("RUNTIME_TOOL_MAX_RETRIES", "1")),
        )
    )
    worker = RuntimeWorkerPool(
        store,
        model,
        tools,
        workers=int(os.environ.get("RUNTIME_WORKER_COUNT", "2")),
        lease_seconds=int(os.environ.get("RUNTIME_WORKER_LEASE_SECONDS", "60")),
        poll_seconds=float(os.environ.get("RUNTIME_WORKER_POLL_SECONDS", "1")),
        context_builder=ContextBuilder(
            token_budget=int(os.environ.get("RUNTIME_CONTEXT_TOKEN_BUDGET", "8000"))
        ),
        quota=QuotaPolicy.from_env(),
    )
    stop = asyncio.Event()

    def _stop(_signum, _frame) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            pass

    logger.info(
        "runtime worker pool starting: workers=%s lease=%ss tools=%s",
        worker.worker_count,
        worker.lease_seconds,
        sorted(allowlist),
    )
    await worker.start()
    metrics_server = await _metrics_server(
        int(os.environ.get("RUNTIME_WORKER_METRICS_PORT", "8031")),
        worker,
    )
    maintenance = ReconciliationLoop(
        store,
        interval_seconds=float(
            os.environ.get("RUNTIME_RECONCILE_INTERVAL_SECONDS", "30")
        ),
    )
    maintenance_task = asyncio.create_task(
        maintenance.run_forever(), name="runtime-delegation-reconcile"
    )
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        maintenance.stop()
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
        await worker.stop()
        metrics_server.close()
        await metrics_server.wait_closed()
        await model.close()
        await tools.close()
        await store.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(main()))
