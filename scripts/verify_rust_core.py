"""Prove the Rust core is built, loaded and used on the real execution path.

    python scripts/verify_rust_core.py --image veynod-kernel:rust

1. Builds a fixture repository on the host and mounts it into the container.
2. Starts the Kernel API and the Gate Worker from the image.
3. Asserts /healthz reports core.available=true (the PyO3 module loaded).
4. Runs create task -> pull bundle -> submit patch -> gate -> commit -> evidence
   through the container: the native path, not a compile-only claim.
5. Compares engine selection and microbenchmarks against the Python fallback
   (VEYNOD_DISABLE_RUST=1) inside the same image.

Requires Docker and the test PostgreSQL on 127.0.0.1:55433.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from e2e_unified import free_port  # noqa: E402

ADMIN_KEY = "rust-check-admin-key-0123456789abcdef0123"
SERVICE_KEY = "rust-check-service-key-0123456789abcdef01"
DB = "veynod_rust_check"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def docker(*args: str, check: bool = True) -> str:
    result = run(["docker", *args])
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}: {result.stderr[-800:]}")
    return result.stdout


def http(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{method} {url} -> {exc.code}: {exc.read().decode('utf-8', 'replace')[:800]}"
        ) from exc


def wait_for(url: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            http("GET", url, timeout=3)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not become healthy")


def build_repo(root: Path) -> Path:
    work = root / "work"
    (work / "src").mkdir(parents=True)
    (work / "tests").mkdir(parents=True)
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work / "src" / "calc.py").write_text(
        '"""Calc."""\n\n\ndef add(a: int, b: int) -> int:\n'
        '    """Add."""\n    ...\n',
        encoding="utf-8",
    )
    (work / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (work / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add() -> None:\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    def git(*args: str) -> None:
        result = run(["git", *args], cwd=work)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)

    git("init", "-b", "main")
    git("config", "user.email", "rust@veynod.test")
    git("config", "user.name", "Rust Check")
    git("add", "-A")
    git("commit", "-m", "fixture")
    bare = root / "suite.git"
    result = run(["git", "clone", "--bare", str(work), str(bare)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return bare


def ensure_database() -> None:
    import asyncio

    import asyncpg

    async def main() -> None:
        conn = await asyncpg.connect(
            "postgresql://agentgraph:agentgraph@127.0.0.1:55433/postgres"
        )
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", DB
            )
            if exists:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()",
                    DB,
                )
                await conn.execute(f'DROP DATABASE "{DB}"')
            await conn.execute(f'CREATE DATABASE "{DB}"')
        finally:
            await conn.close()

    asyncio.run(main())


def microbench(image: str, disable_rust: bool) -> dict:
    code = r"""
import json, time
from app.engine import rust_bridge
from app.engine.lock_algebra import LockAlgebra
paths = [f"src/mod{i}.py::Class{i}::method{i}" for i in range(200)]
pairs = [(f"src/a{i}.py::f{i}", f"src/a{i}.py::f{i}:x") for i in range(200)]
start = time.perf_counter()
for _ in range(200):
    for p in paths:
        LockAlgebra.normalize(p)
normalize_ms = (time.perf_counter() - start) * 1000
start = time.perf_counter()
for _ in range(200):
    for a, b in pairs:
        LockAlgebra.conflicts(a, b)
