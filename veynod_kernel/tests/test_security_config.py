from __future__ import annotations

import pytest

from app.config import Settings
from app.security import (
    InsecureProductionConfig,
    validate_api_production_settings,
    validate_worker_production_settings,
)


def _settings(**kwargs) -> Settings:
    aliases = {
        "auth_mode": "VN_AUTH_MODE",
        "admin_api_key": "VN_ADMIN_API_KEY",
        "gate_sandbox": "VN_GATE_SANDBOX",
        "allow_local_gate_in_prod": "VN_ALLOW_LOCAL_GATE_IN_PROD",
        "gate_docker_user": "VN_GATE_DOCKER_USER",
        "gate_docker_network": "VN_GATE_DOCKER_NETWORK",
        "gate_sandbox_image": "VN_GATE_SANDBOX_IMAGE",
    }
    return Settings(
        _env_file=None, **{aliases.get(key, key): value for key, value in kwargs.items()}
    )


def test_production_api_rejects_weak_admin_key() -> None:
    settings = _settings(auth_mode="prod", admin_api_key="change-me")
    with pytest.raises(InsecureProductionConfig, match="ADMIN_API_KEY"):
        validate_api_production_settings(settings)


def test_production_api_rejects_short_admin_key() -> None:
    settings = _settings(auth_mode="prod", admin_api_key="x" * 31)
    with pytest.raises(InsecureProductionConfig, match="ADMIN_API_KEY"):
        validate_api_production_settings(settings)


def test_production_api_accepts_strong_key() -> None:
    settings = _settings(auth_mode="prod", admin_api_key="x" * 40)
    validate_api_production_settings(settings)


def test_production_worker_rejects_local_gate_by_default() -> None:
    settings = _settings(
        auth_mode="prod",
        gate_sandbox="local",
        allow_local_gate_in_prod=False,
    )
    with pytest.raises(InsecureProductionConfig, match="GATE_SANDBOX=local"):
        validate_worker_production_settings(settings)


def test_production_worker_allows_explicit_local_gate_opt_out() -> None:
    settings = _settings(
        auth_mode="prod",
        gate_sandbox="local",
        allow_local_gate_in_prod=True,
    )
    validate_worker_production_settings(settings)


def test_production_worker_accepts_hardened_docker(monkeypatch) -> None:
    monkeypatch.setenv("VN_GATE_HOST_REPOS_DIR", "/srv/veynod/repos")
    settings = _settings(
        auth_mode="prod",
        gate_sandbox="docker",
        gate_sandbox_image="veynod-kernel:test",
        gate_docker_user="65534:65534",
        gate_docker_network="none",
    )
    validate_worker_production_settings(settings)


def test_production_worker_rejects_root_docker() -> None:
    settings = _settings(auth_mode="prod", gate_sandbox="docker")
    with pytest.raises(InsecureProductionConfig, match="GATE_DOCKER_USER"):
        validate_worker_production_settings(settings)
    root = _settings(auth_mode="prod", gate_sandbox="docker",
                     gate_docker_user="0:0")
    with pytest.raises(InsecureProductionConfig, match="GATE_DOCKER_USER"):
        validate_worker_production_settings(root)


def test_production_worker_rejects_missing_host_repository_path(monkeypatch) -> None:
    monkeypatch.delenv("VN_GATE_HOST_REPOS_DIR", raising=False)
    settings = _settings(auth_mode="prod", gate_sandbox="docker",
                         gate_sandbox_image="veynod-kernel:test",
                         gate_docker_user="1000:1000")
    with pytest.raises(InsecureProductionConfig, match="GATE_HOST_REPOS_DIR"):
        validate_worker_production_settings(settings)


def test_dev_allows_local_gate_and_weak_key() -> None:
    settings = _settings(
        auth_mode="dev", admin_api_key="change-me", gate_sandbox="local"
    )
    validate_api_production_settings(settings)
    validate_worker_production_settings(settings)
