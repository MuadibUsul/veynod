"""Production safety checks for the Kernel.

The Kernel has historically shipped dev defaults (``change-me`` / ``dev-admin``)
and a ``local`` gate that executes agent code on the host.  Those are fine for
tests, but must never be accepted silently in production.
"""

from __future__ import annotations

import os
import posixpath
from typing import Any

WEAK_ADMIN_KEYS = frozenset(
    {
        "",
        "change-me",
        "dev-admin",
        "dev-admin-token-change-me",
        "agentgraph-dev-admin",
        "test-admin",
        "postgres",
        "password",
    }
)


class InsecureProductionConfig(RuntimeError):
    """Raised before the API/worker starts with unsafe production settings."""


def validate_api_production_settings(settings: Any) -> None:
    """The API plane does not execute agent code; it must have a strong key."""
    if getattr(settings, "auth_mode", "dev") != "prod":
        return
    key = str(getattr(settings, "admin_api_key", "") or "")
    if len(key) < 32 or key in WEAK_ADMIN_KEYS:
        raise InsecureProductionConfig(
            "VN_AUTH_MODE=prod requires VN_ADMIN_API_KEY with at least 32 "
            "random characters (known dev/default keys are rejected)."
        )


def validate_worker_production_settings(settings: Any) -> None:
    """The gate worker executes untrusted code; local mode must be explicit."""
    if getattr(settings, "auth_mode", "dev") != "prod":
        return
    if getattr(settings, "gate_sandbox", "local") == "local" and not getattr(
        settings, "allow_local_gate_in_prod", False
    ):
        raise InsecureProductionConfig(
            "VN_AUTH_MODE=prod refuses VN_GATE_SANDBOX=local; use docker or "
            "set VN_ALLOW_LOCAL_GATE_IN_PROD=true only for an isolated host."
        )
    if getattr(settings, "gate_sandbox", "local") == "docker":
        user = str(getattr(settings, "gate_docker_user", "") or "").strip()
        uid_gid = user.split(":")
        if (len(uid_gid) != 2 or not all(part.isdigit() for part in uid_gid)
                or any(int(part) == 0 for part in uid_gid)):
            raise InsecureProductionConfig(
                "VN_GATE_SANDBOX=docker in production requires "
                "VN_GATE_DOCKER_USER=<nonroot uid:gid> so tests do not run as root."
            )
        if getattr(settings, "gate_docker_network", "none") != "none":
            raise InsecureProductionConfig(
                "VN_GATE_SANDBOX=docker in production requires "
                "VN_GATE_DOCKER_NETWORK=none."
            )
        if not str(getattr(settings, "gate_sandbox_image", "") or "").strip():
            raise InsecureProductionConfig(
                "VN_GATE_SANDBOX=docker in production requires VN_GATE_SANDBOX_IMAGE."
            )
        if not posixpath.isabs(os.environ.get("VN_GATE_HOST_REPOS_DIR", "")):
            raise InsecureProductionConfig(
                "VN_GATE_SANDBOX=docker in production requires an absolute "
                "VN_GATE_HOST_REPOS_DIR."
            )


__all__ = [
    "InsecureProductionConfig",
    "WEAK_ADMIN_KEYS",
    "validate_api_production_settings",
    "validate_worker_production_settings",
]
