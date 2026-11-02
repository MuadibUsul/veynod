"""Product tasks execute on the Kernel, not on the host checkout.

The board creates a Kernel DAG task. The runner asks this module for a
worktree lease (``pull_task_bundle``: write-set lock plus the right to a
pooled worktree). Patches go to ``submit_patch``. ``verification_gate`` and
``merge_coordinator`` decide the outcome. The lease token is stored here and
is never copied onto a task the console can read.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger("agentgraph.kernel_delivery")

# The Kernel's six MCP/REST primitives. Submissions use submit_patch; the
# other four stay available to the same lease (split, escalate, inject, query).
PRIMITIVES = (
    "pull_task_bundle",
    "submit_patch",
    "request_task_split",
    "escalate_contract_defect",
    "inject_dependency_task",
    "query_symbol_signature",
)

_SECRET_KEYS = {"lease_token", "token", "authorization", "service_key"}
_NOT_WIRED = {"", "legacy", "unbound", "unconfigured"}


class KernelDeliveryError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, code: str = "kernel_delivery") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def write_set_for(task: Any) -> list[str]:
    """Symbols the product task already named. Empty means the Kernel cannot lock."""
    context = getattr(task, "input_context", None) or {}
    if not isinstance(context, dict):
        return []
    found: list[str] = []
    for key in ("write_set_symbols", "write_set", "target_symbols"):
        raw = context.get(key)
        if isinstance(raw, str) and raw.strip():
            found.append(raw.strip())
        elif isinstance(raw, list):
            found.extend(str(item).strip() for item in raw if str(item).strip())
    symbol = context.get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        found.append(symbol.strip())
    seen: dict[str, None] = {}
    for item in found:
        seen.setdefault(item, None)
    return list(seen)


def work_scope_for(task: Any) -> dict[str, Any] | None:
    """Carry the Diagram boundary unchanged; the Kernel canonicalizes it."""
    context = getattr(task, "input_context", None) or {}
    if not isinstance(context, dict):
        return None
    if isinstance(context.get("diagram"), dict):
        return {
            "scope_version": 1,
            "paths": {
                "include": context.get("allowed_write_paths") or [],
                "exclude": [],
            },
        }
    explicit = context.get("work_scope")
    return explicit if isinstance(explicit, dict) else None


def _scope_matches_delivery(task: Any, delivery: dict[str, Any]) -> bool:
    requested = work_scope_for(task)
    if requested is None:
        return True
    granted = delivery.get("work_scope")
    if not delivery.get("scope_hash") or not isinstance(granted, dict):
        return False
    context = getattr(task, "input_context", None) or {}
    if isinstance(context.get("diagram"), dict):
        # Compare exact source strings. A benign normalization difference may
        # require a new attempt, but a changed frozen scope can never reuse a
        # broader old lease by accident.
        return (
            sorted((granted.get("paths") or {}).get("include") or [])
            == sorted(requested["paths"]["include"])
            and not (granted.get("paths") or {}).get("exclude")
            and not (granted.get("symbols") or {}).get("include")
            and not (granted.get("symbols") or {}).get("exclude")
            and not (granted.get("resources") or {}).get("database_tables")
            and not (granted.get("resources") or {}).get("api_contracts")
            and not granted.get("protected_paths")
        )
    if requested.get("scope_hash"):
        return requested["scope_hash"] == delivery["scope_hash"]
    for section, names in (
        ("paths", ("include", "exclude")),
        ("symbols", ("include", "exclude")),
        ("resources", ("database_tables", "api_contracts")),
    ):
        for name in names:
            source = (requested.get(section) or {}).get(name) or []
            actual = (granted.get(section) or {}).get(name) or []
            if sorted(source) != sorted(actual):
                return False
    return sorted(requested.get("protected_paths") or []) == sorted(granted.get("protected_paths") or [])


def public_delivery(record: dict[str, Any] | None) -> dict[str, Any]:
    """Drop anything that could carry a lease token or a credential."""
    if not isinstance(record, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in _SECRET_KEYS or "token" in key.lower() or "secret" in key.lower():
            continue
        if isinstance(value, dict):
            out[key] = public_delivery(value)
        else:
            out[key] = value
    return out


def kernel_owns_steps(task: Any) -> bool:
    """Kernel gate and merge replace a second reviewer/merger computer."""
    correlation = getattr(task, "correlation", None) or {}
    if not isinstance(correlation, dict):
        return False
    delivery = correlation.get("kernel_delivery") or {}
    if not isinstance(delivery, dict):
        return False
    status = str(delivery.get("status") or "")
    return delivery.get("mode") == "kernel" and status not in _NOT_WIRED


def split_bundle(bundle: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Separate the public lease view from the token the server keeps."""
    token = bundle.get("lease_token")
    public = public_delivery(bundle)
    public.pop("lease_token", None)
    return public, str(token) if token else None


