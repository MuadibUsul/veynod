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


def test_empty_diagram_write_paths_fall_back_to_legacy_path() -> None:
    """The Kernel parser rejects an empty include list, so an empty Diagram
    scope must not be sent as one."""
    task = SimpleNamespace(input_context={"diagram": {"key": "d"},
                                          "allowed_write_paths": []})
    assert _module.work_scope_for(task) is None
    scoped = SimpleNamespace(input_context={"diagram": {"key": "d"},
                                            "allowed_write_paths": ["src/a.py"]})
    assert _module.work_scope_for(scoped)["paths"]["include"] == ["src/a.py"]


def test_needs_write_set_is_not_kernel_owned() -> None:
    """Review/merge must keep being placed while nothing is leased yet."""
    for status in ("needs_write_set", "lease_denied", "admission", "unbound", ""):
        task = SimpleNamespace(correlation={"kernel_delivery": {"mode": "kernel",
                                                               "status": status}})
        assert _module.kernel_owns_steps(task) is False, status
    leased = SimpleNamespace(correlation={"kernel_delivery": {"mode": "kernel",
                                                             "status": "leased"}})
    assert _module.kernel_owns_steps(leased) is True
    # The Loop's read model passes plain rows, not ORM objects.
    assert _module.kernel_owns_steps({"correlation": leased.correlation}) is True
    assert _module.kernel_owns_steps({"correlation": None}) is False


def test_expired_lease_is_not_reused() -> None:
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert _module._lease_expired(past) is True
    assert _module._lease_expired(soon) is False
    assert _module._lease_expired(None) is False
    # An unparsable stamp is treated as expired: re-provision instead of
    # submitting a token the Kernel will reject.
    assert _module._lease_expired("not-a-date") is True


@pytest.mark.asyncio
async def test_bind_only_creates_the_node_without_taking_a_lease(monkeypatch) -> None:
    """Dispatch binds; the executing side leases. A node leased while waiting
    for a host would hold a write-set lock nobody works under."""
    task = SimpleNamespace(
        repo="example/repo",
        input_context={"diagram": {"key": "d"}, "allowed_write_paths": ["src/a.py"]},
        title="A", task_key="DG-d-a", description="Implement the frozen contract.",
        priority=100, correlation={},
    )
    calls: list[str] = []

    class Plane:
        async def create_task(self, body):
            calls.append("create")
            return {"task_id": "11111111-1111-1111-1111-111111111111"}

        async def pull(self, task_id):
            calls.append("pull")
            return {"leased": True, "lease_token": "t"}

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
        Session(), task, plane=Plane(), lease=False,
        binding={"workspace_id": "kw", "base_url": "http://k"},
    )
    assert calls == ["create"]
    assert result["leased"] is False
    assert result["status"] == "bound"
    assert result["scope_hash"]


def test_kernel_error_codes_survive_the_product_api() -> None:
    code = _module._kernel_code
    assert code({"detail": {"error": "submission_in_progress", "message": "wait"}}) == \
        "submission_in_progress"
    assert code({"error": {"code": "lease_not_active"}}) == "lease_not_active"
    assert code({"detail": "plain text"}) is None
    assert code({"detail": {"error": "not a code; DROP"}}) is None


@pytest.mark.asyncio
async def test_a_revised_diff_gets_its_own_idempotency_key(monkeypatch) -> None:
    """A failed gate's revision used to reuse the first key: 409 forever."""
    task = SimpleNamespace(task_key="T-1", input_context={},
                           correlation={"kernel_delivery": {"kernel_task_id": "k-1",
                                                            "lease": "held"}})
    keys: list[str] = []

    class Plane:
        async def submit(self, body, key):
            keys.append(key)
            return 202, {"submission_id": f"s-{len(keys)}", "status": "QUEUED"}

    async def token(*_):
        return "lease-token"

    async def binding(*_):
        return {"workspace_id": "w"}

    async def stamp(*_):
        return None

    class Session:
        async def commit(self):
            return None

    monkeypatch.setattr(_module, "_load_token", token)
    monkeypatch.setattr(_module, "_binding", binding)
    monkeypatch.setattr(_module, "_stamp", stamp)
    for diff in ("diff --git a/x b/x\n+one\n", "diff --git a/x b/x\n+one\n",
                 "diff --git a/x b/x\n+two\n"):
        await _module.submit_diff(Session(), task, git_diff=diff, touched_symbols=["x"],
                                  local_test_passed=True, plane=Plane())
    assert keys[0] == keys[1], "the same diff retried dedupes"
    assert keys[2] != keys[0], "a revision is a new submission"


def test_the_reaper_waits_for_a_gate_that_holds_the_submission() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    def task(**delivery):
        return SimpleNamespace(correlation={"kernel_delivery": {"mode": "kernel", **delivery}})

    recent = (now - timedelta(minutes=10)).isoformat()
    assert _module.gate_pending(task(status="gate", gate="pending", submitted_at=recent), now, 3600)
    # Overdue, failed, merged, legacy and malformed records do not hold the task.
    old = (now - timedelta(hours=2)).isoformat()
    assert not _module.gate_pending(task(status="gate", gate="pending", submitted_at=old),
                                    now, 3600)
    assert not _module.gate_pending(task(status="gate_failed", gate="fail", submitted_at=recent),
                                    now, 3600)
    assert not _module.gate_pending(task(status="merged", submitted_at=recent), now, 3600)
    assert not _module.gate_pending(task(status="gate", submitted_at="yesterday"), now, 3600)
    assert not _module.gate_pending(SimpleNamespace(correlation={}), now, 3600)
