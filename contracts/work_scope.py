"""One canonical write boundary for Diagram, Runtime, Runner and Kernel.

Only the Kernel interprets path patterns for enforcement. Other services carry
the versioned object unchanged. A task's scope is immutable once created; a
new attempt is required to widen it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import Any, Iterable


_DRIVE = re.compile(r"^[A-Za-z]:")
_VALID_SEGMENT = re.compile(r"^[^\x00-\x1f\[\]]+$")


def normalize_path(value: str, *, pattern: bool = False) -> str:
    """Normalize a repository-relative path; fail closed on traversal."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("scope path must be a nonempty string")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or _DRIVE.match(raw) or raw.startswith("//"):
        raise ValueError(f"absolute scope path is forbidden: {value!r}")
    segments = raw.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"unsafe scope path: {value!r}")
    for segment in segments:
        if not _VALID_SEGMENT.fullmatch(segment):
            raise ValueError(f"unsupported scope path: {value!r}")
        if not pattern and any(char in segment for char in "*?"):
            raise ValueError(f"wildcard is not a file path: {value!r}")
        if pattern and "**" in segment and segment != "**":
            raise ValueError(f"** must occupy a whole path segment: {value!r}")
    return "/".join(segments)


def _patterns(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("scope patterns must be a list")
    return tuple(sorted({normalize_path(value, pattern=True) for value in values}))


def _strings(values: Any, label: str) -> tuple[str, ...]:
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError(f"{label} must be a list of nonempty strings")
    return tuple(sorted({item.strip() for item in values}))


def _symbol(value: str) -> str:
    parts = value.strip().split("::")
    file_path = normalize_path(parts[0])
    if any(
        not part or part in {".", ".."} or
        any(char in part for char in "/\\*?\x00\n\r")
        for part in parts[1:]
    ):
        raise ValueError(f"unsafe scope symbol: {value!r}")
    return "::".join((file_path, *parts[1:]))


def _symbols(values: Any, label: str) -> tuple[str, ...]:
    return tuple(sorted({_symbol(value) for value in _strings(values, label)}))


@lru_cache(maxsize=4096)
def _match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return _match(pattern[1:], path) or bool(path and _match(pattern, path[1:]))
    return bool(path and fnmatchcase(path[0], pattern[0]) and _match(pattern[1:], path[1:]))


@dataclass(frozen=True)
class WorkScope:
    paths_include: tuple[str, ...]
    paths_exclude: tuple[str, ...] = ()
    symbols_include: tuple[str, ...] = ()
    symbols_exclude: tuple[str, ...] = ()
    database_tables: tuple[str, ...] = ()
    api_contracts: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    scope_version: int = 1

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkScope":
        if not isinstance(raw, dict) or raw.get("scope_version") != 1:
            raise ValueError("unsupported WorkScope version")
        if set(raw) - {"scope_version", "scope_hash", "paths", "symbols", "resources", "protected_paths"}:
            raise ValueError("unknown WorkScope field")
        paths = raw.get("paths") or {}
        symbols = raw.get("symbols") or {}
        resources = raw.get("resources") or {}
        if not all(isinstance(item, dict) for item in (paths, symbols, resources)):
            raise ValueError("paths, symbols and resources must be objects")
        if (set(paths) - {"include", "exclude"} or
            set(symbols) - {"include", "exclude"} or
            set(resources) - {"database_tables", "api_contracts"}):
            raise ValueError("unknown WorkScope nested field")
        scope = cls(
            paths_include=_patterns(paths.get("include", [])),
            paths_exclude=_patterns(paths.get("exclude", [])),
            symbols_include=_symbols(symbols.get("include", []), "symbols.include"),
            symbols_exclude=_symbols(symbols.get("exclude", []), "symbols.exclude"),
            database_tables=_strings(resources.get("database_tables", []), "resources.database_tables"),
            api_contracts=_strings(resources.get("api_contracts", []), "resources.api_contracts"),
            protected_paths=_patterns(raw.get("protected_paths", [])),
        )
        if not scope.paths_include:
            raise ValueError("WorkScope requires a nonempty paths.include")
        claimed = raw.get("scope_hash")
        if claimed is not None and claimed != scope.scope_hash:
            raise ValueError("WorkScope scope_hash does not match its contents")
        return scope

    @classmethod
    def from_legacy_paths(cls, values: Iterable[str]) -> "WorkScope":
        return cls.from_dict({"scope_version": 1, "paths": {"include": list(values), "exclude": []}})

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_version": self.scope_version,
            "scope_hash": self.scope_hash,
            "paths": {"include": list(self.paths_include), "exclude": list(self.paths_exclude)},
            "symbols": {"include": list(self.symbols_include), "exclude": list(self.symbols_exclude)},
            "resources": {"database_tables": list(self.database_tables), "api_contracts": list(self.api_contracts)},
            "protected_paths": list(self.protected_paths),
        }

    @property
    def scope_hash(self) -> str:
        body = {
            "scope_version": self.scope_version,
            "paths": {"include": self.paths_include, "exclude": self.paths_exclude},
            "symbols": {"include": self.symbols_include, "exclude": self.symbols_exclude},
            "resources": {"database_tables": self.database_tables, "api_contracts": self.api_contracts},
            "protected_paths": self.protected_paths,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def allows_file(self, value: str) -> bool:
        path = tuple(normalize_path(value).split("/"))
        allowed = any(_match(tuple(pattern.split("/")), path) for pattern in self.paths_include)
        denied = any(
            _match(tuple(pattern.split("/")), path)
            for pattern in (*self.paths_exclude, *self.protected_paths)
        )
        return allowed and not denied

    def assert_files_allowed(self, values: Iterable[str]) -> None:
        files = list(values)
        if not files or any(not self.allows_file(value) for value in files):
            raise PermissionError("submission changes files outside the frozen WorkScope")

    def allows_symbol(self, value: str) -> bool:
        symbol = _symbol(value)
        if not self.allows_file(symbol.split("::", 1)[0]):
            return False
        includes = not self.symbols_include or any(
            symbol == allowed or symbol.startswith(f"{allowed}::")
            for allowed in self.symbols_include
        )
        excludes = any(
            symbol == denied or symbol.startswith(f"{denied}::")
            for denied in self.symbols_exclude
        )
        return includes and not excludes

    def kernel_write_locks(self) -> list[str]:
        """Conservative lock ancestors; exact scope remains the submission fence."""
        locks: set[str] = set()
        for pattern in self.paths_include:
            segments = pattern.split("/")
            prefix: list[str] = []
            for segment in segments:
                if any(char in segment for char in "*?"):
                    break
                prefix.append(segment)
            if not prefix:
                raise ValueError("root-wide wildcard scope cannot be mapped to a Kernel lock")
            locks.add("/".join(prefix))
        return sorted(locks)


__all__ = ["WorkScope", "normalize_path"]
