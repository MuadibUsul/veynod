from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


# Load this dependency-light module without importing app.services.__init__,
# which pulls optional database extensions into a pure adapter test.
_path = Path(__file__).resolve().parents[1] / "app" / "services" / "kernel_delivery.py"
_spec = importlib.util.spec_from_file_location("kernel_delivery_scope_test", _path)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
work_scope_for = _module.work_scope_for
write_set_for = _module.write_set_for


def test_diagram_scope_is_source_even_with_legacy_symbols() -> None:
    task = SimpleNamespace(input_context={
        "diagram": {"key": "d", "version": 2},
        "allowed_write_paths": ["src/api/**"],
        "write_set_symbols": ["src/other.py"],
    })
    assert work_scope_for(task) == {
        "scope_version": 1,
        "paths": {"include": ["src/api/**"], "exclude": []},
    }
    assert write_set_for(task) == ["src/other.py"]


def test_non_diagram_legacy_task_keeps_its_write_set() -> None:
    task = SimpleNamespace(input_context={"write_set_symbols": ["src/main.py"]})
    assert work_scope_for(task) is None
    assert write_set_for(task) == ["src/main.py"]


def test_scoped_diagram_cannot_reuse_an_old_unscoped_lease() -> None:
    task = SimpleNamespace(input_context={
        "diagram": {"key": "d"}, "allowed_write_paths": ["src/api/**"],
    })
    assert not _module._scope_matches_delivery(task, {"write_set": ["src"]})
    assert not _module._scope_matches_delivery(task, {
        "scope_hash": "a" * 64,
        "work_scope": {"paths": {"include": ["src/other/**"]}},
    })
    assert not _module._scope_matches_delivery(task, {
        "scope_hash": "a" * 64,
        "work_scope": {
            "paths": {"include": ["src/api/**"], "exclude": []},
            "protected_paths": ["src/api/hidden.py"],
        },
    })


@pytest.mark.asyncio
async def test_diagram_product_task_carries_scope_to_kernel_creation(monkeypatch) -> None:
    task = SimpleNamespace(
        repo="example/repo",
        input_context={
            "diagram": {"key": "d", "version": 2},
            "allowed_write_paths": ["src/api/**"],
        },
        title="API chunk",
        task_key="DG-d-api",
        description="Implement the frozen API contract.",
        priority=100,
        correlation={},
    )
    created: list[dict] = []

    class Plane:
        async def create_task(self, body):
            created.append(body)
            return {"task_id": "11111111-1111-1111-1111-111111111111"}

        async def pull(self, task_id):
            return {
                "leased": True,
                "lease_token": "vnl_secret",
                "write_set": ["src/api"],
                "work_scope": {"scope_version": 1, "paths": {"include": ["src/api/**"], "exclude": []}},
                "scope_hash": "a" * 64,
            }

    class Session:
        async def commit(self):
            pass

    async def remember(*args, **kwargs):
        pass

    async def stamp(session, product_task, record):
        return record

    monkeypatch.setattr(_module, "_remember_lease", remember)
    monkeypatch.setattr(_module, "_stamp", stamp)
    result = await _module.provision_product_task(
        Session(), task, plane=Plane(),
        binding={"workspace_id": "kernel-workspace", "base_url": "http://kernel"},
    )
    assert created[0]["work_scope"]["paths"]["include"] == ["src/api/**"]
    assert created[0]["write_set_symbols"] == []
    assert created[0]["target_symbols"] == []
    assert result["scope_hash"] == "a" * 64
