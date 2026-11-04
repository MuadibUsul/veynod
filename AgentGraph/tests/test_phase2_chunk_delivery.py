"""Phase 2 acceptance: a chunk is delivered by the Kernel and the graph follows.

The chain under test is the one the audit found missing entirely:

    freeze -> dispatch binds a Kernel node -> lease -> patch -> 5-stage gate ->
    Kernel commit -> chunk merged with the commit sha -> downstream unblocked

The Kernel, its gate worker and its git merge are real; only the API client is
ours. A replayed commit event and a reconciliation pass must neither merge again
nor unlock twice.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.conftest import run_server
from tests.test_diagrams import _agent, _chunk, _workspace

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "veynod_kernel"
REPO = "acme/kernel-chunk"
ADMIN_KEY = "chunk-admin-key-0123456789abcdef"
SERVICE_KEY = "chunk-service-key-0123456789ab"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _db_url(name: str) -> str:
    base = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0]
    return f"{base}/{name}"


def _plain(name: str | None = None) -> str:
    """A plain DSN; the default is the product test database itself."""
    if name is None:
        return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    return _db_url(name).replace("postgresql+asyncpg://", "postgresql://", 1)


async def _pg(sql: str, database: str = "postgres"):
    import asyncpg

    conn = await asyncpg.connect(_plain(database))
    try:
        return await conn.execute(sql)
    finally:
        await conn.close()


def _log_tail(log: Path | None, lines: int = 40) -> str:
    if log is None or not log.exists():
        return ""
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return f"\n--- {log} (last {len(tail)} lines) ---\n" + "\n".join(tail)


async def _wait_url(url: str, timeout: float = 120, *, process=None,
                    log: Path | None = None) -> None:
    """Wait for ``url`` to answer 200; fail at once if ``process`` exits first.

    A Kernel that cannot start (a missing dependency, say) exits within a
    second; waiting out the timeout would only hide why.
    """
    import httpx

    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2) as probe:
        while True:
            if process is not None and process.returncode is not None:
                raise AssertionError(
                    f"exited with {process.returncode} before {url} answered"
                    + _log_tail(log))
            try:
                if (await probe.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"did not start: {url}" + _log_tail(log))
            await asyncio.sleep(0.25)


async def _kernel_ready(base_url: str, timeout: float = 90, *, process=None,
                        log: Path | None = None) -> None:
    await _wait_url(f"{base_url}/healthz", timeout, process=process, log=log)


@pytest.fixture(scope="module")
async def kernel():
    """A real Kernel, its gate worker, and a product server wired to both.

    The runner token has to exist before the product process starts because the
    product reads it once at boot (``KERNEL_RUNTIME_TOKEN``), so Kernel first,
    then the product.
    """
    if not (KERNEL / "app").exists():  # pragma: no cover - layout guard
        pytest.skip("veynod_kernel not present")
    kernel_db = f"chunk_kernel_{uuid.uuid4().hex[:8]}"
    await _pg(f"CREATE DATABASE {kernel_db}")
    repo = Path(os.environ.get("TEMP", "/tmp")) / f"chunk-repo-{uuid.uuid4().hex[:8]}.git"
    source = Path(os.environ.get("TEMP", "/tmp")) / f"chunk-src-{uuid.uuid4().hex[:8]}"
    source.mkdir(parents=True)
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "t@example.test"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(command, cwd=source, check=True, capture_output=True)
    (source / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests_dir = source / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_main.py").write_text(
        "from main import VALUE\n\n\ndef test_value():\n    assert VALUE >= 1\n",
        encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=source, check=True, capture_output=True)
    # The gate advances the branch with update-ref, which needs a bare mirror.
    subprocess.run(["git", "clone", "--bare", str(source), str(repo)],
                   check=True, capture_output=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    # The Kernel has its own dependencies (tree-sitter, pyright); the product's
    # venv does not hold them, so both Kernel processes run on the host python.
    kernel_python = os.environ.get("KERNEL_PYTHON") or "python"
    common = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "DATABASE_URL": _db_url(kernel_db),
        "VN_AUTO_INIT_DB": "true",
        "VN_AUTH_MODE": "dev",
        "VN_ADMIN_API_KEY": ADMIN_KEY,
        "VN_RUNTIME_SERVICE_KEY": SERVICE_KEY,
        "VN_REPO_ROOT": str(repo),
    }
    # The Kernel's output goes to a log so a failed start says why
    # (``KERNEL_PYTHON`` without the Kernel's requirements, for example).
    api_log = Path(os.environ.get("TEMP", "/tmp")) / f"chunk-kernel-{port}.log"
    api = await asyncio.create_subprocess_exec(
        kernel_python, "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(port), cwd=str(KERNEL), env=common,
        stdout=api_log.open("w"), stderr=subprocess.STDOUT)
    gate = None
    try:
        await _kernel_ready(base_url, process=api, log=api_log)
        import httpx

        async with httpx.AsyncClient(base_url=base_url, timeout=60,
                                     headers={"X-Admin-Key": ADMIN_KEY}) as admin:
            made = await admin.post("/v1/admin/workspaces", json={
                "name": f"chunk-{uuid.uuid4().hex[:6]}",
                "repo_path": str(repo), "main_branch": "main"})
            assert made.status_code in (200, 201), made.text
            kernel_workspace = made.json().get("workspace_id") or made.json().get("id")
            runner = await admin.post("/v1/admin/tokens", json={
                "label": "phase2-runner", "workspace_id": kernel_workspace})
            assert runner.status_code in (200, 201), runner.text
            runner_token = runner.json()["token"]
        assert kernel_workspace

        # The gate is its own process in production; a submit only answers 202
        # without it and nothing would ever commit.
        gate_log = Path(os.environ.get("TEMP", "/tmp")) / "chunk-gate.log"
        gate = await asyncio.create_subprocess_exec(
            kernel_python, "-m", "app.worker",
            "--workers", "1", "--database-url", _db_url(kernel_db),
            "--metrics-port", str(_free_port()), "--poll-seconds", "0.5",
            cwd=str(KERNEL), env={**common, "LOG_LEVEL": "DEBUG"},
            stdout=gate_log.open("w"), stderr=subprocess.STDOUT)

        # kernel_delivery reads the runner token from the process environment,
        # so the test process sets it for the child it starts.
        os.environ["KERNEL_RUNTIME_TOKEN"] = runner_token
        os.environ["KERNEL_ADMIN_KEY"] = ADMIN_KEY
        async with run_server({
            "KERNEL_SERVICE_KEY": SERVICE_KEY,
            # The event poller reads the Kernel endpoint from settings, not from
            # the binding, so point the whole process at this test Kernel.
            "KERNEL_URL": base_url,
            "KERNEL_ADMIN_KEY": ADMIN_KEY,
            "CODE_PROJECTION_EVENT_INTERVAL_SECONDS": "0.5",
        }, ".server-phase2.log") as product_url:
            async with httpx.AsyncClient(base_url=product_url, timeout=90) as product:
                yield {"base_url": base_url, "database": kernel_db, "repo": repo,
                       "workspace": kernel_workspace, "runner_token": runner_token,
                       "gate": gate, "client": product}
    finally:
        for process in (gate, api):
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)
        await _pg(f"DROP DATABASE IF EXISTS {kernel_db} WITH (FORCE)")


async def _bind_kernel(client, ws: dict, kernel: dict) -> None:
    """Register the repository with its Kernel workspace (product side).

    The repository route upserts the binding too, which is the path a person
    takes from the console.
    """
    project = (await client.post("/v1/projects", headers=ws["key"], json={
        "name": f"chunk-{uuid.uuid4().hex[:6]}", "repo": REPO})).json()
    project_name = project.get("name") or project.get("project")
    added = await client.post(f"/v1/projects/{project_name}/repositories", headers=ws["key"], json={
        "repo_path": REPO, "default_branch": "main",
        "kernel_base_url": kernel["base_url"],
        "kernel_workspace_id": kernel["workspace"]})
    assert added.status_code in (200, 201), added.text


async def _diagram_and_freeze(client, ws: dict, chunks: list[dict]) -> tuple[str, str]:
    architect = await _agent(client, ws, "ANALYST")
    # A diagram only becomes Kernel work when it names a repository; the chunk
    # task inherits it from the diagram.
    key = f"dg-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/diagrams", headers=architect["headers"], json={
        "diagram_key": key, "title": "Payments", "namespace": "default",
        "repo": REPO, "chunks": chunks,
        "self_check": {"big": True, "stable": True, "machine_checkable": True, "long": True}})
    assert made.status_code == 201, made.text
    assert (await client.post(f"/v1/diagrams/{key}/confirm", headers=ws["person"])
            ).status_code == 200
    assert (await client.post(f"/v1/diagrams/{key}/freeze", headers=ws["person"], json={})
            ).status_code == 200
    chunk_key = chunks[0]["chunk_key"]
    dispatched = await client.post(f"/v1/diagrams/{key}/chunks/{chunk_key}/dispatch",
                                   headers=architect["headers"])
    assert dispatched.status_code == 200, dispatched.text
    return key, chunk_key


async def _chunk_row(client, ws: dict, key: str, chunk_key: str) -> dict:
    view = (await client.get(f"/v1/diagrams/{key}", headers=ws["key"])).json()
    return next(item for item in view["chunks"] if item["chunk_key"] == chunk_key)


async def _deliver_through_kernel(kernel: dict, bound: dict) -> str:
    """Lease, patch, commit and submit in the Kernel; returns the commit sha."""
    import httpx

    auth = {"Authorization": f"Bearer {kernel['runner_token']}"}
    async with httpx.AsyncClient(base_url=kernel["base_url"], timeout=90,
                                 headers=auth) as client:
        pull = await client.post("/v1/syscalls/pull_task_bundle", json={
            "task_id": bound["kernel_task_id"], "worker_hub_id": "veynod-runtime",
            "supported_task_types": ["IMPLEMENT_BODY"], "max_context_tokens": 4000,
        })
        assert pull.status_code == 200, pull.text
        bundle = pull.json()
        assert bundle.get("leased"), bundle
        worktree = Path(bundle["worktree_path"])
        (worktree / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-m", "chunk"], cwd=worktree,
                       check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree,
                             check=True, capture_output=True, text=True).stdout.strip()
        diff = subprocess.run(["git", "diff", bundle["base_commit_sha"], sha], cwd=worktree,
                              check=True, capture_output=True, text=True).stdout
        # The one submit path: v2 answers 202 and the gate worker process
        # runs the five gates and the merge; poll the submission to its end.
        sent = await client.post(
            "/v2/syscalls/submit_patch",
            headers={"Idempotency-Key": f"chunk-{uuid.uuid4().hex}"},
            json={"task_id": bound["kernel_task_id"], "lease_token": bundle["lease_token"],
                  "git_diff": diff, "touched_symbols": ["main.py"],
                  "local_test_passed": True})
        assert sent.status_code in (200, 202), sent.text
        body = sent.json()
        for _ in range(240):
            if body.get("final"):
                break
            await asyncio.sleep(0.5)
            body = (await client.get(f"/v2/submissions/{body['submission_id']}")).json()
        assert body.get("final") and body.get("status") == "COMMITTED", body
        assert body.get("result_commit_sha"), body
        return str(body["result_commit_sha"])


async def test_a_chunk_is_delivered_by_the_kernel_and_only_then_does_the_graph_move(
        kernel) -> None:
    client = kernel["client"]
    ws = await _workspace(client)
    await _bind_kernel(client, ws, kernel)
    key, chunk_key = await _diagram_and_freeze(
        client, ws, [{**_chunk("c1", layer="interface"),
                      "allowed_write_paths": ["main.py"],
                      "commands": {"test": "python -c \"import main\""}}])

    bound = await _chunk_row(client, ws, key, chunk_key)
    assert bound["kernel_task_id"], bound

    sha = await _deliver_through_kernel(kernel, {**bound, "chunk_key": chunk_key})

    for _ in range(180):
        row = await _chunk_row(client, ws, key, chunk_key)
        if row["state"] == "merged":
            break
        await asyncio.sleep(0.5)
    if row["state"] != "merged":
        import asyncpg

        conn = await asyncpg.connect(_plain(kernel["database"]))
        try:
            await conn.fetchrow(
                "SELECT status, result_commit_sha FROM dag_tasks WHERE task_id = $1::uuid",
                bound["kernel_task_id"])
        finally:
            await conn.close()
        assert row["state"] == "merged", row
    assert row["merged_commit_sha"] == sha
    # The bare mirror has no working tree: read the branch through git.
    merged = subprocess.run(["git", "show", "main:main.py"], cwd=kernel["repo"],
                            check=True, capture_output=True, text=True).stdout
    assert merged == "VALUE = 2\n"


async def test_a_replayed_commit_unlocks_downstream_once(kernel) -> None:
    client = kernel["client"]
    ws = await _workspace(client)
    await _bind_kernel(client, ws, kernel)
    key, chunk_key = await _diagram_and_freeze(client, ws, [
        {**_chunk("up", layer="interface"), "allowed_write_paths": ["main.py"]},
        {**_chunk("down"), "depends_on": ["up"], "allowed_write_paths": ["down.py"]},
    ])
    bound = await _chunk_row(client, ws, key, chunk_key)

    from app.database import async_session_factory
    from app.services import diagrams

    dedupe = f"kernel:{bound['kernel_task_id']}:{'a' * 40}"
    async with async_session_factory() as session:
        first = await diagrams.project_kernel_commit(
            session, bound["id"], "a" * 40, dedupe_key=dedupe, task_status="COMMITTED")
        await session.commit()
        replay = await diagrams.project_kernel_commit(
            session, bound["id"], "a" * 40, dedupe_key=dedupe, task_status="COMMITTED")
        await session.commit()

    assert first["projected"] is True
    # A replay is a no-op either because the dedupe row already holds it or
    # because the chunk is already merged at that sha.
    assert replay["projected"] is False
    assert replay["reason"] in ("duplicate_event", "already_merged")
    chain = (await client.get(f"/v1/diagrams/{key}/replay", headers=ws["key"])).json()
    types = [event["type"] for event in chain["events"]]
    assert types.count("timeline.chunk_merged") == 1, types


async def test_the_reconciler_projects_a_lost_commit_event(kernel) -> None:
    client = kernel["client"]
    ws = await _workspace(client)
    await _bind_kernel(client, ws, kernel)
    key, chunk_key = await _diagram_and_freeze(
        client, ws, [{**_chunk("lost", layer="interface"),
                      "allowed_write_paths": ["main.py"]}])
    bound = await _chunk_row(client, ws, key, chunk_key)

    # A Kernel that committed with no event delivered, and a stale chunk age.
    import asyncpg

    conn = await asyncpg.connect(_plain(kernel["database"]))
    try:
        await conn.execute(
            "UPDATE dag_tasks SET status = 'COMMITTED', result_commit_sha = $1 "
            "WHERE task_id = $2::uuid", "b" * 40, bound["kernel_task_id"])
    finally:
        await conn.close()
    conn = await asyncpg.connect(_plain())
    try:
        await conn.execute("UPDATE diagram_chunks SET updated_at = NOW() - INTERVAL '1 hour' "
                           "WHERE id = $1", bound["id"])
    finally:
        await conn.close()

    from app.database import async_session_factory
    from app.services import kernel_delivery
    from app.services.task_lifecycle import kernel_binding_for

    # The reconciler's own read, so a failure below is not the SQL's fault.
    async with async_session_factory() as probe_session:
        # Ask for the repository that was bound: a binding never applies to another one.
        binding = await kernel_binding_for(probe_session, ws["id"], None, REPO)
        assert binding, "no kernel binding"
        plane = kernel_delivery.plane_for(binding, admin_key=ADMIN_KEY)
        try:
            seen = await plane.task(bound["kernel_task_id"])
        finally:
            await plane.aclose()
    assert seen and seen["status"] == "COMMITTED", seen

    from app.services.diagram_reconcile import reconcile_once

    # Two servers share the test database; the other one's reconciler may look
    # at this chunk first with a Kernel URL it cannot reach. Retry until this
    # process projects it, which is the behaviour a real deployment relies on.
    for _ in range(8):
        stats = await reconcile_once(grace_seconds=0, workspace_id=ws["id"],
                                    admin_key=ADMIN_KEY)
        if stats["projected"]:
            break
        await asyncio.sleep(1)
    assert stats["projected"] >= 1, stats
    row = await _chunk_row(client, ws, key, chunk_key)
    assert row["state"] == "merged" and row["merged_commit_sha"] == "b" * 40
