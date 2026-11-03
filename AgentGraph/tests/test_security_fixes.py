"""Offline regressions for the audited trust boundaries."""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.request_body import bounded_body
from app.auth import AuthKind
from app.errors import ApiError, Forbidden
from app.services import exports, task_lifecycle
from app.services import kernel_binding_policy as binding_policy


@pytest.mark.asyncio
async def test_tenant_owner_cannot_use_privileged_import() -> None:
    principal = SimpleNamespace(auth_kind=AuthKind.HUMAN, human_role="OWNER")
    with pytest.raises(Forbidden):
        await exports.import_archive(principal, b"", target="increment")


def test_archive_rejects_uncompressed_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exports, "settings", SimpleNamespace(max_import_bytes=128))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"format": exports.FORMAT}))
        archive.writestr("objects.jsonl", "x" * 200)
    with pytest.raises(ApiError) as error:
        exports._read_archive(payload.getvalue())
    assert error.value.status_code == 413


def test_deleted_rows_only_accept_exported_primary_keys() -> None:
    pks = {"shared_documents": ["id"]}
    assert exports._validate_deletion(
        {"table": "shared_documents", "columns": ["id"], "pk": ["doc"]}, pks
    ) == "shared_documents"
    for table, columns in (("shared_documents; DROP TABLE tenants", ["id"]),
                           ("shared_documents", ["id OR true"])):
        with pytest.raises(ApiError):
            exports._validate_deletion({"table": table, "columns": columns,
                                        "pk": ["doc"]}, pks)


def test_binding_rejects_unregistered_destinations_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binding_policy, "get_settings", lambda: SimpleNamespace(
        kernel_url="http://kernel-api:8001", kernel_admin_keys="",
        kernel_workspace_id=None, environment="production"))
    monkeypatch.delenv("KERNEL_ALLOWED_BASE_URLS", raising=False)
    monkeypatch.delenv("KERNEL_ALLOWED_CREDENTIAL_REFS", raising=False)
    assert binding_policy.validate_binding("http://kernel-api:8001/", "KERNEL_RUNTIME_TOKEN") == (
        "http://kernel-api:8001", "KERNEL_RUNTIME_TOKEN")
    with pytest.raises(ApiError):
        binding_policy.validate_binding("https://attacker.example", "KERNEL_RUNTIME_TOKEN")
    with pytest.raises(ApiError):
        binding_policy.validate_binding("http://kernel-api:8001", "ADMIN_TOKEN")
    monkeypatch.delenv("KERNEL_WORKSPACE_BINDINGS", raising=False)
    with pytest.raises(ApiError):
        binding_policy.validate_workspace_assignment(
            "product-a", "kernel-a", "http://kernel-api:8001", "KERNEL_RUNTIME_TOKEN")
    monkeypatch.setenv("KERNEL_WORKSPACE_BINDINGS", json.dumps({
        "product-a": [{"kernel_workspace_id": "kernel-a",
                       "kernel_base_url": "http://kernel-api:8001",
                       "credential_ref": "KERNEL_RUNTIME_TOKEN"}]}))
    binding_policy.validate_workspace_assignment(
        "product-a", "kernel-a", "http://kernel-api:8001", "KERNEL_RUNTIME_TOKEN")
    with pytest.raises(ApiError):
        binding_policy.validate_workspace_assignment(
            "product-b", "kernel-a", "http://kernel-api:8001", "KERNEL_RUNTIME_TOKEN")


@pytest.mark.asyncio
async def test_chunked_request_body_stops_at_limit() -> None:
    parts = iter([b"abcd", b"efghi"])

    async def receive() -> dict:
        try:
            return {"type": "http.request", "body": next(parts), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "PUT", "path": "/blob",
                       "headers": []}, receive)
    with pytest.raises(ApiError) as error:
        await bounded_body(request, 8)
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_kernel_binding_never_falls_back_to_another_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rows:
        def __init__(self, row):
            self.row = row

        def mappings(self):
            return self

        def all(self):
            return [self.row]

    class Session:
        def __init__(self, row):
            self.row = row

        async def execute(self, *args, **kwargs):
            return Rows(self.row)

    monkeypatch.setattr(task_lifecycle, "settings", SimpleNamespace(
        kernel_url="", kernel_workspace_id=""))
    row = {"project": "other", "repo_path": "other/repo",
           "kernel_base_url": "http://wrong-kernel",
           "kernel_workspace_id": "wrong-workspace",
           "credential_ref": "KERNEL_RUNTIME_TOKEN",
           "worker_hub_id": "veynod-runtime"}
    assert await task_lifecycle.kernel_binding_for(
        Session(row), "workspace", "requested", "requested/repo") is None
    row.update(project="requested", repo_path="requested/repo")
    assert await task_lifecycle.kernel_binding_for(
        Session(row), "workspace", "requested", "requested/repo") is None
