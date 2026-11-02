#!/usr/bin/env python3
"""Prove one Kernel worktree lease, then the gate and merge records.

Default (no cluster required):

    python3 scripts/smoke_kernel_worktree_gate.py

This allocates a real git worktree under ``.veynod-worktrees/lease/<hash>``
via ``worktree_pool.allocate_lease_worktree`` and checks the path is not the
repository root. It also prints the three host checks to run after deploy.

Live (kernel API already up, workspace seeded, write set exists in that repo):

    KERNEL_URL=http://127.0.0.1:8001 \\
    KERNEL_SERVICE_KEY=... \\
    KERNEL_RUNTIME_TOKEN=vnk_... \\
    KERNEL_WORKSPACE_ID=<uuid> \\
    SMOKE_WRITE_SET='src/calc.py::add' \\
    python3 scripts/smoke_kernel_worktree_gate.py --live

Do not put token values in the command line of a shared shell history if you
can avoid it; export them in the environment instead.

After deploy, on the host that runs Claude:

1. Process count (slots=1, no -review twin)::

    ps aux | grep -E '[c]laude' 

   Expect exactly one ``claude`` process. There must be no ``-review`` agent
   and the hub log must contain ``slots=1 workers=1 review_twin=absent``.

2. Working directory (never the shared checkout)::

    grep 'kernel worktree cwd=' ~/.veynod/hub/*/runner-receipts.jsonl
    # or the hub stdout

   The cwd must contain ``/lease/<16 hex>`` (or ``lease\\\\<hash>`` on
   Windows) under ``.veynod-worktrees``. It must not be the repository root
   mounted as the host checkout.

3. Submit pipeline (not a second Claude commenting)::

    grep -E 'submission_service id=|verification_gate |merge_coordinator sha=' \\
      <hub log>

   Expect, in order, a ``submission_service`` id, ``verification_gate`` with
   ``STATIC_TYPECHECK`` and ``UNIT_TEST_SUITE``, then ``merge_coordinator``
   with a sha and ``mode=fast-forward``.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "veynod_kernel"))

from app.services.git_ops import GitRepository  # noqa: E402
from app.services.worktree_pool import allocate_lease_worktree  # noqa: E402


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def local_lease() -> str:
    base = Path(tempfile.mkdtemp(prefix="veynod-smoke-lease-"))
    repo = base / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "smoke@example.com"], repo)
    _git(["config", "user.name", "smoke"], repo)
    (repo / "README").write_text("seed\n", encoding="utf-8")
    _git(["add", "README"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    task_id = UUID("33333333-3333-3333-3333-333333333333")

    async def _alloc() -> Path:
        path, _anchor, rel = await allocate_lease_worktree(
            GitRepository(repo, "main"), task_id, "HEAD"
        )
        if "lease/" not in rel:
            raise SystemExit(f"worktree rel is not a lease path: {rel}")
        if path.resolve() == repo.resolve():
            raise SystemExit("worktree is the repository root")
        return path

    path = asyncio.run(_alloc())
    print(f"kernel worktree cwd={path} pool=worktree_pool lease=held task={task_id}")
    print("local lease ok: path is an isolated worktree, not the repository root")
    print("hub log after a real task must also contain:")
    print("  submission_service id=<uuid> status=QUEUED")
    print("  verification_gate STATIC_TYPECHECK=... UNIT_TEST_SUITE=...")
    print("  merge_coordinator sha=<sha> mode=fast-forward")
    return str(path)


def live() -> None:
    import os

    import httpx

    base = os.environ["KERNEL_URL"].rstrip("/")
    workspace = os.environ["KERNEL_WORKSPACE_ID"]
    service = os.environ["KERNEL_SERVICE_KEY"]
    token = os.environ["KERNEL_RUNTIME_TOKEN"]
    symbol = os.environ.get("SMOKE_WRITE_SET", "src/calc.py::add")
    headers_service = {
        "X-Veynod-Service-Key": service,
        "X-Workspace-Id": workspace,
    }
    created = httpx.post(
        f"{base}/v1/runtime/tasks",
        headers=headers_service,
        json={
            "request_key": "smoke-kernel-worktree-gate",
            "title": "Smoke worktree gate",
            "spec_prompt": "Touch only the leased write set and leave the gate to merge.",
            "type": "IMPLEMENT_BODY",
            "target_symbols": [symbol],
            "write_set_symbols": [symbol],
            "priority": 100,
        },
        timeout=60,
    )
    created.raise_for_status()
    task_id = created.json()["task_id"]
    bundle = httpx.post(
        f"{base}/v1/syscalls/pull_task_bundle",
        headers={"Authorization": f"Bearer {token}", "X-Workspace-Id": workspace},
        json={
            "task_id": task_id,
            "worker_hub_id": "smoke-kernel-worktree",
            "supported_task_types": ["IMPLEMENT_BODY"],
            "max_context_tokens": 6000,
            "wait_seconds": 0,
        },
        timeout=60,
    )
    bundle.raise_for_status()
    body = bundle.json()
    path = body.get("worktree_path") or ""
    if "/lease/" not in path.replace("\\", "/"):
        raise SystemExit(f"pull did not allocate a lease worktree: {path!r}")
    print(f"kernel worktree cwd={path} pool=worktree_pool lease=held task={task_id}")
    print("live lease allocated; submit a diff with the runner to record")
    print("submission_service → verification_gate → merge_coordinator")
    # The lease token stays out of this log on purpose.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call a running Kernel")
    args = parser.parse_args()
    if args.live:
        live()
    else:
        local_lease()


if __name__ == "__main__":
    main()