def fold_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Map Kernel evidence onto the fields the console and the runner read."""
    if not isinstance(evidence, dict) or evidence.get("error"):
        return {"gate": "unknown", "merged": False, "needs_human": False}
    task_status = str(evidence.get("task_status") or "")
    submission = evidence.get("submission") if isinstance(evidence.get("submission"), dict) else {}
    intent = evidence.get("merge_intent") if isinstance(evidence.get("merge_intent"), dict) else {}
    delivery = evidence.get("delivery") if isinstance(evidence.get("delivery"), dict) else {}
    results = evidence.get("gate_results") if isinstance(evidence.get("gate_results"), list) else []
    merge_sha = (
        intent.get("merged_commit_sha")
        or delivery.get("commit_sha")
        or evidence.get("result_commit_sha")
        or submission.get("result_commit_sha")
    )
    needs_human = task_status in {"SUSPENDED_ESCALATED", "ESCALATED"}
    if needs_human:
        gate = "escalated"
    elif results:
        gate = "pass" if all(
            isinstance(item, dict) and item.get("passed") is True for item in results
        ) else "fail"
    elif submission.get("status") in {"QUEUED", "RUNNING", "ACCEPTED", "VERIFYING"}:
        gate = "pending"
    else:
        gate = "absent"
    merged = bool(merge_sha) and (
        intent.get("status") == "MERGED"
        or evidence.get("evidence_complete") is True
        or task_status == "COMMITTED"
    )
    if merged:
        worktree, lease, status = "merged", "closed", "merged"
    elif gate == "fail":
        worktree, lease, status = "gate", "held", "gate_failed"
    elif gate == "escalated":
        worktree, lease, status = "gate", "held", "escalated"
    elif gate == "pending":
        worktree, lease, status = "gate", "held", "gate"
    elif gate == "pass":
        worktree, lease, status = "gate", "held", "gate_passed"
    else:
        worktree, lease, status = "leased", "held", "leased"
    return {
        "gate": gate,
        "merged": merged,
        "merge_sha": merge_sha,
        "needs_human": needs_human,
        "submission_id": submission.get("submission_id"),
        "submission_status": submission.get("status"),
        "worktree": worktree,
        "lease": lease,
        "status": status,
        "gate_results": results,
        "task_status": task_status,
    }


def _spec(task: Any) -> str:
    text_value = (getattr(task, "description", None) or getattr(task, "title", None) or "").strip()
    if len(text_value) < 10:
        text_value = f"{text_value} Implement this product task inside the leased write set.".strip()
    return text_value[:20_000]


class KernelPlane:
    """HTTP calls the Kernel already exposes. No new Kernel routes."""

    def __init__(
        self,
        base_url: str,
        workspace_id: str,
        *,
        service_key: str,
        runner_token: str,
        worker_hub_id: str = "veynod-runtime",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workspace_id = workspace_id
        self.service_key = service_key
        self.runner_token = runner_token
        self.worker_hub_id = worker_hub_id
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        service: bool,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"X-Workspace-Id": self.workspace_id}
        if service:
            if not self.service_key:
                raise KernelDeliveryError(
                    "KERNEL_SERVICE_KEY is not configured on the product API",
                    503,
                    "kernel_unconfigured",
                )
            headers["X-Veynod-Service-Key"] = self.service_key
        else:
            if not self.runner_token:
                raise KernelDeliveryError(
                    "KERNEL_RUNTIME_TOKEN is not configured on the product API",
                    503,
                    "kernel_unconfigured",
                )
            headers["Authorization"] = f"Bearer {self.runner_token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(
                method, self.base_url + path, headers=headers, json=json_body
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise KernelDeliveryError(f"Kernel unreachable: {exc}", 502, "kernel_unreachable") from exc
        payload: dict[str, Any] = {}
        if response.content:
            try:
                parsed = response.json()
                payload = parsed if isinstance(parsed, dict) else {"detail": parsed}
            except ValueError:
                payload = {"detail": response.text[:500]}
        if response.status_code >= 400:
            raise KernelDeliveryError(
                f"Kernel HTTP {response.status_code}: {_safe_detail(payload)}",
                502 if response.status_code >= 500 else response.status_code,
                "kernel_rejected",
            )
        return response.status_code, payload

    async def create_task(self, body: dict[str, Any]) -> dict[str, Any]:
        _status, payload = await self._request(
            "POST", "/v1/runtime/tasks", service=True, json_body=body,
            idempotency_key=str(body.get("request_key") or "") or None,
        )
        return payload

    async def pull(self, task_id: str) -> dict[str, Any]:
        _status, payload = await self._request(
            "POST",
            "/v1/syscalls/pull_task_bundle",
            service=False,
            json_body={
                "task_id": task_id,
                "worker_hub_id": self.worker_hub_id,
                "supported_task_types": [
                    "IMPLEMENT_BODY", "WRITE_TEST", "FIX_INTEGRATION",
                    "DESIGN_CONTRACT", "REVISE_CONTRACT",
                ],
                "max_context_tokens": 6000,
                "wait_seconds": 0,
            },
        )
        return payload

    async def submit(self, body: dict[str, Any], idempotency_key: str) -> tuple[int, dict[str, Any]]:
        return await self._request(
            "POST",
            "/v2/syscalls/submit_patch",
            service=False,
            json_body=body,
            idempotency_key=idempotency_key,
        )

    async def evidence(self, task_id: str) -> dict[str, Any]:
        _status, payload = await self._request(
            "GET", f"/v1/runtime/tasks/{task_id}/evidence", service=True
        )
        return payload


def plane_for(binding: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None) -> KernelPlane:
    settings = get_settings()
    credential = str(binding.get("credential_ref") or "KERNEL_RUNTIME_TOKEN")
    return KernelPlane(
        str(binding["base_url"]),
        str(binding["workspace_id"]),
        service_key=settings.kernel_service_key,
        runner_token=os.environ.get(credential, "").strip(),
        worker_hub_id=str(binding.get("worker_hub_id") or "veynod-runtime"),
        transport=transport,
    )


def _safe_detail(payload: dict[str, Any]) -> str:
    return str(public_delivery(payload))[:500]


async def _stamp(session: AsyncSession, task: Any, record: dict[str, Any]) -> dict[str, Any]:
    public = public_delivery(record)
    correlation = dict(getattr(task, "correlation", None) or {})
    if public.get("kernel_task_id"):
        correlation["kernel_task_id"] = public["kernel_task_id"]
    if public.get("kernel_workspace_id"):
        correlation["kernel_workspace_id"] = public["kernel_workspace_id"]
    if public.get("submission_id"):
        correlation["submission_id"] = public["submission_id"]
    if public.get("merge_sha"):
        correlation["commit_sha"] = public["merge_sha"]
    correlation["kernel_delivery"] = public
    task.correlation = correlation
    context = dict(getattr(task, "input_context", None) or {})
    if public.get("kernel_task_id"):
        context["code_engine"] = "veynod-kernel"
        kernel = dict(context.get("kernel") or {})
        kernel.update({
            "workspace_id": public.get("kernel_workspace_id") or kernel.get("workspace_id"),
            "task_id": public["kernel_task_id"],
        })
        if public.get("base_url"):
            kernel["base_url"] = public["base_url"]
        context["kernel"] = kernel
        if public.get("write_set"):
            context["write_set_symbols"] = list(public["write_set"])
        task.input_context = context
    await session.flush()
    return public


async def _remember_lease(
    session: AsyncSession,
    task: Any,
    *,
    kernel_task_id: str,
    token: str,
    write_set: list[str],
    base_commit_sha: str | None,
    expires_at: str | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO kernel_delivery_leases (
                workspace_id, task_key, kernel_task_id, lease_token, write_set,
                base_commit_sha, expires_at
            ) VALUES (
                :workspace_id, :task_key, CAST(:kernel_task AS UUID), :token,
                CAST(:write_set AS jsonb), :base, CAST(:expires AS timestamptz)
            )
            ON CONFLICT (workspace_id, task_key) DO UPDATE SET
                kernel_task_id = EXCLUDED.kernel_task_id,
                lease_token = EXCLUDED.lease_token,
                write_set = EXCLUDED.write_set,
                base_commit_sha = EXCLUDED.base_commit_sha,
                expires_at = EXCLUDED.expires_at,
                updated_at = NOW()
            """
        ),
        {
            "workspace_id": task.workspace_id,
            "task_key": task.task_key,
            "kernel_task": kernel_task_id,
            "token": token,
            "write_set": _json(write_set),
            "base": base_commit_sha,
            "expires": _expires(expires_at),
        },
    )


