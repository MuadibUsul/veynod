"""In-memory long-poll hold queue for runner claims (Phase 8.2).

A throttled claim must not pin a PostgreSQL connection while it waits for a
gate slot. The scheduler closes its session, parks on a per-workspace
Condition, and retries after a signal or a short slice. EventHub wakes the
queue when GATE_PASSED/TASK_READY arrives, so a commit wakes the same
process's waiters without a DB poll per waiter.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from uuid import UUID

_WAKE_EVENTS = frozenset(
    {
        "GATE_PASSED",
        "TASK_READY",
        "TASK_RELEASED",
        "TASK_REAPED",
        "SUBMISSION_COMMITTED",
        "SUBMISSION_FAILED",
    }
)


class ClaimHoldQueue:
    def __init__(self, max_waiters_per_workspace: int = 1200) -> None:
        self._max_waiters = max_waiters_per_workspace
        self._conditions: dict[UUID, asyncio.Condition] = defaultdict(
            asyncio.Condition
        )
        self._waiter_counts: dict[UUID, int] = defaultdict(int)
        self._generation: dict[UUID, int] = defaultdict(int)

    async def wait_for_opportunity(
        self, workspace_id: UUID, timeout: float
    ) -> bool:
        if timeout <= 0:
            return False
        if self._waiter_counts[workspace_id] >= self._max_waiters:
            await asyncio.sleep(min(timeout, 2.0))
            return False
        condition = self._conditions[workspace_id]
        async with condition:
            start = self._generation[workspace_id]
            self._waiter_counts[workspace_id] += 1
            try:
                await asyncio.wait_for(
                    condition.wait_for(
                        lambda: self._generation[workspace_id] != start
                    ),
                    timeout=timeout,
                )
                return True
            except asyncio.TimeoutError:
                return False
            finally:
                self._waiter_counts[workspace_id] -= 1

    async def notify_available(self, workspace_id: UUID, n: int = 4) -> None:
        condition = self._conditions.get(workspace_id)
        if condition is None:
            return
        async with condition:
            self._generation[workspace_id] += 1
            condition.notify(max(1, n))

    def event_wakes(self, event_type: str) -> bool:
        return event_type in _WAKE_EVENTS


GLOBAL_CLAIM_HOLD_QUEUE = ClaimHoldQueue()

__all__ = ["ClaimHoldQueue", "GLOBAL_CLAIM_HOLD_QUEUE"]
