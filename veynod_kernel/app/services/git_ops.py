"""Async Git plumbing for worktree sandboxes and the atomic merge gate."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.engine import rust_bridge
from app.services.artifact_snapshot import artifact_snapshot_cache

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    def __init__(self, command: str, returncode: int, output: str) -> None:
        super().__init__(f"{command} exited {returncode}: {output[-2000:]}")
        self.command = command
        self.returncode = returncode
        self.output = output


class RepositoryNotUsable(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def _communicate(proc: asyncio.subprocess.Process, timeout: float) -> CommandResult:
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        return CommandResult(124, f"command timed out after {timeout:.0f}s")

    return CommandResult(
        proc.returncode if proc.returncode is not None else 1,
        (stdout or b"").decode("utf-8", errors="replace"),
    )


async def run_git(
    args: list[str], cwd: str | Path, *, timeout: float = 120.0
) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    return await _communicate(proc, timeout)


async def run_git_checked(
    args: list[str], cwd: str | Path, *, timeout: float = 120.0
) -> str:
    result = await run_git(args, cwd, timeout=timeout)
    if not result.ok:
        raise GitError("git " + " ".join(args), result.returncode, result.output)
    return result.output


async def run_shell(
    command: str,
    cwd: str | Path,
    *,
    timeout: float | None = None,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a trusted, configuration-supplied gate command through the shell.

    The Rust supervisor provides process-tree kill and bounded output; if the
    extension is unavailable (or errors), this falls back to asyncio subprocess.
    """
    root = str(cwd)
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    overrides = {
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": root
        + (os.pathsep + existing_pythonpath if existing_pythonpath else ""),
    }
    if extra_env:
        overrides.update(extra_env)

    limit = timeout or settings.gate_timeout_seconds
    if rust_bridge.available():
        try:
            result = await asyncio.to_thread(
                rust_bridge.run_command,
                command,
                root,
                float(limit),
                overrides,
                settings.gate_output_chars,
            )
            return CommandResult(int(result["returncode"]), str(result["output"]))
        except Exception as exc:  # noqa: BLE001 - keep the gate alive
            logger.warning("Rust command supervisor failed; using asyncio: %s", exc)

    env = os.environ.copy()
    env.update(overrides)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    return await _communicate(proc, limit)


