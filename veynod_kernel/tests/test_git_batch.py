"""Batch Git object reads used by claim bundle assembly."""

from __future__ import annotations

import asyncio
import subprocess

from app.services.git_ops import GitRepository


def _git(cwd, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, stderr=subprocess.STDOUT
    )


def test_read_files_batch_one_process(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "bench@example.test")
    _git(tmp_path, "config", "user.name", "bench")
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "seed")
    sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    repo = GitRepository(tmp_path)
    contents = asyncio.run(
        repo.read_files_batch(["a.py", "b.py", "missing.py"], sha)
    )
    assert contents["a.py"] == "def a():\n    return 1\n"
    assert contents["b.py"] == "def b():\n    return 2\n"
    assert contents["missing.py"] is None

    # The batch result primes the normal read_file cache.
    assert asyncio.run(repo.read_file("a.py", sha)) == contents["a.py"]
