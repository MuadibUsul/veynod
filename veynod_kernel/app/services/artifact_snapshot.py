"""Immutable Git content and AST snapshot cache for the merge fast path.

Git blobs are immutable, so ``(commit_sha, file_path)`` content and the AST
derived from a content hash can be cached for the process lifetime. The merge
critical section compares these snapshots instead of spawning ``git show`` and
re-parsing every sensitive file.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _is_commit_sha(value: str) -> bool:
    return len(value) in (40, 64) and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol_path: str
    source_hash: str
    signature_hash: str
    skeleton: str
    docstring: str | None
    referenced_names: tuple[str, ...]


@dataclass(frozen=True)
class FileASTSnapshot:
    file_path: str
    content_hash: str
    skeleton: str
    signature_hash: str
    symbols: dict[str, SymbolSnapshot]
    referenced_names: tuple[str, ...]

    @classmethod
    def from_slicer(cls, slicer: Any, source: str, file_path: str) -> "FileASTSnapshot":
        data = slicer.file_snapshot(source, file_path)
        symbols = {
            entry["symbol_path"]: SymbolSnapshot(
                symbol_path=entry["symbol_path"],
                source_hash=entry["source_hash"],
                signature_hash=entry["signature_hash"],
                skeleton=entry["skeleton"],
                docstring=entry["docstring"],
                referenced_names=tuple(entry["referenced_names"]),
            )
            for entry in data["symbols"]
        }
        return cls(
            file_path=file_path,
            content_hash=_content_hash(source),
            skeleton=data["skeleton"],
            signature_hash=data["signature_hash"],
            symbols=symbols,
            referenced_names=tuple(data["referenced_names"]),
        )

    def artifact_info(self, symbol_path: str) -> SymbolSnapshot | None:
        """Return the signature view for a file-level path or one symbol."""
        if "::" not in symbol_path:
            return SymbolSnapshot(
                symbol_path=symbol_path,
                source_hash=self.content_hash,
                signature_hash=self.signature_hash,
                skeleton=self.skeleton,
                docstring=None,
                referenced_names=self.referenced_names,
            )
        return self.symbols.get(symbol_path)

    def source_hash_for(self, symbol_path: str) -> str | None:
        info = self.artifact_info(symbol_path)
        return info.source_hash if info is not None else None

    def referenced_names_for(self, symbol_path: str) -> tuple[str, ...] | None:
        info = self.artifact_info(symbol_path)
        return info.referenced_names if info is not None else None


class ArtifactSnapshotCache:
    """Process-wide bounded cache; all operations run on the event loop."""

    def __init__(
        self,
        max_content: int = 4000,
        max_snapshots: int = 4000,
        max_commits: int = 10000,
    ) -> None:
        self._max_content = max_content
        self._max_snapshots = max_snapshots
        self._max_commits = max_commits
        self._content: OrderedDict[tuple[str, str, str], str | None] = OrderedDict()
        self._snapshots: OrderedDict[tuple[str, str], FileASTSnapshot] = OrderedDict()
        self._commit_snapshots: OrderedDict[
            tuple[str, str], FileASTSnapshot | None
        ] = OrderedDict()
        self._touched: OrderedDict[str, frozenset[str]] = OrderedDict()
        self._parents: OrderedDict[str, str | None] = OrderedDict()
        self._inflight: dict[tuple[str, str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------ primitives
    @staticmethod
    def _remember(cache: OrderedDict, key: Any, value: Any, limit: int) -> None:
        if key in cache:
            cache.move_to_end(key)
        else:
            if len(cache) >= limit:
                cache.popitem(last=False)
            cache[key] = value

    def get_content(
        self, repo_path: str | Path, commit: str, path: str
    ) -> tuple[bool, str | None]:
        key = (str(repo_path), commit, path)
        if key in self._content:
            self._content.move_to_end(key)
            return True, self._content[key]
        return False, None

    def put_content(
        self,
        repo_path: str | Path,
        commit: str,
        path: str,
        content: str | None,
    ) -> None:
        key = (str(repo_path), commit, path)
        self._remember(self._content, key, content, self._max_content)

    def get_snapshot(self, file_path: str, content_hash: str) -> FileASTSnapshot | None:
        key = (file_path, content_hash)
        snapshot = self._snapshots.get(key)
        if snapshot is not None:
            self._snapshots.move_to_end(key)
        return snapshot

    def put_snapshot(
        self, file_path: str, content_hash: str, snapshot: FileASTSnapshot
    ) -> None:
        self._remember(
            self._snapshots, (file_path, content_hash), snapshot, self._max_snapshots
        )

    def get_commit_snapshot(
        self, commit: str, path: str
    ) -> tuple[bool, FileASTSnapshot | None]:
        key = (commit, path)
        if key in self._commit_snapshots:
            self._commit_snapshots.move_to_end(key)
            return True, self._commit_snapshots[key]
        return False, None

    def put_commit_snapshot(
        self, commit: str, path: str, snapshot: FileASTSnapshot | None
    ) -> None:
        self._remember(
            self._commit_snapshots,
            (commit, path),
            snapshot,
            self._max_commits,
        )

    # --------------------------------------------------------------- content
    async def content_for(self, repo: Any, commit: str, path: str) -> str | None:
        found, value = self.get_content(repo.repo_path, commit, path)
        if found:
            return value
        if not _is_commit_sha(commit):
            # HEAD and other mutable refs are not safe to cache globally.
            return await repo.read_file(path, commit)

        key = (str(repo.repo_path), commit, path)
        lock = self._inflight.get(key)
        if lock is None:
            lock = asyncio.Lock()
            if len(self._inflight) >= 10000:
                self._inflight.clear()
            self._inflight[key] = lock
        async with lock:
            found, value = self.get_content(repo.repo_path, commit, path)
            if found:
                return value
            value = await repo.read_file(path, commit)
            self.put_content(repo.repo_path, commit, path, value)
            return value

    # -------------------------------------------------------------- snapshot
    async def snapshot_for(
        self, repo: Any, commit: str, path: str, slicer: Any
    ) -> FileASTSnapshot | None:
        found, snapshot = self.get_commit_snapshot(commit, path)
        if found:
            return snapshot

        content = await self.content_for(repo, commit, path)
        if content is None:
            self.put_commit_snapshot(commit, path, None)
            return None

        content_hash = _content_hash(content)
        snapshot = self.get_snapshot(path, content_hash)
        if snapshot is None:
            snapshot = FileASTSnapshot.from_slicer(slicer, content, path)
            self.put_snapshot(path, content_hash, snapshot)
        self.put_commit_snapshot(commit, path, snapshot)
        return snapshot

    async def ensure_commit_snapshots(
        self, repo: Any, commit: str, paths: list[str], slicer: Any
    ) -> None:
        unique = list(dict.fromkeys(paths))
        if not unique:
            return
        await asyncio.gather(
            *(self.snapshot_for(repo, commit, path, slicer) for path in unique)
        )

    def register_worktree_snapshot(
        self,
        repo_path: str | Path,
        commit: str,
        path: str,
        content: str | None,
        slicer: Any,
    ) -> FileASTSnapshot | None:
        """Precompute from a worktree file; the caller is outside the merge lock."""
        self.put_content(repo_path, commit, path, content)
        if content is None:
            self.put_commit_snapshot(commit, path, None)
            return None
        content_hash = _content_hash(content)
        snapshot = self.get_snapshot(path, content_hash)
        if snapshot is None:
            snapshot = FileASTSnapshot.from_slicer(slicer, content, path)
            self.put_snapshot(path, content_hash, snapshot)
        self.put_commit_snapshot(commit, path, snapshot)
        return snapshot

    def copy_commit_snapshots(
        self, source_commit: str, target_commit: str, paths: list[str]
    ) -> None:
        for path in dict.fromkeys(paths):
            found, snapshot = self.get_commit_snapshot(source_commit, path)
            if found:
                self.put_commit_snapshot(target_commit, path, snapshot)

    # ---------------------------------------------------------- commit graph
    def register_commit(
        self, commit: str, parent: str | None, changed_files: list[str]
    ) -> None:
        self._remember(
            self._touched, commit, frozenset(changed_files), self._max_commits
        )
        self._remember(self._parents, commit, parent, self._max_commits)

    def changed_files_in_range(self, base: str, head: str) -> set[str] | None:
        """Union touched files walking our own commit chain; None if unknown."""
        if base == head:
            return set()
        changed: set[str] = set()
        current = head
        for _ in range(self._max_commits):
            if current == base:
                return changed
            touched = self._touched.get(current)
            parent = self._parents.get(current)
            if touched is None or parent is None:
                return None
            changed.update(touched)
            current = parent
        return None

    def stats(self) -> dict[str, int]:
        return {
            "content": len(self._content),
            "snapshots": len(self._snapshots),
            "commit_snapshots": len(self._commit_snapshots),
            "commits": len(self._touched),
        }


artifact_snapshot_cache = ArtifactSnapshotCache()

__all__ = [
    "ArtifactSnapshotCache",
    "FileASTSnapshot",
    "SymbolSnapshot",
    "artifact_snapshot_cache",
]
