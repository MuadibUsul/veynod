"""Parent/child Agent Run delegation (Phase 2G).

The parent moves to WAITING_CHILDREN while child runs execute.  Collecting the
children is explicit: the parent only resumes to RUNNING when every child is
terminal.  This is intentionally not a hidden in-process future.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import RunConflict, RunNotFound
from .states import TERMINAL
from .store import RunStore

logger = logging.getLogger("agent_runtime.delegation")

_CHILD_START = ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING")


class DelegationManager:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    async def delegate(
        self,
        parent_run_id: str,
        *,
        product_task_id: str | None = None,
        budget: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        parent = await self.store.get_run(parent_run_id)
        if idempotency_key:
            existing = await self.store.get_child_by_delegation(
                parent_run_id, idempotency_key
            )
            if existing is not None:
                return {
                    "parent": parent,
                    "child": existing,
                    "children": await self.store.list_children(parent_run_id),
                    "idempotent": True,
                }
        if parent["status"] != "RUNNING":
            raise RunConflict(
                f"parent run {parent_run_id} is {parent['status']}, not RUNNING"
            )
        child = await self.store.create_run(
            tenant_id=parent["tenant_id"],
            workspace_id=parent["workspace_id"],
            product_task_id=product_task_id or parent.get("product_task_id"),
            workflow_id=parent.get("workflow_id"),
            workflow_version=parent.get("workflow_version"),
            budget=budget or {},
            correlation_id=parent.get("correlation_id"),
            idempotency_key=(
                f"delegate:{parent_run_id}:{idempotency_key}"
                if idempotency_key
                else None
            ),
            parent_run_id=parent_run_id,
            delegation_key=idempotency_key,
        )
        child_run = child
        for target in _CHILD_START:
            await self.store.transition(
                child_run["run_id"],
                target,
                expected_version=child_run["version"],
                reason=f"delegated by {parent_run_id}",
                actor="delegation",
            )
            child_run = await self.store.get_run(child_run["run_id"])

        parent = await self.store.get_run(parent_run_id)
        if parent["status"] != "RUNNING":
            raise RunConflict(
                f"parent run {parent_run_id} changed to {parent['status']} "
                "while delegating"
            )
        await self.store.transition(
            parent_run_id,
            "WAITING_CHILDREN",
            expected_version=parent["version"],
            reason=f"delegated child {child_run['run_id']}",
            actor="delegation",
        )
        return {
            "parent": await self.store.get_run(parent_run_id),
            "child": child_run,
            "children": await self.store.list_children(parent_run_id),
        }

    async def reconcile(self, parent_run_id: str) -> dict[str, Any]:
        """Resume a WAITING_CHILDREN parent that can no longer make progress."""
        parent = await self.store.get_run(parent_run_id)
        if parent["status"] != "WAITING_CHILDREN":
            return {"ready": False, "parent": parent, "children": []}
        children = await self.store.list_children(parent_run_id)
        if not children:
            await self.store.transition(
                parent_run_id,
                "RUNNING",
                expected_version=parent["version"],
                reason="delegation had no children",
                actor="delegation",
            )
            return {
                "ready": True,
                "parent": await self.store.get_run(parent_run_id),
                "children": [],
            }
        return await self.collect(parent_run_id)

    async def reconcile_all(self) -> dict[str, int]:
        parents = await self.store.list_runs_by_status("WAITING_CHILDREN")
        stats = {"parents": len(parents), "resumed": 0, "waiting": 0}
        for parent in parents:
            result = await self.reconcile(parent["run_id"])
            if result["ready"]:
                stats["resumed"] += 1
            else:
                stats["waiting"] += 1
        return stats

    async def collect(self, parent_run_id: str) -> dict[str, Any]:
        parent = await self.store.get_run(parent_run_id)
        children = await self.store.list_children(parent_run_id)
        if parent["status"] != "WAITING_CHILDREN":
            return {"ready": False, "parent": parent, "children": children}
        if any(child["status"] not in {state.value for state in TERMINAL} for child in children):
            return {"ready": False, "parent": parent, "children": children}
        await self.store.transition(
            parent_run_id,
            "RUNNING",
            expected_version=parent["version"],
            reason="all child runs reached a terminal state",
            actor="delegation",
        )
        return {
            "ready": True,
            "parent": await self.store.get_run(parent_run_id),
            "children": await self.store.list_children(parent_run_id),
        }


__all__ = ["DelegationManager"]
