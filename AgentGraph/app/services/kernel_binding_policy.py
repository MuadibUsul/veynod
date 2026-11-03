"""Trusted destinations and secret references for repository Kernel bindings."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from app.config import get_settings
from app.errors import ApiError


def validate_binding(base_url: str, credential_ref: str) -> tuple[str, str]:
    settings = get_settings()
    base = base_url.rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment):
        raise ApiError(422, "invalid_kernel_binding", "invalid Kernel base URL")
    allowed = {settings.kernel_url.rstrip("/")}
    try:
        configured = json.loads(settings.kernel_admin_keys or "{}")
        if isinstance(configured, dict):
            allowed.update(str(url).rstrip("/") for url in configured)
    except ValueError:
        pass
    allowed.update(url.strip().rstrip("/") for url in
                   os.environ.get("KERNEL_ALLOWED_BASE_URLS", "").split(",") if url.strip())
    if base not in allowed:
        raise ApiError(422, "invalid_kernel_binding", "Kernel URL is not configured")
    refs = {"KERNEL_RUNTIME_TOKEN"}
    refs.update(ref.strip() for ref in
                os.environ.get("KERNEL_ALLOWED_CREDENTIAL_REFS", "").split(",")
                if ref.strip())
    if credential_ref not in refs:
        raise ApiError(422, "invalid_kernel_binding", "credential reference is not configured")
    return base, credential_ref


def validate_workspace_assignment(
    product_workspace_id: str, kernel_workspace_id: str,
    base_url: str, credential_ref: str,
) -> None:
    """Only operator provisioned Kernel workspaces may be bound by owners."""
    base_url, credential_ref = validate_binding(base_url, credential_ref)
    try:
        assignments = json.loads(os.environ.get("KERNEL_WORKSPACE_BINDINGS", "{}"))
    except ValueError as exc:
        raise ApiError(503, "invalid_kernel_configuration",
                       "Kernel workspace assignments are invalid") from exc
    if not isinstance(assignments, dict):
        raise ApiError(503, "invalid_kernel_configuration",
                       "Kernel workspace assignments are invalid")
    settings = get_settings()
    allowed = (assignments.get(product_workspace_id, []) if assignments
               else [] if settings.environment == "production" else None)
    if allowed is None:
        allowed = [{"kernel_workspace_id": settings.kernel_workspace_id,
                    "kernel_base_url": settings.kernel_url,
                    "credential_ref": "KERNEL_RUNTIME_TOKEN"}]
    if not isinstance(allowed, list):
        raise ApiError(503, "invalid_kernel_configuration",
                       "Kernel workspace assignments are invalid")
    if not any(isinstance(item, dict)
               and item.get("kernel_workspace_id") == kernel_workspace_id
               and str(item.get("kernel_base_url", "")).rstrip("/") == base_url
               and item.get("credential_ref", "KERNEL_RUNTIME_TOKEN") == credential_ref
               for item in allowed):
        raise ApiError(403, "kernel_workspace_unassigned",
                       "Kernel workspace is not assigned to this workspace")
