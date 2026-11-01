from __future__ import annotations

from pathlib import Path

from veynod_platform.store import PlatformStore


def test_store_roundtrip(tmp_path: Path) -> None:
    store = PlatformStore(tmp_path / "platform.db")
    store.init()
    store.kv_put("agentgraph_workspace_id", "ws_1")
    store.kv_put("agentgraph_workspace_key", "ag_key")
    store.kernel_workspace_put("repo.git", "kw_1")

    run = store.create_run(
        id="run_1",
        title="Implement triple",
        repo_path="repo.git",
        symbol="src/calc.py::triple",
        test_symbol="tests/test_triple.py::test_triple",
        spec="Implement triple.",
        agentgraph_workspace_id="ws_1",
        agentgraph_task_key="code-1",
        agentgraph_task_id="task_1",
        kernel_workspace_id="kw_1",
        kernel_task_id="ktask_1",
        status="DISPATCHED",
        kernel_status="READY",
    )
    assert run["id"] == "run_1"
    assert store.kv_get("agentgraph_workspace_id") == "ws_1"
    assert store.kernel_workspace_get("repo.git") == "kw_1"
    assert store.list_runs()[0]["kernel_task_id"] == "ktask_1"

    updated = store.update_run("run_1", status="SYNCED", commit_sha="abc123")
    assert updated is not None
    assert updated["status"] == "SYNCED"
    assert updated["commit_sha"] == "abc123"
    assert store.get_run("missing") is None
