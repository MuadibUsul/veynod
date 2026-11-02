from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.work_scope import WorkScope, normalize_path


def test_versioned_contract_schemas_are_valid_json() -> None:
    contract_dir = Path(__file__).resolve().parents[1]
    for name in ("work-item", "work-scope", "event-envelope"):
        schema = json.loads((contract_dir / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert schema["$schema"].endswith("2020-12/schema")
        assert schema["type"] == "object"


def test_legacy_paths_have_stable_hash_and_exclusions_win() -> None:
    scope = WorkScope.from_dict({
        "scope_version": 1,
        "paths": {
            "include": ["src/module/**", "docs/plan.md", "src/module/**"],
            "exclude": ["src/module/secrets/**"],
        },
        "protected_paths": ["src/module/auth.py"],
    })
    assert scope.scope_hash == WorkScope.from_dict(scope.to_dict()).scope_hash
    assert scope.kernel_write_locks() == ["docs/plan.md", "src/module"]
    assert scope.allows_file("src/module/new/file.py")
    assert not scope.allows_file("src/module/secrets/key.py")
    assert not scope.allows_file("src/module/auth.py")
    assert not scope.allows_file("src/other.py")
    assert not scope.allows_file("SRC/module/new/file.py")
    assert WorkScope.from_legacy_paths(["src\\module\\file.py"]).allows_file("src/module/file.py")


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "C:\\secret", "src/../secret", "src//file", "src/./file", "src/[ab].py"])
def test_unsafe_scope_paths_fail_closed(path: str) -> None:
    with pytest.raises(ValueError):
        normalize_path(path, pattern=True)


def test_hash_cannot_be_rebound_and_root_wildcard_cannot_be_locked() -> None:
    with pytest.raises(ValueError, match="scope_hash"):
        WorkScope.from_dict({
            "scope_version": 1,
            "scope_hash": "0" * 64,
            "paths": {"include": ["src/**"]},
        })
    with pytest.raises(ValueError, match="root-wide"):
        WorkScope.from_legacy_paths(["**/*.py"]).kernel_write_locks()


def test_double_star_and_single_star_have_different_depth() -> None:
    scope = WorkScope.from_legacy_paths(["src/*/config.py"])
    assert scope.allows_file("src/api/config.py")
    assert not scope.allows_file("src/api/v2/config.py")
    assert not scope.allows_file("src/config.py")


def test_symbol_scope_only_narrows_path_scope() -> None:
    scope = WorkScope.from_dict({
        "scope_version": 1,
        "paths": {"include": ["src/**"]},
        "symbols": {
            "include": ["src/api.py::Allowed"],
            "exclude": ["src/api.py::Allowed::secret"],
        },
    })
    assert scope.allows_symbol("src/api.py::Allowed::run")
    assert not scope.allows_symbol("src/api.py::Allowed::secret")
    assert not scope.allows_symbol("src/api.py::Other")
    assert not scope.allows_symbol("tests/test_api.py::Allowed")
