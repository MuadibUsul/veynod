"""Pure-function tests for the v2 submission idempotency material."""

from __future__ import annotations

import pytest

from app.services.submission_service import (
    SubmissionRejected,
    derive_idempotency_key,
    normalize_idempotency_key,
    patch_sha256,
)


def test_patch_sha_is_exact_bytes():
    assert patch_sha256("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert patch_sha256("abc ") != patch_sha256("abc")


def test_derived_key_binds_task_lease_and_patch():
    kwargs = dict(
        task_id="task-1",
        runner_id="runner-1",
        lease_token_hash="a" * 64,
        patch_digest=patch_sha256("diff"),
    )
    first = derive_idempotency_key(**kwargs)
    assert first == derive_idempotency_key(**kwargs)
    assert first != derive_idempotency_key(**{**kwargs, "task_id": "task-2"})
    assert first != derive_idempotency_key(**{**kwargs, "runner_id": "runner-2"})
    assert first != derive_idempotency_key(**{**kwargs, "lease_token_hash": "b" * 64})
    assert first != derive_idempotency_key(
        **{**kwargs, "patch_digest": patch_sha256("diff-2")}
    )


def test_opaque_key_is_trimmed_and_validated():
    key, derived = normalize_idempotency_key(
        "  retry-key  ",
        task_id="task",
        runner_id="runner",
        lease_token_hash="a" * 64,
        patch_digest="b" * 64,
    )
    assert key == "retry-key"
    assert derived is False

    derived_key, was_derived = normalize_idempotency_key(
        None,
        task_id="task",
        runner_id="runner",
        lease_token_hash="a" * 64,
        patch_digest="b" * 64,
    )
    assert was_derived is True
    assert len(derived_key) == 64

    with pytest.raises(SubmissionRejected):
        normalize_idempotency_key(
            "x" * 201,
            task_id="task",
            runner_id="runner",
            lease_token_hash="a" * 64,
            patch_digest="b" * 64,
        )
    with pytest.raises(SubmissionRejected):
        normalize_idempotency_key(
            "bad\x00key",
            task_id="task",
            runner_id="runner",
            lease_token_hash="a" * 64,
            patch_digest="b" * 64,
        )


def test_sandbox_environment_drops_database_and_admin_keys(tmp_path, monkeypatch):
    from app.services.gate_sandbox import sanitized_environment

    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("VN_ADMIN_API_KEY", "secret-admin")
    monkeypatch.setenv("VN_BOOTSTRAP_RUNNER_TOKEN", "secret-token")
    env, sandbox_tmp = sanitized_environment(tmp_path)
    try:
        assert "DATABASE_URL" not in env
        assert "VN_ADMIN_API_KEY" not in env
        assert "VN_BOOTSTRAP_RUNNER_TOKEN" not in env
        assert env["VEYNOD_SANDBOX"] == "1"
        assert env["PYTHONPATH"] == str(tmp_path)
    finally:
        import shutil

        shutil.rmtree(sandbox_tmp, ignore_errors=True)
