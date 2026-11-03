"""Phase 3/7 product surface: inbox, projects, execution graph, usage."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.database import async_session_factory
from app.services import event_dispatch

pytestmark = pytest.mark.integration


async def test_projects_execution_and_limits(client, workspace, admin_headers):
    headers = workspace["headers"]
    name = f"proj-{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/v1/projects",
        headers=headers,
        json={"name": name, "repo": "demo/repo", "description": "phase 7"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == name

    projects = await client.get("/v1/projects", headers=headers)
    assert projects.status_code == 200, projects.text
    assert any(item["name"] == name for item in projects.json())

    task = await client.post(
        "/v1/tasks",
        headers=headers,
        json={"title": "project task", "project": name},
    )
    assert task.status_code == 201, task.text
    graph = await client.get(
        f"/v1/execution-graph?project={name}", headers=headers
    )
    assert graph.status_code == 200, graph.text
    assert any(node["task_key"] == task.json()["task_key"] for node in graph.json()["nodes"])

    deliveries = await client.get("/v1/deliveries", headers=headers)
    assert deliveries.status_code == 200, deliveries.text
    assert deliveries.json() == []

    usage = await client.get("/v1/usage/runtime", headers=headers)
    assert usage.status_code == 200, usage.text
    assert usage.json()["runs"] == 0

    limit = await client.put(
        "/v1/tenant/runtime-limit",
        headers=headers,
        json={"max_model_calls": 10, "max_cost_usd": 1.5},
    )
    assert limit.status_code == 200, limit.text
    assert float(limit.json()["max_model_calls"]) == 10
    current = await client.get("/v1/tenant/runtime-limit", headers=headers)
    assert current.status_code == 200
    assert float(current.json()["max_cost_usd"]) == 1.5


async def test_event_inbox_admin_surface(client, admin_headers):
    stats = await client.get("/v1/admin/event-inbox/stats", headers=admin_headers)
    assert stats.status_code == 200, stats.text
    assert "by_source_status" in stats.json()
    listed = await client.get("/v1/admin/event-inbox", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    dispatch = await client.post(
        "/v1/admin/event-inbox/dispatch", headers=admin_headers
    )
    assert dispatch.status_code == 200, dispatch.text
    assert "claimed" in dispatch.json()
    missing = await client.post(
        "/v1/admin/event-inbox/999999999/replay", headers=admin_headers
    )
    assert missing.status_code == 404


async def _insert_event(source_seq: int) -> int:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO event_inbox
                        (source, source_instance, source_seq, event_type, payload)
                    VALUES ('kernel', 'test-workspace', :seq, 'TASK_READY',
                            CAST(:payload AS JSONB))
                    RETURNING inbox_id
                    """
                ),
                {
                    "seq": source_seq,
                    "payload": '{"task_id": "task-1"}',
                },
            )
        ).scalar_one()
        await session.commit()
    return int(row)


async def _event_status(inbox_id: int) -> tuple[str, int]:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, attempts FROM event_inbox WHERE inbox_id = :id"
                ),
                {"id": inbox_id},
            )
        ).mappings().one()
    return row["status"], int(row["attempts"])


async def test_event_inbox_retry_dead_letter_and_replay(monkeypatch):
    async with async_session_factory() as session:
        await session.execute(text("DELETE FROM event_inbox"))
        await session.commit()
    calls: list[str] = []

    async def ok(workspace, task_id):
        calls.append(task_id)
        return {"synced": 1}

    async def fail(workspace, task_id):
        raise RuntimeError("projection exploded")

    seq = uuid.uuid4().int % 1_000_000_000
    inbox_id = await _insert_event(seq)
    monkeypatch.setattr(event_dispatch.code_engine, "sync_kernel_task_event", ok)
    result = await event_dispatch.dispatch_once(limit=10)
    assert result["processed"] >= 1, result
    assert await _event_status(inbox_id) == ("PROCESSED", 0)
    assert "task-1" in calls

    failed_id = await _insert_event(seq + 1)
    monkeypatch.setattr(event_dispatch.code_engine, "sync_kernel_task_event", fail)
    for expected in range(1, 6):
        async with async_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE event_inbox SET next_attempt_at = NOW() "
                    "WHERE inbox_id = :id"
                ),
                {"id": failed_id},
            )
            await session.commit()
        # The server's own dispatch loop shares this table and may get there
        # first, so wait for the attempt count rather than demanding exactly.
        for _ in range(20):
            await event_dispatch.dispatch_once(limit=10)
            status, attempts = await _event_status(failed_id)
            if attempts >= expected:
                break
            await asyncio.sleep(0.1)
        assert attempts >= expected, (status, attempts)
    assert status == "DEAD", status
    replayed = await event_dispatch.replay(failed_id)
    assert replayed is not None and replayed["status"] == "PENDING"
    monkeypatch.setattr(event_dispatch.code_engine, "sync_kernel_task_event", ok)
    await event_dispatch.dispatch_once(limit=10)
    assert await _event_status(failed_id) == ("PROCESSED", 0)


async def test_runtime_usage_metering_is_idempotent(client, workspace):
    from sqlalchemy import select

    from app.models import CollabTask
    from app.services import agent_runtime as runtime_service

    headers = workspace["headers"]
    task = await client.post(
        "/v1/tasks", headers=headers, json={"title": "metered task"}
    )
    assert task.status_code == 201, task.text
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(CollabTask).where(
                    CollabTask.task_key == task.json()["task_key"]
                )
            )
        ).scalar_one()
        run_id = str(uuid.uuid4())
        recorded = await runtime_service.meter_runtime_run(
            session,
            workspace_id=row.workspace_id,
            run={
                "run_id": run_id,
                "status": "COMPLETED",
                "usage": {
                    "model_calls": 1,
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cost_usd": 0.002,
                },
            },
            tenant_id="tenant-test",
        )
        assert recorded is True
        again = await runtime_service.meter_runtime_run(
            session,
            workspace_id=row.workspace_id,
            run={
                "run_id": run_id,
                "status": "COMPLETED",
                "usage": {"model_calls": 1, "prompt_tokens": 100},
            },
            tenant_id="tenant-test",
        )
        assert again is False
    usage = await client.get("/v1/usage/runtime", headers=headers)
    assert usage.status_code == 200, usage.text
    assert usage.json()["runs"] == 1
    assert usage.json()["prompt_tokens"] == 100
