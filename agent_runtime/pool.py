"""Supervised pool of RuntimeWorkers (Phase 2H).

The pool owns N worker loops.  A worker that exits unexpectedly is restarted;
the pool's stop() stops and awaits every loop.  It intentionally does not
spawn processes yet; deployment still needs a process supervisor/K8s.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from .context import ContextBuilder
from .loop import ModelGateway, ToolRuntime
from .quota import QuotaPolicy
from .store import RunStore
from .worker import RuntimeWorker

logger = logging.getLogger("agent_runtime.pool")


class RuntimeWorkerPool:
    def __init__(
        self,
        store: RunStore,
        model: ModelGateway,
        tools: ToolRuntime,
        *,
        workers: int = 2,
        lease_seconds: int = 60,
        poll_seconds: float = 1.0,
        context_builder: ContextBuilder | None = None,
        quota: QuotaPolicy | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.worker_count = max(1, workers)
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.context_builder = context_builder
        self.quota = quota
        self._tasks: dict[int, asyncio.Task] = {}
        self._workers: dict[int, RuntimeWorker] = {}
        self._stopping = False

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        for index in range(self.worker_count):
            self._tasks[index] = asyncio.create_task(
                self._supervise(index), name=f"runtime-worker-{index}"
            )

    async def _supervise(self, index: int) -> None:
        while not self._stopping:
            worker = RuntimeWorker(
                self.store,
                self.model,
                self.tools,
                worker_id=f"runtime-{index}-{uuid.uuid4().hex[:6]}",
                lease_seconds=self.lease_seconds,
                poll_seconds=self.poll_seconds,
                quota=self.quota,
                context_builder=self.context_builder,
            )
            self._workers[index] = worker
            try:
                await worker.run_forever()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - restart a crashed worker loop
                logger.exception("runtime worker %s crashed; restarting", index)
                await asyncio.sleep(self.poll_seconds)
            finally:
                worker.stop()

    async def stop(self) -> None:
        self._stopping = True
        for worker in self._workers.values():
            worker.stop()
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("runtime worker pool task failed on stop")

    def snapshot(self) -> dict[str, Any]:
        return {
            "workers": self.worker_count,
            "started": len(self._tasks),
            "ids": [worker.worker_id for worker in self._workers.values()],
            "stopping": self._stopping,
        }


__all__ = ["RuntimeWorkerPool"]