async def _load_token(session: AsyncSession, task: Any) -> str | None:
    token = await session.scalar(
        text(
            """
            SELECT lease_token FROM kernel_delivery_leases
            WHERE workspace_id = :workspace_id AND task_key = :task_key
            """
        ),
        {"workspace_id": task.workspace_id, "task_key": task.task_key},
    )
    return str(token) if token else None


def _json(value: Any) -> str:
    import json
    return json.dumps(value)


def _expires(value: str | None) -> str | None:
    if not value:
        return None
    text_value = str(value).replace("Z", "+00:00")
    try:
        datetime.fromisoformat(text_value)
    except ValueError:
        return None
    return text_value


def _blocked(status: str, error: str, *, write_set: list[str] | None = None) -> dict[str, Any]:
    return {
        "mode": "kernel",
        "status": status,
        "worktree": "none",
        "lease": "none",
        "leased": False,
        "write_set": write_set or [],
        "error": error,
        "primitives": list(PRIMITIVES),
    }


async def _binding(session: AsyncSession, task: Any) -> dict[str, Any] | None:
    from app.services.task_lifecycle import kernel_binding_for

    return await kernel_binding_for(session, task.workspace_id, task.project, task.repo)


async def provision_product_task(
    session: AsyncSession,
    task: Any,
    *,
    plane: KernelPlane | None = None,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create the Kernel task and take a worktree lease. No-op without a binding.

    Returns the public delivery record, or None when this repository is not
    wired to a Kernel (the row is left unchanged).
    """
    if not getattr(task, "repo", None):
        return None
    binding = binding if binding is not None else await _binding(session, task)
    if binding is None:
        return None
    write_set = write_set_for(task)
    work_scope = work_scope_for(task)
    if not write_set and work_scope is None:
        record = _blocked(
            "needs_write_set",
            "Set input_context.write_set_symbols before this task can take a Kernel lease.",
        )
        await _stamp(session, task, record)
        await session.commit()
        return record
    owned = plane is None
    plane = plane or plane_for(binding)
    try:
        created = await plane.create_task({
            "request_key": f"product:{task.task_key}"[:200],
            "title": str(task.title or task.task_key)[:256],
            "spec_prompt": _spec(task),
            "type": "IMPLEMENT_BODY",
            "target_symbols": [] if work_scope is not None else write_set,
            "test_symbols": _test_symbols(task),
            "write_set_symbols": [] if work_scope is not None else write_set,
            **({"work_scope": work_scope} if work_scope is not None else {}),
            "priority": int(getattr(task, "priority", None) or 100),
        })
        kernel_task_id = str(created.get("task_id") or "")
        if not kernel_task_id:
            raise KernelDeliveryError("Kernel created a task without task_id", 502)
        bundle = await plane.pull(kernel_task_id)
    finally:
        if owned:
            await plane.aclose()
    public, token = split_bundle(bundle)
    if not bundle.get("leased") or not token:
        record = {
            "mode": "kernel",
            "status": "admission" if bundle.get("admission_denied") else "lease_denied",
            "leased": False,
            "kernel_task_id": kernel_task_id,
            "kernel_workspace_id": binding["workspace_id"],
            "base_url": binding["base_url"],
            "write_set": write_set,
            "worktree": "pending",
            "lease": "denied",
            "retry_after_seconds": bundle.get("retry_after_seconds"),
            "primitives": list(PRIMITIVES),
            "error": "Kernel did not grant a worktree lease.",
        }
        await _stamp(session, task, record)
        await session.commit()
        return record
    granted_set = list(public.get("write_set") or write_set)
    await _remember_lease(
        session, task,
        kernel_task_id=kernel_task_id,
        token=token,
        write_set=granted_set,
        base_commit_sha=public.get("base_commit_sha"),
        expires_at=str(public.get("lease_expires_at") or "") or None,
    )
    record = {
        "mode": "kernel",
        "status": "leased",
        "leased": True,
        "kernel_task_id": kernel_task_id,
        "kernel_workspace_id": binding["workspace_id"],
        "base_url": binding["base_url"],
        "write_set": granted_set,
        "work_scope": public.get("work_scope"),
        "scope_hash": public.get("scope_hash"),
        "worktree": "leased",
        "lease": "held",
        "lease_expires_at": public.get("lease_expires_at"),
        "base_commit_sha": public.get("base_commit_sha"),
        "worktree_path": public.get("worktree_path"),
        "worktree_anchor": public.get("worktree_anchor"),
        "worktree_rel": public.get("worktree_rel"),
        "primitives": list(PRIMITIVES),
    }
    await _stamp(session, task, record)
    await session.commit()
    return record


def _test_symbols(task: Any) -> list[str]:
    context = getattr(task, "input_context", None) or {}
    if not isinstance(context, dict):
        return []
    raw = context.get("test_symbols") or context.get("test_symbol") or []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


async def open_lease(
    session: AsyncSession,
    task: Any,
    *,
    plane: KernelPlane | None = None,
) -> dict[str, Any]:
    """Lease endpoint: reuse a held lease, otherwise provision one."""
    current = (getattr(task, "correlation", None) or {}).get("kernel_delivery") or {}
    if (
        isinstance(current, dict)
        and current.get("lease") == "held"
        and current.get("kernel_task_id")
        and current.get("write_set")
        and await _load_token(session, task)
    ):
        if not _scope_matches_delivery(task, current):
            raise KernelDeliveryError(
                "The frozen WorkScope changed or this lease predates WorkScope; create a new attempt.",
                409, "scope_reissue_required",
            )
        return public_delivery(current)
    record = await provision_product_task(session, task, plane=plane)
    if record is None:
        raise KernelDeliveryError(
            "No Kernel binding for this repository. Set KERNEL_WORKSPACE_ID or a kernel binding.",
            409,
            "kernel_unbound",
        )
    if not record.get("leased"):
        raise KernelDeliveryError(
            str(record.get("error") or "Kernel worktree lease was not granted."),
            409,
            "kernel_lease_required",
        )
    return record


async def submit_diff(
    session: AsyncSession,
    task: Any,
    *,
    git_diff: str,
    touched_symbols: list[str],
    local_test_passed: bool,
    idempotency_key: str | None = None,
    plane: KernelPlane | None = None,
) -> dict[str, Any]:
    """Forward one diff through submit_patch. The host checkout is not touched."""
    if not git_diff or len(git_diff) < 10:
        raise KernelDeliveryError("git_diff must be a unified diff", 422, "validation")
    if not touched_symbols:
        raise KernelDeliveryError("touched_symbols is required", 422, "validation")
    delivery = (getattr(task, "correlation", None) or {}).get("kernel_delivery") or {}
    if not _scope_matches_delivery(task, delivery if isinstance(delivery, dict) else {}):
        raise KernelDeliveryError(
            "A scoped task cannot submit through an unscoped or stale Kernel lease.",
            409, "scope_reissue_required",
        )
    kernel_task_id = str(
        (delivery or {}).get("kernel_task_id")
        or (getattr(task, "correlation", None) or {}).get("kernel_task_id")
        or ""
    )
    token = await _load_token(session, task)
    if not kernel_task_id or not token:
        raise KernelDeliveryError(
            "No Kernel worktree lease is held. Lease one before submitting.",
            409,
            "kernel_lease_required",
        )
    binding = await _binding(session, task)
    if binding is None:
        raise KernelDeliveryError("Kernel binding disappeared", 409, "kernel_unbound")
    key = (idempotency_key or f"{task.task_key}:{kernel_task_id}")[:200]
    owned = plane is None
    plane = plane or plane_for(binding)
    try:
        status, payload = await plane.submit(
            {
                "task_id": kernel_task_id,
                "lease_token": token,
                "git_diff": git_diff,
                "touched_symbols": touched_symbols,
                "local_test_passed": bool(local_test_passed),
                "idempotency_key": key,
                "correlation": {
                    "product_task_id": task.task_key,
                    "kernel_task_id": kernel_task_id,
                },
            },
            key,
        )
    finally:
        if owned:
            await plane.aclose()
    submission_id = str(payload.get("submission_id") or "")
    record = {
        **public_delivery(delivery if isinstance(delivery, dict) else {}),
        "mode": "kernel",
        "status": "gate",
        "leased": True,
        "lease": "held",
        "worktree": "gate",
        "kernel_task_id": kernel_task_id,
        "kernel_workspace_id": binding["workspace_id"],
        "submission_id": submission_id,
        "submission_status": payload.get("status"),
        "gate": "pending",
        "primitives": list(PRIMITIVES),
    }
    await _stamp(session, task, record)
    await session.commit()
    return {
        "accepted": status == 202 or bool(submission_id),
        "submission_id": submission_id,
        "status": payload.get("status"),
        "kernel_task_id": kernel_task_id,
        "worktree": "gate",
        "lease": "held",
        "gate": "pending",
    }


async def refresh_delivery(
    session: AsyncSession,
    task: Any,
    *,
    plane: KernelPlane | None = None,
) -> dict[str, Any]:
    """Read gate and merge evidence and fold it onto the product task."""
    correlation = getattr(task, "correlation", None) or {}
    current = dict(correlation.get("kernel_delivery") or {}) if isinstance(correlation, dict) else {}
    kernel_task_id = str(current.get("kernel_task_id") or correlation.get("kernel_task_id") or "")
    if not kernel_task_id:
        return public_delivery(current) or {
            "mode": "kernel", "status": "absent", "worktree": "idle", "lease": "none", "leased": False,
        }
    binding = await _binding(session, task)
    if binding is None or plane is None and not get_settings().kernel_service_key:
        return public_delivery(current)
    owned = plane is None
    plane = plane or plane_for(binding)
    try:
        evidence = await plane.evidence(kernel_task_id)
    except KernelDeliveryError as exc:
        logger.warning("kernel evidence unavailable for %s: %s", kernel_task_id, exc)
        return public_delivery(current)
    finally:
        if owned:
            await plane.aclose()
    folded = fold_evidence(evidence)
    record = {
        **public_delivery(current),
        **{key: value for key, value in folded.items() if key != "gate_results"},
        "kernel_task_id": kernel_task_id,
        "kernel_workspace_id": current.get("kernel_workspace_id") or binding["workspace_id"],
        "mode": "kernel",
    }
    await _stamp(session, task, record)
    await session.commit()
    view = public_delivery(record)
    view["gate_results"] = folded.get("gate_results") or []
    return view


def kernel_section(delivery: dict[str, Any] | None, evidence: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Shape embedded in the task execution view."""
    public = public_delivery(delivery)
    if evidence:
        folded = fold_evidence(evidence)
        public = {**public, **{key: value for key, value in folded.items() if key != "gate_results"}}
        public["gate_results"] = folded.get("gate_results") or []
    if not public:
        return None
    return public
