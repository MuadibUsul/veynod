"""Event routing across multiple Kernel instances (one cursor per instance)."""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from agent_runtime.store import RunStore
from agent_runtime.wakeups import KernelEventWatcher, parse_service_keys

URL = os.environ.get("RUNTIME_TEST_DATABASE_URL") or os.environ.get(
    "TEST_DATABASE_URL"
)
pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def store():
    if not URL:
        pytest.skip("RUNTIME_TEST_DATABASE_URL/TEST_DATABASE_URL is not set")
    name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
    if "test" not in name and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("runtime tests require a disposable database")
    runtime = RunStore(URL)
    await runtime.init_schema(reset=True)
    try:
        yield runtime
    finally:
        await runtime.close()


async def _running_bound(store: RunStore, base_url: str, workspace_id: str) -> dict:
    run = await store.create_run(
        tenant_id=str(uuid4()),
        workspace_id=str(uuid4()),
        product_task_id=f"task-{workspace_id[:6]}",
        kernel={"base_url": base_url, "workspace_id": workspace_id},
    )
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        await store.transition(run["run_id"], target, expected_version=run["version"])
        run = await store.get_run(run["run_id"])
    return run


def _multi_kernel(events: dict[str, list[dict]], calls: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        base = f"{request.url.scheme}://{request.url.host}"
        key = request.headers.get("x-veynod-service-key")
        calls.append({"base": base, "key": key, "path": request.url.path})
        expected = {"http://kernel-a.test": "key-a", "http://kernel-b.test": "key-b"}
        if expected.get(base) != key:
            return httpx.Response(401, json={"detail": "bad service key"})
        after = int(request.url.params.get("after_seq") or 0)
        fresh = [item for item in events.get(base, []) if item["event_seq"] > after]
        return httpx.Response(
            200,
            json={
                "events": fresh,
                "next_after_seq": fresh[-1]["event_seq"] if fresh else after,
            },
        )

    return handler


@pytest.mark.asyncio
async def test_each_kernel_instance_has_its_own_cursor_and_key(store: RunStore) -> None:
    ws_a, ws_b = str(uuid4()), str(uuid4())
    sub_a, sub_b = str(uuid4()), str(uuid4())
    await _running_bound(store, "http://kernel-a.test", ws_a)
    await _running_bound(store, "http://kernel-b.test", ws_b)

    events = {
        "http://kernel-a.test": [
            {
                "event_seq": 3,
                "event_type": "SUBMISSION_COMMITTED",
                "payload": {"submission_id": sub_a, "status": "COMMITTED"},
            }
        ],
        "http://kernel-b.test": [
            {
                "event_seq": 9,
                "event_type": "SUBMISSION_FAILED",
                "payload": {"submission_id": sub_b, "status": "FAILED"},
            }
        ],
    }
    calls: list[dict] = []
    watcher = KernelEventWatcher(
        store,
        None,
        service_keys={
            "http://kernel-a.test": "key-a",
            "http://kernel-b.test": "key-b",
        },
        transport=httpx.MockTransport(_multi_kernel(events, calls)),
    )
    first = await watcher.poll_once()
    assert first["wakeups"] == 2

    # Each instance carries its own key and its own cursor.
    assert {
        (call["base"], call["key"]) for call in calls
    } == {
        ("http://kernel-a.test", "key-a"),
        ("http://kernel-b.test", "key-b"),
    }
    cursor_a = await store.wakeup_cursor(f"kernel:http://kernel-a.test:{ws_a}")
    cursor_b = await store.wakeup_cursor(f"kernel:http://kernel-b.test:{ws_b}")
    assert (cursor_a, cursor_b) == (3, 9)

    # A second poll has nothing new; the wakeups are not duplicated.
    calls.clear()
    second = await watcher.poll_once()
    assert second["wakeups"] == 0
    async with store._sessions() as session:  # noqa: SLF001 - assertion only
        from sqlalchemy import text

        total = await session.scalar(
            text(f"SELECT count(*) FROM {store.schema}.runtime_wakeups")
        )
    assert int(total) == 2
    await watcher.close()


@pytest.mark.asyncio
async def test_an_instance_without_credentials_is_skipped_not_misrouted(
    store: RunStore,
) -> None:
    ws_a, ws_b = str(uuid4()), str(uuid4())
    await _running_bound(store, "http://kernel-a.test", ws_a)
    await _running_bound(store, "http://kernel-b.test", ws_b)
    events = {
        "http://kernel-a.test": [
            {
                "event_seq": 1,
                "event_type": "SUBMISSION_COMMITTED",
                "payload": {"submission_id": str(uuid4()), "status": "COMMITTED"},
            }
        ],
        "http://kernel-b.test": [
            {
                "event_seq": 1,
                "event_type": "SUBMISSION_COMMITTED",
                "payload": {"submission_id": str(uuid4()), "status": "COMMITTED"},
            }
        ],
    }
    calls: list[dict] = []
    watcher = KernelEventWatcher(
        store,
        None,
        service_keys={"http://kernel-a.test": "key-a"},
        transport=httpx.MockTransport(_multi_kernel(events, calls)),
    )
    stats = await watcher.poll_once()
    assert stats["wakeups"] == 1
    # The second instance was never called with instance A's credential.
    assert [call["base"] for call in calls] == ["http://kernel-a.test"]
    await watcher.close()


def test_service_key_map_parsing() -> None:
    assert parse_service_keys(None) == {}
    assert parse_service_keys("not json") == {}
    assert parse_service_keys('["x"]') == {}
    assert parse_service_keys(
        '{"http://k:8001/": "a", "http://b:8001": "b"}'
    ) == {"http://k:8001": "a", "http://b:8001": "b"}
