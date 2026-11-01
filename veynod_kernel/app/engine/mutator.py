"""Runtime graph mutations: SPLIT, contract ESCALATE, dependency INJECTION."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.lock_algebra import LockAlgebra
from app.services.event_bus import EventBus


class SubTaskNodeSpec(BaseModel):
    """Fully resolved child node (artifact ids and canonical locks included)."""

    local_id: str
    title: str
    spec_prompt: str
    task_type: str
    read_artifact_ids: list[UUID] = Field(default_factory=list)
    target_artifact_ids: list[UUID] = Field(default_factory=list)
    test_artifact_ids: list[UUID] = Field(default_factory=list)
    write_locks: list[str] = Field(default_factory=list)
    ancestor_locks: list[str] = Field(default_factory=list)
    depends_on_local_ids: list[str] = Field(default_factory=list)


@dataclass
class SplitResult:
    parent_task_id: UUID
    child_ids: list[UUID]
    local_map: dict[str, UUID] = field(default_factory=dict)
    sink_ids: list[UUID] = field(default_factory=list)


class GraphMutator:
    def __init__(self, session: AsyncSession, event_bus: EventBus) -> None:
        self.session = session
        self.event_bus = event_bus

    # ------------------------------------------------------------------ SPLIT
    async def split_task_into_subdag(
        self,
        workspace_id: UUID,
        parent_task_id: UUID,
        sub_nodes: list[SubTaskNodeSpec],
    ) -> SplitResult:
        parent = (
            await self.session.execute(
                text(
                    """
                    SELECT task_id, title, status, write_locks, priority, base_commit_sha
                    FROM dag_tasks
                    WHERE task_id = :task_id AND workspace_id = :workspace_id
                    FOR UPDATE
                    """
                ),
                {"task_id": parent_task_id, "workspace_id": workspace_id},
            )
        ).mappings().first()
        if parent is None:
            raise ValueError("Parent task not found.")
        if parent["status"] != "LEASED":
            raise ValueError("Only an active LEASED task can request a SPLIT mutation.")

        parent_locks: list[str] = list(parent["write_locks"])
        local_map = {node.local_id: node for node in sub_nodes}
        if len(local_map) != len(sub_nodes):
            raise ValueError("Duplicate local_id in split sub-DAG.")
        if len(sub_nodes) < 2:
            raise ValueError("A split must contain at least two sub-tasks.")

        # 1. Write-set conservation and DAG validation.
        adjacency: dict[str, list[str]] = {node.local_id: [] for node in sub_nodes}
        in_degree: dict[str, int] = {node.local_id: 0 for node in sub_nodes}
        for node in sub_nodes:
            if not node.write_locks:
                raise ValueError(f"Sub-task {node.local_id!r} has an empty write set.")
            if not LockAlgebra.is_sub_write_set(parent_locks, node.write_locks):
                raise PermissionError(
                    f"Sub-task {node.local_id!r} write set {node.write_locks} exceeds "
                    f"parent write locks {parent_locks}."
                )
            for upstream in node.depends_on_local_ids:
                if upstream not in local_map:
                    raise ValueError(f"Unknown upstream local_id: {upstream}")
                if upstream == node.local_id:
                    raise ValueError(f"Self-dependency on sub-task {node.local_id!r}.")
                adjacency[upstream].append(node.local_id)
                in_degree[node.local_id] += 1

        queue = deque([key for key, degree in in_degree.items() if degree == 0])
        visited = 0
        probe = dict(in_degree)
        while queue:
            current = queue.popleft()
            visited += 1
            for nxt in adjacency[current]:
                probe[nxt] -= 1
                if probe[nxt] == 0:
                    queue.append(nxt)
        if visited != len(sub_nodes):
            raise ValueError("Sub-DAG contains a cycle; SPLIT rejected.")

        # 2. Concurrent siblings must be write-set orthogonal.
        reachable: dict[str, set[str]] = {key: set() for key in local_map}
        for key in local_map:
            stack = list(adjacency[key])
            while stack:
                nxt = stack.pop()
                if nxt in reachable[key]:
                    continue
                reachable[key].add(nxt)
                stack.extend(adjacency[nxt])

        nodes = list(sub_nodes)
        for i, left in enumerate(nodes):
            for right in nodes[i + 1 :]:
                if right.local_id in reachable[left.local_id] or left.local_id in reachable[right.local_id]:
                    continue
                if LockAlgebra.lock_set(left.write_locks).conflicts_with(
                    LockAlgebra.lock_set(right.write_locks)
                ):
                    raise ValueError(
                        f"Concurrently-runnable sub-tasks {left.local_id!r} and "
                        f"{right.local_id!r} have conflicting write locks."
                    )

        sink_local_ids = [
            key for key in local_map if not adjacency[key]
        ]
        if not sink_local_ids:
            raise ValueError("Split sub-DAG has no sink node; cyclic graph rejected.")

        # 3. Persist children.
        local_to_uuid: dict[str, UUID] = {}
        for node in sub_nodes:
            initial_status = "READY" if in_degree[node.local_id] == 0 else "BLOCKED"
            row = (
                await self.session.execute(
                    text(
                        """
                        INSERT INTO dag_tasks (
                            workspace_id, parent_task_id, title, spec_prompt, type,
                            status, priority, in_degree, read_artifact_ids,
                            target_artifact_ids, test_artifact_ids,
                            write_locks, ancestor_locks
                        ) VALUES (
                            :workspace_id, :parent_task_id, :title, :spec, CAST(:task_type AS task_type),
                            CAST(:status AS task_status), :priority, :in_degree,
                            CAST(:read_ids AS UUID[]), CAST(:target_ids AS UUID[]),
                            CAST(:test_ids AS UUID[]), CAST(:write_locks AS TEXT[]),
                            CAST(:ancestor_locks AS TEXT[])
                        )
                        RETURNING task_id
                        """
                    ),
                    {
                        "workspace_id": workspace_id,
                        "parent_task_id": parent_task_id,
                        "title": node.title[:256],
                        "spec": node.spec_prompt,
                        "task_type": node.task_type,
                        "status": initial_status,
                        "priority": int(parent["priority"]),
                        "in_degree": in_degree[node.local_id],
                        "read_ids": [str(value) for value in node.read_artifact_ids],
                        "target_ids": [str(value) for value in node.target_artifact_ids],
                        "test_ids": [str(value) for value in node.test_artifact_ids],
                        "write_locks": node.write_locks,
                        "ancestor_locks": node.ancestor_locks,
                    },
                )
            ).scalar_one()
            local_to_uuid[node.local_id] = row

        for node in sub_nodes:
            for upstream in node.depends_on_local_ids:
                await self.session.execute(
                    text(
                        "INSERT INTO task_dependencies (upstream_task_id, downstream_task_id) "
                        "VALUES (:upstream, :downstream) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "upstream": local_to_uuid[upstream],
                        "downstream": local_to_uuid[node.local_id],
                    },
                )

        sink_ids = [local_to_uuid[key] for key in sink_local_ids]

        # 4. Reconnect the parent's downstream tasks to the sinks.
        await self.session.execute(
            text(
                """
                WITH old_downstreams AS (
                    DELETE FROM task_dependencies
                    WHERE upstream_task_id = :parent_id
                    RETURNING downstream_task_id
                ),
                inserted AS (
                    INSERT INTO task_dependencies (upstream_task_id, downstream_task_id)
                    SELECT sink_id, od.downstream_task_id
                    FROM old_downstreams od
                    CROSS JOIN unnest(CAST(:sink_ids AS UUID[])) AS sink_id
                    ON CONFLICT DO NOTHING
                    RETURNING downstream_task_id
                ),
                adjusted AS (
                    UPDATE dag_tasks
                    SET in_degree = in_degree + :num_sinks - 1,
                        updated_at = NOW()
                    WHERE task_id IN (SELECT downstream_task_id FROM old_downstreams)
                    RETURNING task_id
                )
                SELECT downstream_task_id FROM inserted
                """
            ),
            {
                "parent_id": parent_task_id,
                "sink_ids": [str(value) for value in sink_ids],
                "num_sinks": len(sink_ids),
            },
        )

        # 5. Archive the parent lease.
        await self.session.execute(
            text(
                """
                UPDATE dag_tasks
                SET status = 'SPLIT_ARCHIVED',
                    lease_token_hash = NULL,
                    leased_by_runner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE task_id = :parent_id
                """
            ),
            {"parent_id": parent_task_id},
        )

        await self.event_bus.publish(
            workspace_id,
            "TASK_SPLIT",
            {
                "parent_task_id": str(parent_task_id),
                "spawned_task_ids": [str(value) for value in local_to_uuid.values()],
                "sink_task_ids": [str(value) for value in sink_ids],
            },
        )
        return SplitResult(
            parent_task_id=parent_task_id,
            child_ids=list(local_to_uuid.values()),
            local_map=local_to_uuid,
            sink_ids=sink_ids,
        )

    # ------------------------------------------------------------- ESCALATE
    async def escalate_and_propagate_blast_radius(
        self,
        workspace_id: UUID,
        reporting_task_id: UUID,
        defective_artifact_id: UUID,
        affected_fields: list[str],
        reason: str,
        proposed_signature: str,
        revise_write_locks: list[str],
        revise_ancestor_locks: list[str],
    ) -> dict[str, Any]:
        artifact = (
            await self.session.execute(
                text(
                    """
                    SELECT artifact_id, symbol_path, file_path
                    FROM code_artifacts
                    WHERE artifact_id = :artifact_id AND workspace_id = :workspace_id
                    """
                ),
                {"artifact_id": defective_artifact_id, "workspace_id": workspace_id},
            )
        ).mappings().first()
        if artifact is None:
            raise ValueError("Defective artifact not found in workspace.")

        # 1. Recursive symbol-edge blast radius.
        impacted_rows = await self.session.execute(
            text(
                """
                WITH RECURSIVE impacted AS (
                    SELECT artifact_id, 0 AS depth
                    FROM code_artifacts
                    WHERE artifact_id = :defective_id
                    UNION
                    SELECT se.caller_artifact_id, ia.depth + 1
                    FROM symbol_edges se
                    JOIN impacted ia ON se.callee_artifact_id = ia.artifact_id
                    WHERE se.workspace_id = :workspace_id
                      AND (
                            cardinality(CAST(:fields AS TEXT[])) = 0
                         OR se.referenced_fields && CAST(:fields AS TEXT[])
                      )
                      AND ia.depth < 6
                )
                SELECT DISTINCT artifact_id FROM impacted
                """
            ),
            {
                "workspace_id": workspace_id,
                "defective_id": defective_artifact_id,
                "fields": affected_fields,
            },
        )
        impacted_artifact_ids = [row[0] for row in impacted_rows.fetchall()]

        stale_rows = await self.session.execute(
            text(
                """
                UPDATE code_artifacts
                SET status = 'STALE', updated_at = NOW()
                WHERE workspace_id = :workspace_id
                  AND artifact_id = ANY(CAST(:ids AS UUID[]))
                RETURNING artifact_id
                """
            ),
            {
                "workspace_id": workspace_id,
                "ids": [str(value) for value in impacted_artifact_ids],
            },
        )
        stale_ids = [row[0] for row in stale_rows.fetchall()]

        # 2. Block every task that reads or targets an impacted artifact.
        victim_rows = (
            await self.session.execute(
                text(
                    """
                    SELECT task_id, leased_by_runner, status
                    FROM dag_tasks
                    WHERE workspace_id = :workspace_id
                      AND task_id <> :reporting_id
                      AND (
                            read_artifact_ids && CAST(:ids AS UUID[])
                         OR target_artifact_ids && CAST(:ids AS UUID[])
                         OR test_artifact_ids && CAST(:ids AS UUID[])
                      )
                      AND status IN ('READY', 'LEASED', 'BLOCKED', 'SUSPENDED_ESCALATED')
                    FOR UPDATE
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "reporting_id": reporting_task_id,
                    "ids": [str(value) for value in impacted_artifact_ids],
                },
            )
        ).mappings().all()

        victim_ids = [row["task_id"] for row in victim_rows]
        preempted = [
            {"task_id": str(row["task_id"]), "runner_id": row["leased_by_runner"]}
            for row in victim_rows
            if row["leased_by_runner"]
        ]
        if victim_ids:
            await self.session.execute(
                text(
                    """
                    UPDATE dag_tasks
                    SET status = 'BLOCKED',
                        in_degree = in_degree + 1,
                        leased_by_runner = NULL,
                        lease_token_hash = NULL,
                        lease_expires_at = NULL,
                        updated_at = NOW()
                    WHERE task_id = ANY(CAST(:ids AS UUID[]))
                    """
                ),
                {"ids": [str(value) for value in victim_ids]},
            )

        reporting = await self.session.execute(
            text(
                """
                UPDATE dag_tasks
                SET status = 'SUSPENDED_ESCALATED',
                    in_degree = in_degree + 1,
                    leased_by_runner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE task_id = :reporting_id
                  AND workspace_id = :workspace_id
                  AND status = 'LEASED'
                RETURNING task_id
                """
            ),
            {"reporting_id": reporting_task_id, "workspace_id": workspace_id},
        )
        if reporting.first() is None:
            raise ValueError("Reporting task is no longer an active LEASED task.")

        # 3. Create the high-priority contract revision node.
        revision_id = (
            await self.session.execute(
                text(
                    """
                    INSERT INTO dag_tasks (
                        workspace_id, title, spec_prompt, type, status, priority,
                        in_degree, read_artifact_ids, target_artifact_ids,
                        write_locks, ancestor_locks
                    ) VALUES (
                        :workspace_id, :title, :spec, 'REVISE_CONTRACT', 'READY', 999,
                        0, CAST(:read_ids AS UUID[]), CAST(:target_ids AS UUID[]),
                        CAST(:write_locks AS TEXT[]), CAST(:ancestor_locks AS TEXT[])
                    )
                    RETURNING task_id
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "title": f"Revise contract: {artifact['symbol_path']}"[:256],
                    "spec": (
                        f"Escalation reason:\n{reason}\n\n"
                        f"Proposed signature:\n{proposed_signature}\n\n"
                        f"Reported by task {reporting_task_id}."
                    ),
                    "read_ids": [str(defective_artifact_id)],
                    "target_ids": [str(defective_artifact_id)],
                    "write_locks": revise_write_locks,
                    "ancestor_locks": revise_ancestor_locks,
                },
            )
        ).scalar_one()

        downstream_ids = [*victim_ids, reporting_task_id]
        if downstream_ids:
            await self.session.execute(
                text(
                    """
                    INSERT INTO task_dependencies (upstream_task_id, downstream_task_id)
                    SELECT :revision_id, unnest(CAST(:down_ids AS UUID[]))
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "revision_id": revision_id,
                    "down_ids": [str(value) for value in downstream_ids],
                },
            )

        if preempted:
            await self.event_bus.publish(
                workspace_id,
                "TASK_ABORT",
                {
                    "aborted_leases": preempted,
                    "reason": "Upstream contract invalidated",
                },
            )
        await self.event_bus.publish(
            workspace_id,
            "CONTRACT_ESCALATED",
            {
                "reporting_task_id": str(reporting_task_id),
                "defective_artifact_id": str(defective_artifact_id),
                "revision_task_id": str(revision_id),
                "stale_artifact_ids": [str(value) for value in stale_ids],
                "impacted_task_ids": [str(value) for value in victim_ids],
            },
        )

        return {
            "suspended_task_id": reporting_task_id,
            "contract_revision_task_id": revision_id,
            "stale_marked_artifact_ids": stale_ids,
            "interrupted_running_task_ids": victim_ids,
        }

    # ---------------------------------------------------------------- INJECT
    async def inject_prerequisite_node(
        self,
        workspace_id: UUID,
        current_task_id: UUID,
        prerequisite_title: str,
        prerequisite_spec: str,
        prerequisite_target_ids: list[UUID],
        prerequisite_write_locks: list[str],
        prerequisite_ancestor_locks: list[str],
        prerequisite_priority: int = 500,
    ) -> UUID:
        current = (
            await self.session.execute(
                text(
                    """
                    UPDATE dag_tasks
                    SET status = 'BLOCKED',
                        in_degree = in_degree + 1,
                        leased_by_runner = NULL,
                        lease_token_hash = NULL,
                        lease_expires_at = NULL,
                        updated_at = NOW()
                    WHERE task_id = :task_id
                      AND workspace_id = :workspace_id
                      AND status = 'LEASED'
                    RETURNING task_id
                    """
                ),
                {"task_id": current_task_id, "workspace_id": workspace_id},
            )
        ).first()
        if current is None:
            raise ValueError("Current task is no longer an active LEASED task.")

        new_task_id = (
            await self.session.execute(
                text(
                    """
                    INSERT INTO dag_tasks (
                        workspace_id, title, spec_prompt, type, status, priority,
                        in_degree, target_artifact_ids, write_locks, ancestor_locks
                    ) VALUES (
                        :workspace_id, :title, :spec, 'DESIGN_CONTRACT', 'READY',
                        :priority, 0, CAST(:target_ids AS UUID[]),
                        CAST(:write_locks AS TEXT[]), CAST(:ancestor_locks AS TEXT[])
                    )
                    RETURNING task_id
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "title": prerequisite_title[:256],
                    "spec": prerequisite_spec,
                    "priority": prerequisite_priority,
                    "target_ids": [str(value) for value in prerequisite_target_ids],
                    "write_locks": prerequisite_write_locks,
                    "ancestor_locks": prerequisite_ancestor_locks,
                },
            )
        ).scalar_one()

        await self.session.execute(
            text(
                "INSERT INTO task_dependencies (upstream_task_id, downstream_task_id) "
                "VALUES (:upstream, :downstream)"
            ),
            {"upstream": new_task_id, "downstream": current_task_id},
        )
        await self.event_bus.publish(
            workspace_id,
            "TASK_INJECTED",
            {
                "current_task_id": str(current_task_id),
                "injected_task_id": str(new_task_id),
            },
        )
        return new_task_id


__all__ = ["GraphMutator", "SubTaskNodeSpec", "SplitResult"]