conflicts_ms = (time.perf_counter() - start) * 1000
print(json.dumps({
    "available": rust_bridge.available(),
    "engine": rust_bridge.engine_name(),
    "normalize_ms": round(normalize_ms, 2),
    "conflicts_ms": round(conflicts_ms, 2),
    "normalized_sample": LockAlgebra.normalize("src/x.py::A.method"),
}))
"""
    env = ["-e", "VEYNOD_DISABLE_RUST=1"] if disable_rust else []
    output = docker("run", "--rm", *env, image, "python", "-c", code)
    return json.loads(output.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="veynod-kernel:rust")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    size = docker("images", "--format", "{{.Size}}", args.image).strip()
    micro_rust = microbench(args.image, disable_rust=False)
    micro_python = microbench(args.image, disable_rust=True)
    if not micro_rust["available"]:
        print("FAIL: the image reports the Rust core unavailable", file=sys.stderr)
        return 1

    ensure_database()
    temp = Path(tempfile.mkdtemp(prefix="veynod-rust-"))
    repo = build_repo(temp)
    port = free_port(8121)
    name = f"veynod-rust-check-{uuid.uuid4().hex[:6]}"
    gate = f"{name}-gate"
    dsn_host = (
        "postgresql+asyncpg://agentgraph:agentgraph@host.docker.internal:55433/"
        + DB
    )
    common = [
        "-e", f"DATABASE_URL={dsn_host}",
        "-e", "VN_AUTO_INIT_DB=true",
        "-e", "VN_AUTH_MODE=prod",
        "-e", f"VN_ADMIN_API_KEY={ADMIN_KEY}",
        "-e", f"VN_RUNTIME_SERVICE_KEY={SERVICE_KEY}",
        "-e", "VN_GATE_SANDBOX=local",
        "-e", "VN_ALLOW_LOCAL_GATE_IN_PROD=1",
        "-v", f"{repo}:/repos/suite.git",
    ]
    try:
        docker("run", "-d", "--name", name, "-p", f"{port}:8001", *common,
               args.image)
        docker("run", "-d", "--name", gate, *common, args.image,
               "python", "-m", "app.worker", "--workers", "2",
               "--database-url", dsn_host)
        base = f"http://127.0.0.1:{port}"
        wait_for(base + "/healthz")
        health = http("GET", base + "/healthz")
        core = health.get("core") or {}
        assert core.get("available") is True, health

        admin = {"X-Admin-Key": ADMIN_KEY}
        workspace = http(
            "POST", base + "/v1/admin/workspaces", headers=admin,
            body={"name": "rust", "repo_path": "/repos/suite.git",
                  "main_branch": "main"},
        )
        workspace_id = workspace["workspace_id"]
        for symbol, kind in (("src/calc.py::add", "stub"),
                             ("tests/test_calc.py::test_add", "test")):
            http("POST", base + "/v1/admin/artifacts", headers=admin, body={
                "workspace_id": workspace_id, "symbol_path": symbol,
                "kind": kind, "status": "FROZEN",
            })
        task = http("POST", base + "/v1/admin/tasks", headers=admin, body={
            "workspace_id": workspace_id, "title": "Implement add",
            "spec_prompt": "Implement add(a,b) so the frozen test passes.",
            "type": "IMPLEMENT_BODY",
            "target_symbols": ["src/calc.py::add"],
            "test_symbols": ["tests/test_calc.py::test_add"],
            "write_set_symbols": ["src/calc.py::add"],
        })
        token = http("POST", base + "/v1/admin/tokens", headers=admin,
                     body={"label": "rust", "workspace_id": workspace_id})["token"]
        auth = {"Authorization": f"Bearer {token}"}
        bundle = http("POST", base + "/v1/syscalls/pull_task_bundle", headers=auth,
                      body={"worker_hub_id": "rust-check",
                            "supported_task_types": ["IMPLEMENT_BODY"]})
        assert bundle["leased"] is True, bundle

        diff = (
            "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,6 +1,6 @@\n"
            ' """Calc."""\n \n \n def add(a: int, b: int) -> int:\n'
            '     """Add."""\n-    ...\n+    return a + b\n'
        )
        accepted = http(
            "POST", base + "/v2/syscalls/submit_patch", headers={
                **auth, "Idempotency-Key": "rust-check-1"},
            body={"task_id": task["task_id"],
                  "lease_token": bundle["lease_token"],
                  "git_diff": diff,
                  "touched_symbols": ["src/calc.py::add"],
                  "local_test_passed": True},
        )
        deadline = time.monotonic() + 180
        final = {}
        while time.monotonic() < deadline:
            final = http("GET", base + f"/v2/submissions/{accepted['submission_id']}",
                         headers=auth)
            if final.get("final"):
                break
            time.sleep(0.3)
        evidence = http("GET", base + f"/v1/admin/tasks/{task['task_id']}/evidence",
                        headers=admin)
        report = {
            "image": args.image,
            "image_size": size,
            "core": core,
            "submission": final.get("status"),
            "commit": final.get("result_commit_sha"),
            "gates": [g.get("stage") for g in (evidence.get("gate_results") or [])],
            "evidence_complete": evidence.get("evidence_complete"),
            "claim_timing_ms": (bundle.get("claim_timing") or {}),
            "merge_timing_ms": (final.get("merge_timing") or {}),
            "normalize_ms": {"rust": micro_rust["normalize_ms"],
                             "python": micro_python["normalize_ms"]},
            "conflicts_ms": {"rust": micro_rust["conflicts_ms"],
                             "python": micro_python["conflicts_ms"]},
            "python_engine": micro_python["engine"],
            "rust_engine": micro_rust["engine"],
            "parity_sample": micro_rust["normalized_sample"] == micro_python["normalized_sample"],
        }
        print(json.dumps(report, indent=2))
        ok = (
            report["submission"] == "COMMITTED"
            and report["commit"]
            and len(report["gates"]) == 5
            and report["evidence_complete"] is True
        )
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if not args.keep:
            docker("rm", "-f", name, check=False)
            docker("rm", "-f", gate, check=False)
            shutil.rmtree(temp, ignore_errors=True)
        else:
            print(f"kept {temp} and containers {name}/{gate}")


if __name__ == "__main__":
    raise SystemExit(main())