class _GitBatchReader:
    """One long-lived ``git cat-file --batch`` process."""

    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = str(repo_path)
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self.process is not None and self.process.returncode is None:
            return self.process
        self.process = await asyncio.create_subprocess_exec(
            "git",
            "cat-file",
            "--batch",
            cwd=self.repo_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self.process

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    async def read(self, specs: list[str], timeout: float = 120.0) -> list[str | None]:
        async with self.lock:
            for attempt in range(2):
                process = await self._ensure_process()
                assert process.stdin is not None and process.stdout is not None
                try:
                    process.stdin.write(
                        "".join(f"{spec}\n" for spec in specs).encode()
                    )
                    await asyncio.wait_for(process.stdin.drain(), timeout=timeout)
                    contents: list[str | None] = []
                    for _ in specs:
                        header = await asyncio.wait_for(
                            process.stdout.readline(), timeout=timeout
                        )
                        if not header:
                            raise ConnectionError("git cat-file closed stdout")
                        header = header.rstrip(b"\r\n")
                        if header.endswith(b" missing") or header.endswith(
                            b" ambiguous"
                        ):
                            contents.append(None)
                            continue
                        parts = header.split()
                        if len(parts) != 3 or parts[1] != b"blob":
                            raise GitError(
                                "git cat-file --batch",
                                1,
                                f"unexpected header {header[:200]!r}",
                            )
                        size = int(parts[2])
                        body = await asyncio.wait_for(
                            process.stdout.readexactly(size + 1), timeout=timeout
                        )
                        contents.append(
                            body[:size].decode("utf-8", errors="replace")
                        )
                    return contents
                except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
                    await self.close()
                    if attempt == 1:
                        raise
            raise GitError("git cat-file --batch", 1, "reader restarted")


class _GitBatchPool:
    def __init__(self, repo_path: str | Path, size: int = 4) -> None:
        self.readers = [_GitBatchReader(repo_path) for _ in range(max(1, size))]
        self.index = 0

    def next(self) -> _GitBatchReader:
        reader = self.readers[self.index % len(self.readers)]
        self.index += 1
        return reader

    async def close(self) -> None:
        for reader in self.readers:
            await reader.close()


_batch_pools: dict[tuple[str, int], _GitBatchPool] = {}


def _batch_pool(repo_path: str | Path) -> _GitBatchPool:
    key = (str(repo_path), id(asyncio.get_running_loop()))
    pool = _batch_pools.get(key)
    if pool is None:
        pool = _GitBatchPool(repo_path)
        _batch_pools[key] = pool
    return pool


async def close_git_batch_pools() -> None:
    for key, pool in list(_batch_pools.items()):
        _batch_pools.pop(key, None)
        await pool.close()


class GitRepository:
    def __init__(self, repo_path: str | Path, main_branch: str = "main") -> None:
        self.repo_path = Path(repo_path)
        self.main_branch = main_branch
        self._file_cache: dict[tuple[str, str], str | None] = {}
        self._list_cache: dict[str, set[str]] = {}

    # --------------------------------------------------------------- queries
    async def head_sha(self) -> str:
        return (await run_git_checked(["rev-parse", "HEAD"], self.repo_path)).strip()

    async def resolve_ref(self, ref: str) -> str | None:
        result = await run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], self.repo_path)
        return result.output.strip() if result.ok else None

    async def is_bare(self) -> bool:
        out = await run_git_checked(["rev-parse", "--is-bare-repository"], self.repo_path)
        return out.strip().lower() == "true"

    async def current_branch(self) -> str | None:
        result = await run_git(["symbolic-ref", "--short", "-q", "HEAD"], self.repo_path)
        if not result.ok:
            return None
        return result.output.strip() or None

    async def checked_out_branches(self) -> set[str]:
        result = await run_git(["worktree", "list", "--porcelain"], self.repo_path)
        if not result.ok:
            return set()
        branches: set[str] = set()
        for line in result.output.splitlines():
            if line.startswith("branch refs/heads/"):
                branches.add(line[len("branch refs/heads/") :].strip())
        return branches

    async def validate_usable(self, allow_nonbare: bool | None = None) -> None:
        allow = settings.allow_nonbare_repo if allow_nonbare is None else allow_nonbare
        if not self.repo_path.exists():
            raise RepositoryNotUsable(f"Repository path does not exist: {self.repo_path}")
        bare = await self.is_bare()
        if bare:
            return
        if allow:
            return
        branches = await self.checked_out_branches()
        if self.main_branch in branches:
            raise RepositoryNotUsable(
                f"'{self.main_branch}' is checked out in {self.repo_path}. The gate advances "
                "the branch with git update-ref, which would leave that working tree stale. "
                "Use a bare mirror (git clone --bare) or check out another branch. "
                "Set VN_ALLOW_NONBARE_REPO=true only if you understand the risk."
            )

    async def list_files(self, rev: str = "HEAD") -> set[str]:
        if rev in self._list_cache:
            return self._list_cache[rev]
        out = await run_git_checked(["ls-tree", "-r", "--name-only", rev], self.repo_path)
        files = {line.strip() for line in out.splitlines() if line.strip()}
        self._list_cache[rev] = files
        return files

    async def read_file(self, path: str, rev: str = "HEAD") -> str | None:
        immutable = len(rev) in (40, 64) and all(
            character in "0123456789abcdefABCDEF" for character in rev
        )
        if immutable:
            found, value = artifact_snapshot_cache.get_content(
                self.repo_path, rev, path
            )
            if found:
                return value
        cache_key = (rev, path)
        if cache_key in self._file_cache:
            return self._file_cache[cache_key]
        result = await run_git(["show", f"{rev}:{path}"], self.repo_path)
        content = result.output if result.ok else None
        self._file_cache[cache_key] = content
        if immutable:
            artifact_snapshot_cache.put_content(self.repo_path, rev, path, content)
        return content

    async def read_files_batch(
        self, paths: list[str] | set[str], rev: str = "HEAD"
    ) -> dict[str, str | None]:
        """Read many blobs with one ``git cat-file --batch`` process.

        Claim bundle assembly otherwise starts one ``git show`` per artifact;
        at 1000 concurrent claims that is thousands of process launches and
        becomes the dominant API-side Git cost. Results are primed into the
        per-repository cache so the existing ``read_file`` callers transparently
        hit memory.
        """
        immutable = len(rev) in (40, 64) and all(
            character in "0123456789abcdefABCDEF" for character in rev
        )
        result_map: dict[str, str | None] = {}
        requested: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            cache_key = (rev, path)
            if cache_key in self._file_cache:
                result_map[path] = self._file_cache[cache_key]
                continue
            if immutable:
                found, value = artifact_snapshot_cache.get_content(
                    self.repo_path, rev, path
                )
                if found:
                    self._file_cache[cache_key] = value
                    result_map[path] = value
                    continue
            requested.append(path)

        if not requested:
            return result_map

        started = time.perf_counter()
        reader = _batch_pool(self.repo_path).next()
        contents = await reader.read(
            [f"{rev}:{path}" for path in requested], timeout=120.0
        )
        for path, content in zip(requested, contents):
            cache_key = (rev, path)
            self._file_cache[cache_key] = content
            if immutable:
                artifact_snapshot_cache.put_content(
                    self.repo_path, rev, path, content
                )
            result_map[path] = content

        try:
            from app.services.runtime_metrics import get_runtime_metrics

            metrics = get_runtime_metrics()
            metrics.observe(
                "git.batch_read_ms", (time.perf_counter() - started) * 1000
            )
            metrics.inc("git.batch_read_calls")
            metrics.inc("git.batch_read_files", len(requested))
        except Exception:  # noqa: BLE001 - metrics must not break reads
            pass
        return result_map

    async def path_exists(self, path: str, rev: str = "HEAD") -> bool:
        result = await run_git(["cat-file", "-e", f"{rev}:{path}"], self.repo_path)
        return result.ok

    async def diff_at_head(self, worktree: Path) -> str:
        return await run_git_checked(["diff", "HEAD"], worktree)

    async def changed_files_staged(self, worktree: Path, base: str = "HEAD") -> list[str]:
        out = await run_git_checked(
            ["diff", "--cached", "--name-only", base], worktree
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def commit_exists(self, sha: str) -> bool:
        result = await run_git(["cat-file", "-e", f"{sha}^{{commit}}"], self.repo_path)
        return result.ok

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = await run_git(
            ["merge-base", "--is-ancestor", ancestor, descendant], self.repo_path
        )
        return result.ok

    async def changed_files_between(self, old_sha: str, new_sha: str) -> list[str]:
        out = await run_git_checked(
            ["diff", "--name-only", old_sha, new_sha], self.repo_path
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def merge_tree(self, ours: str, theirs: str) -> str | None:
        """Memory three-way tree merge; None on conflict or unsupported Git."""
        result = await run_git(
            ["merge-tree", "--write-tree", ours, theirs], self.repo_path, timeout=120
        )
        if not result.ok:
            return None
        lines = result.output.splitlines()
        if not lines:
            return None
        candidate = lines[0].strip()
        if len(candidate) == 40 and all(
            character in "0123456789abcdef" for character in candidate.lower()
        ):
            return candidate
        return None

    async def commit_tree(self, tree_sha: str, parent_sha: str, message: str) -> str:
        result = await run_git(
            [
                "-c",
                "user.name=Veynod Kernel",
                "-c",
                "user.email=kernel@veynod.local",
                "commit-tree",
                tree_sha,
                "-p",
                parent_sha,
                "-m",
                message,
            ],
            self.repo_path,
            timeout=120,
        )
        if not result.ok:
            raise GitError("git commit-tree", result.returncode, result.output)
        return result.output.strip()

    # -------------------------------------------------------------- worktrees
    async def add_worktree(self, destination: Path, rev: str) -> None:
        await run_git_checked(
            ["worktree", "add", "--detach", str(destination), rev], self.repo_path
        )

    async def reset_worktree(self, destination: Path, rev: str) -> None:
        """Reuse a pooled worktree without touching the bare repo's config.lock."""
        await run_git_checked(["reset", "--hard", "HEAD"], destination)
        await run_git_checked(["clean", "-fdx"], destination)
        await run_git_checked(
            ["checkout", "--detach", "--force", rev], destination
        )
        await run_git_checked(["reset", "--hard", rev], destination)

    async def configure_for_swarm(self) -> None:
        """Disable auto-gc and preload the index for high-frequency worktrees."""
        for key, value in (("gc.auto", "0"), ("core.preloadIndex", "true")):
            result = await run_git(["config", key, value], self.repo_path)
            if not result.ok:
                logger.warning(
                    "Could not set git config %s=%s on %s: %s",
                    key,
                    value,
                    self.repo_path,
                    result.output.strip(),
                )

    async def remove_worktree(self, destination: Path) -> None:
        await run_git(["worktree", "remove", "--force", str(destination)], self.repo_path)
        await run_git(["worktree", "prune"], self.repo_path)
        shutil.rmtree(destination, ignore_errors=True)

    async def apply_patch(self, worktree: Path, patch_file: Path) -> CommandResult:
        return await run_git(
            ["apply", "--whitespace=nowarn", str(patch_file.absolute())], worktree
        )

    async def stage_all(self, worktree: Path) -> None:
        await run_git_checked(["add", "-A"], worktree)

    async def stage_paths(self, worktree: Path, paths: list[str]) -> None:
        if not paths:
            raise GitError("git add", 1, "no changed paths to stage")
        await run_git_checked(["add", "-A", "--", *paths], worktree)

    async def commit(
        self,
        worktree: Path,
        message: str,
        *,
        author_name: str = "Veynod Kernel",
        author_email: str = "kernel@veynod.local",
    ) -> str:
        result = await run_git(
            [
                "-c",
                f"user.name={author_name}",
                "-c",
                f"user.email={author_email}",
                "commit",
                "-m",
                message,
            ],
            worktree,
            timeout=120,
        )
        if not result.ok:
            raise GitError("git commit", result.returncode, result.output)
        return (await run_git_checked(["rev-parse", "HEAD"], worktree)).strip()

    async def update_ref(
        self, new_sha: str, old_sha: str | None = None
    ) -> CommandResult:
        ref = f"refs/heads/{self.main_branch}"
        if old_sha:
            return await run_git(["update-ref", ref, new_sha, old_sha], self.repo_path)
        return await run_git(["update-ref", ref, new_sha], self.repo_path)


__all__ = [
    "CommandResult",
    "GitError",
    "GitRepository",
    "RepositoryNotUsable",
    "run_git",
    "run_git_checked",
    "run_shell",
]
