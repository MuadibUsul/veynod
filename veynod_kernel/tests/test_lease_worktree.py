"""The agent cwd is a worktree_pool lease directory, not the shared checkout."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from uuid import UUID

from app.services.git_ops import GitRepository
from app.services.worktree_pool import allocate_lease_worktree, lease_worktree_paths


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "README").write_text("seed\n", encoding="utf-8")
    _git(["add", "README"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def test_lease_path_is_beside_the_checkout_under_lease(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = UUID("11111111-1111-1111-1111-111111111111")
    path, anchor, rel = lease_worktree_paths(str(repo), task_id)
    assert path != repo.resolve()
    assert repo.resolve() not in path.parents
    assert path.parent.name == "lease"
    assert "lease/" in rel
    assert Path(anchor) == repo.resolve().parent


def test_allocate_lease_worktree_is_a_detached_checkout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_id = UUID("22222222-2222-2222-2222-222222222222")

    async def _alloc() -> Path:
        git_repo = GitRepository(repo, "main")
        path, _anchor, rel = await allocate_lease_worktree(git_repo, task_id, "HEAD")
        assert rel.startswith(".veynod-worktrees/lease/")
        return path

    path = asyncio.run(_alloc())
    assert path.is_dir()
    assert (path / "README").read_text(encoding="utf-8") == "seed\n"
    assert path != repo.resolve()
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == "HEAD"
