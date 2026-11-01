"""Background maintenance loops for the Runtime (Phase 2J)."""

from __future__ import annotations

import asyncio
import logging

from .delegation import DelegationManager
from .store import RunStore

logger = logging.getLogger("agent_runtime.maintenance")


class ReconciliationLoop:
    def __init__(self, store: RunStore, interval_seconds: float = 30.0) -> None:
        self.store = store
        self.interval = max(1.0, interval_seconds)
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> dict[str, int]:
        return await DelegationManager(self.store).reconcile_all()

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                stats = await self.run_once()
                if stats["resumed"]:
                    logger.info("delegation reconciliation: %s", stats)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive
                logger.exception("delegation reconciliation failed")
            await asyncio.sleep(self.interval)


__all__ = ["ReconciliationLoop"]
