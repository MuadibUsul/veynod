"""Runtime worker: claim one RUNNING run under a fenced lease and execute a step."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from .context import ContextBuilder
from .errors import LeaseLost
from .loop import LoopController, ModelGateway, ToolRuntime
from .quota import QuotaPolicy
from .store import RunStore

logger = logging.getLogger("agent_runtime.worker")


class RuntimeWorker:
    def __init__(
        self,
        store: RunStore,
        model: ModelGateway,
        tools: ToolRuntime,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        poll_seconds: float = 1.0,
        quota: QuotaPolicy | None = None,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.worker_id = worker_id or f"runtime-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_seconds = max(10, lease_seconds)
        self.poll_seconds = max(0.2, poll_seconds)
        self.quota = quota
        self.context_builder = context_builder
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> dict[str, Any] | None:
        claim = await self.store.claim_run(
            self.worker_id, lease_seconds=self.lease_seconds
        )
        if claim is None:
            return None
        run = claim["run"]
        run_id = run["run_id"]
        token = claim["lease_token"]
        heartbeat = asyncio.create_task(
            self._heartbeat(run_id, token), name=f"runtime-heartbeat-{run_id}"
        )
        controller = LoopController(
            self.store,
            self.model,
            self.tools,
            guard=lambda: self.store.assert_lease(run_id, token),
            quota=self.quota,
            context_builder=self.context_builder,
        )
        try:
            return await controller.step(run_id)
        except LeaseLost:
            logger.warning(
                "run %s lease lost before step completed", run_id, exc_info=True
            )
            return {"run": await self.store.get_run(run_id), "lease_lost": True}
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            try:
                await self.store.release_lease(run_id, token)
            except Exception:  # noqa: BLE001 - lease cleanup must not hide the step result
                logger.exception("could not release runtime lease for %s", run_id)

    async def _heartbeat(self, run_id: str, token: str) -> None:
        interval = max(2.0, self.lease_seconds / 3)
        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                await self.store.renew_lease(
                    run_id, token, lease_seconds=self.lease_seconds
                )
            except LeaseLost:
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retry while the lease is valid
                logger.exception("runtime lease renewal failed for %s", run_id)

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                result = await self.run_once()
                if result is None:
                    await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.exception("runtime worker step failed")
                await asyncio.sleep(self.poll_seconds)


__all__ = ["RuntimeWorker"]
