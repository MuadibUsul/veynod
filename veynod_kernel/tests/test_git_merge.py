from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from app.services.git_ops import GitRepository, run_git_checked


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {result.stderr}")
    return result.stdout.strip()


def test_merge_tree_and_commit_tree_combine_disjoint_commits(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Merge"], work)
    _run(["git", "config", "user.email", "merge@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "a.py").write_text("VALUE_A = 1\n", encoding="utf-8")
    (work / "src" / "b.py").write_text("VALUE_B = 1\n", encoding="utf-8")
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)
    base = _run(["git", "rev-parse", "HEAD"], work)
    bare = tmp_path / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], tmp_path)

    async def scenario() -> None:
        repo = GitRepository(bare, "main")
        wt_a = tmp_path / "wt-a"
        wt_b = tmp_path / "wt-b"
        await repo.add_worktree(wt_a, base)
        await repo.add_worktree(wt_b, base)
        try:
            (wt_a / "src" / "a.py").write_text("VALUE_A = 2\n", encoding="utf-8")
            await repo.stage_all(wt_a)
            sha_a = await repo.commit(wt_a, "change a")

            (wt_b / "src" / "b.py").write_text("VALUE_B = 2\n", encoding="utf-8")
            await repo.stage_all(wt_b)
            sha_b = await repo.commit(wt_b, "change b")

            tree = await repo.merge_tree(sha_a, sha_b)
            assert tree is not None

            merged = await repo.commit_tree(tree, sha_a, "tree merge")
            updated = await repo.update_ref(merged, base)
            assert updated.ok, updated.output

            a_content = await run_git_checked(["show", "main:src/a.py"], bare)
            b_content = await run_git_checked(["show", "main:src/b.py"], bare)
            assert "VALUE_A = 2" in a_content
            assert "VALUE_B = 2" in b_content
        finally:
            await repo.remove_worktree(wt_a)
            await repo.remove_worktree(wt_b)

    asyncio.run(scenario())


def test_merge_tree_returns_none_on_conflict(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Merge"], work)
    _run(["git", "config", "user.email", "merge@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)
    base = _run(["git", "rev-parse", "HEAD"], work)
    bare = tmp_path / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], tmp_path)

    async def scenario() -> None:
        repo = GitRepository(bare, "main")
        wt_a = tmp_path / "wt-a"
        wt_b = tmp_path / "wt-b"
        await repo.add_worktree(wt_a, base)
        await repo.add_worktree(wt_b, base)
        try:
            (wt_a / "src" / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
            await repo.stage_all(wt_a)
            sha_a = await repo.commit(wt_a, "change a")

            (wt_b / "src" / "a.py").write_text("VALUE = 3\n", encoding="utf-8")
            await repo.stage_all(wt_b)
            sha_b = await repo.commit(wt_b, "change a differently")

            assert await repo.merge_tree(sha_a, sha_b) is None
        finally:
            await repo.remove_worktree(wt_a)
            await repo.remove_worktree(wt_b)

    asyncio.run(scenario())
