"""Kernel tool runtime: the Agent Runtime's only way to execute code.

This adapter implements :class:`agent_runtime.loop.ToolRuntime` on top of the
Veynod Kernel syscall/runner planes.  It is deliberately strict:

* one explicit capability registry (no dynamic tool names from the model);
* argument validation before any request;
* workspace routing from the Run's Kernel binding (never a global default);
* Kernel lease tokens live in ``agent_runtime.kernel_leases`` and are redacted
  from model-visible output;
* asynchronous accept (HTTP 202) becomes a deferred result, so the loop leaves
  RUNNING and waits for a durable event instead of holding its lease;
* every call is classified (AUTH/VALIDATION/CONFLICT/ADMISSION/TRANSPORT/...)
  and audited with its trace correlation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..loop import ToolResult
from ..tool_context import ToolContext, current_tool_context

logger = logging.getLogger("agent_runtime.tools.kernel")

TOOL_NAMES = frozenset(
    {
        "create_task",
        "pull_task_bundle",
        "query_symbol_signature",
        "read_file",
        "submit_patch",
        "get_submission",
        "get_task_evidence",
        "get_task_status",
        "cancel_task",
        "request_task_split",
        "escalate_contract_defect",
        "inject_dependency_task",
    }
)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class KernelBinding:
    """Workspace-scoped Kernel endpoint and credential reference."""

    base_url: str
    workspace_id: str
    credential_ref: str = "KERNEL_RUNTIME_TOKEN"
    worker_hub_id: str = "veynod-runtime"

    def token(self) -> str:
        value = os.environ.get(self.credential_ref, "").strip()
        if not value:
            raise KernelToolConfigError(
                f"credential {self.credential_ref!r} is not configured"
            )
        return value


@dataclass(frozen=True)
class KernelToolConfig:
    default_binding: KernelBinding | None = None
    service_key: str = ""
    timeout_seconds: float = 60.0
    max_retries: int = 2
    allowed_tools: frozenset[str] = TOOL_NAMES
    submission_deadline_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "KernelToolConfig":
        base_url = os.environ.get("KERNEL_URL", "").strip()
        workspace_id = os.environ.get("KERNEL_WORKSPACE_ID", "").strip()
        binding = None
        if base_url and workspace_id:
            binding = KernelBinding(
                base_url=base_url,
                workspace_id=workspace_id,
                credential_ref=os.environ.get(
                    "KERNEL_TOKEN_ENV", "KERNEL_RUNTIME_TOKEN"
                ),
                worker_hub_id=os.environ.get(
                    "KERNEL_WORKER_HUB_ID", "veynod-runtime"
                ),
            )
        allowlist = os.environ.get("RUNTIME_KERNEL_TOOLS", "").strip()
        allowed = (
            frozenset(item.strip() for item in allowlist.split(",") if item.strip())
            if allowlist
            else TOOL_NAMES
        )
        unknown = allowed - TOOL_NAMES
        if unknown:
            raise KernelToolConfigError(
                f"unknown Kernel tools in RUNTIME_KERNEL_TOOLS: {sorted(unknown)}"
            )
        return cls(
            default_binding=binding,
            service_key=os.environ.get("KERNEL_SERVICE_KEY", ""),
            timeout_seconds=float(
                os.environ.get("RUNTIME_KERNEL_TIMEOUT_SECONDS", "60")
            ),
            max_retries=int(os.environ.get("RUNTIME_KERNEL_MAX_RETRIES", "2")),
            allowed_tools=allowed,
            submission_deadline_seconds=int(
                os.environ.get("RUNTIME_SUBMISSION_DEADLINE_SECONDS", "3600")
            ),
        )


class KernelToolConfigError(RuntimeError):
    pass


class KernelToolError(RuntimeError):
    def __init__(self, message: str, error_class: str) -> None:
        super().__init__(message)
        self.error_class = error_class


def classify_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "AUTH"
    if status_code == 404:
        return "NOT_FOUND"
    if status_code == 409:
        return "CONFLICT"
    if status_code == 422:
        return "VALIDATION"
    if status_code == 429:
        return "ADMISSION"
    if status_code >= 500:
        return "TRANSPORT"
    return "INTERNAL"


def _detail(response: httpx.Response) -> str:
    text = response.text[:800]
    try:
        payload = response.json()
    except ValueError:
        return text
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload
        return str(detail)[:800]
    return text


@dataclass
class _Capability:
    name: str
    mutating: bool
    description: str
    required: tuple[str, ...] = field(default_factory=tuple)


CAPABILITIES: dict[str, _Capability] = {
    "create_task": _Capability(
        "create_task",
        True,
        "Create the Kernel DAG node for this product task (idempotent).",
        ("title", "spec_prompt"),
    ),
    "pull_task_bundle": _Capability(
        "pull_task_bundle", True, "Lease a disjoint DAG task with AST-sliced context."
    ),
    "query_symbol_signature": _Capability(
        "query_symbol_signature",
        False,
        "Read 1-hop symbol signatures (no bodies).",
        ("task_id", "symbol_queries"),
    ),
    "read_file": _Capability(
        "read_file",
        False,
        "Read a workspace file at the current base commit.",
        ("path",),
    ),
    "submit_patch": _Capability(
        "submit_patch",
        True,
        "Submit a unified git diff for the leased task; 202 waits for the gate.",
        ("git_diff", "touched_symbols"),
    ),
    "get_submission": _Capability(
        "get_submission", False, "Read the durable status of a submission.",
        ("submission_id",),
    ),
    "get_task_evidence": _Capability(
        "get_task_evidence",
        False,
        "Read gate evidence and the merge result of a DAG task.",
        ("task_id",),
    ),
    "get_task_status": _Capability(
        "get_task_status", False, "Read a DAG task's internal status.",
        ("task_id",),
    ),
    "cancel_task": _Capability(
        "cancel_task", True, "Cancel a DAG task and its dependants.",
        ("task_id", "reason"),
    ),
    "request_task_split": _Capability(
        "request_task_split",
        True,
        "Split the leased task into an acyclic sub-DAG.",
        ("split_reason", "sub_tasks"),
    ),
    "escalate_contract_defect": _Capability(
        "escalate_contract_defect",
        True,
        "Report an upstream contract defect and propagate STALE.",
        (
            "defective_artifact_id",
            "defect_reason",
            "proposed_ast_signature",
            "breaking_symbol_paths",
        ),
    ),
    "inject_dependency_task": _Capability(
        "inject_dependency_task",
        True,
        "Insert a missing prerequisite task ahead of this one.",
        (
            "prerequisite_symbol_path",
            "prerequisite_specification",
            "prerequisite_write_set",
        ),
    ),
}


class KernelToolRuntime:
    """ToolRuntime implementation backed by the Veynod Kernel."""

    def __init__(
        self,
        config: KernelToolConfig,
        *,
        store: Any | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        binding_resolver: Callable[[ToolContext], KernelBinding | None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds, transport=transport
        )
        self._binding_resolver = binding_resolver or self._binding_from_run
        self._memory_leases: dict[tuple[str, str], str] = {}
        self._created_tasks: dict[str, str] = {}

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- execution
    async def execute(
        self, name: str, arguments: dict[str, Any], idempotency_key: str
    ) -> ToolResult:
        started = time.perf_counter()
        context = current_tool_context()
        try:
            if name not in self.config.allowed_tools:
                raise KernelToolError(f"tool {name!r} is not allowed", "AUTH")
            capability = CAPABILITIES.get(name)
            if capability is None:
                raise KernelToolError(f"unknown Kernel tool {name!r}", "VALIDATION")
            if not isinstance(arguments, dict):
                raise KernelToolError("arguments must be an object", "VALIDATION")
            missing = [
                key for key in capability.required if arguments.get(key) in (None, "")
            ]
            if missing:
                raise KernelToolError(
                    f"{name} is missing required arguments: {', '.join(missing)}",
                    "VALIDATION",
                )
            if context is None:
                raise KernelToolError(
                    "Kernel tool called outside of a Run context", "INTERNAL"
                )
            binding = self._binding_resolver(context)
            if binding is None:
                raise KernelToolError(
                    "no Kernel binding for this Run (project/repository is not mapped)",
                    "NOT_FOUND",
                )
            handler = getattr(self, f"_do_{name}")
            result = await handler(context, binding, arguments, idempotency_key)
        except KernelToolError as exc:
            result = ToolResult(
                False,
                output={"error": str(exc)},
                error=str(exc),
                error_class=exc.error_class,
            )
        except KernelToolConfigError as exc:
            result = ToolResult(
                False, output={"error": str(exc)}, error=str(exc),
                error_class="AUTH",
            )
        if self.store is not None:
            try:
                await self.store.record_tool_audit(
                    run_id=context.run_id if context else None,
                    tool=name,
                    idempotency_key=idempotency_key,
                    status="SUCCEEDED" if result.ok else "FAILED",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_class=result.error_class,
                    correlation=context.correlation() if context else {},
                )
            except Exception:  # noqa: BLE001 - audit must not hide the result
                logger.exception("kernel tool audit write failed")
        return result

    # ------------------------------------------------------------- resolution
    def _binding_from_run(self, context: ToolContext) -> KernelBinding | None:
        kernel = context.kernel or {}
        base_url = str(kernel.get("base_url") or "").strip()
        workspace_id = str(kernel.get("workspace_id") or "").strip()
        if base_url and workspace_id:
            return KernelBinding(
                base_url=base_url,
                workspace_id=workspace_id,
                credential_ref=str(
                    kernel.get("credential_ref") or "KERNEL_RUNTIME_TOKEN"
                ),
                worker_hub_id=str(
                    kernel.get("worker_hub_id")
                    or (self.config.default_binding.worker_hub_id
                        if self.config.default_binding else "veynod-runtime")
                ),
            )
        return self.config.default_binding

    # ----------------------------------------------------------------- calls
    async def _request(
        self,
        binding: KernelBinding,
        method: str,
        path: str,
        *,
        service: bool = False,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"X-Workspace-Id": binding.workspace_id}
        if service:
            if not self.config.service_key:
                raise KernelToolConfigError("KERNEL_SERVICE_KEY is not configured")
            headers["X-Veynod-Service-Key"] = self.config.service_key
        else:
            headers["Authorization"] = f"Bearer {binding.token()}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = binding.base_url.rstrip("/") + path

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=timeout or self.config.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            else:
                if response.status_code in RETRYABLE_STATUS and (
                    attempt < self.config.max_retries
                ):
                    last_error = KernelToolError(
                        f"Kernel HTTP {response.status_code}: {_detail(response)}",
                        "ADMISSION"
                        if response.status_code == 429
                        else "TRANSPORT",
                    )
                elif response.status_code >= 400:
                    raise KernelToolError(
                        f"Kernel HTTP {response.status_code}: {_detail(response)}",
                        classify_status(response.status_code),
                    )
                else:
                    try:
                        payload = response.json() if response.content else {}
                    except ValueError:
                        payload = {}
                    return response.status_code, payload
            await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
        raise KernelToolError(
            f"Kernel unreachable: {last_error}", "TRANSPORT"
        )

    async def _do_create_task(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        work_scope = context.kernel.get("work_scope")
        # One product task owns exactly one Kernel node.  The host path
        # (kernel_delivery) uses the same key, so a task provisioned for a host
        # and then started as a Run reuses that node instead of creating a
        # second one whose identical write set can never be leased.
        body = {
            "request_key": (
                f"product:{context.product_task_id}"[:200]
                if context.product_task_id
                else f"{context.run_id}:{idempotency_key}"[:200]
            ),
            "title": arguments["title"],
            "spec_prompt": arguments["spec_prompt"],
            "type": arguments.get("type") or "IMPLEMENT_BODY",
            "read_symbols": arguments.get("read_symbols") or [],
            "target_symbols": arguments.get("target_symbols") or [],
            "test_symbols": arguments.get("test_symbols") or [],
            "write_set_symbols": (
                [] if work_scope is not None
                else arguments.get("write_set_symbols") or []
            ),
            "priority": int(arguments.get("priority") or 100),
        }
        if work_scope is not None:
            body["work_scope"] = work_scope
        _, payload = await self._request(
            binding,
            "POST",
            "/v1/runtime/tasks",
            service=True,
            json_body=body,
            idempotency_key=idempotency_key,
        )
        task_id = str(payload.get("task_id") or "")
        if task_id:
            self._created_tasks[context.run_id] = task_id
            if self.store is not None:
                try:
                    await self.store.remember_kernel_task(
                        run_id=context.run_id,
                        kernel_task_id=task_id,
                        kernel_workspace_id=binding.workspace_id,
                        request_key=body["request_key"],
                    )
                except Exception:  # noqa: BLE001 - the next pull still names it
                    logger.exception("could not persist kernel task for %s", task_id)
        return ToolResult(True, output=payload)

    async def _do_pull_task_bundle(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        # Lease the node this Run created.  Leasing "any orthogonal task"
        # would let two product tasks in one repository take each other's work.
        requested = str(arguments.get("task_id") or "").strip()
        if not requested:
            requested = self._created_tasks.get(context.run_id) or ""
        if not requested and self.store is not None:
            row = await self.store.kernel_task(context.run_id)
            if row:
                requested = str(row["kernel_task_id"])
        if not requested:
            raise KernelToolError(
                "call create_task first: a Run leases the Kernel node it created",
                "VALIDATION",
            )
        body = {
            "task_id": requested,
            "worker_hub_id": binding.worker_hub_id,
            "supported_task_types": arguments.get("supported_task_types")
            or ["IMPLEMENT_BODY", "WRITE_TEST", "FIX_INTEGRATION"],
            "max_context_tokens": int(arguments.get("max_context_tokens") or 6000),
            "wait_seconds": float(arguments.get("wait_seconds") or 0.0),
        }
        _, payload = await self._request(
            binding, "POST", "/v1/syscalls/pull_task_bundle", json_body=body
        )
        if not payload.get("leased"):
            return ToolResult(
                True,
                output={
                    "leased": False,
                    "admission_denied": payload.get("admission_denied", False),
                    "retry_after_seconds": payload.get("retry_after_seconds"),
                    "queue_depth": payload.get("queue_depth") or {},
                },
            )
        task_id = str(payload.get("task_id") or "")
        token = payload.get("lease_token")
        if not task_id or not token:
            raise KernelToolError(
                "Kernel leased a task without task_id/lease_token", "INTERNAL"
            )
        await self._remember_lease(context, binding, task_id, str(token))
        payload = dict(payload)
        payload.pop("lease_token", None)
        payload["lease_token_present"] = True
        return ToolResult(True, output=payload)

    async def _do_query_symbol_signature(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        body = {
            "task_id": arguments["task_id"],
            "symbol_queries": arguments["symbol_queries"],
        }
        _, payload = await self._request(
            binding,
            "POST",
            "/v1/syscalls/query_symbol_signature",
            json_body=body,
        )
        return ToolResult(True, output=payload)

    async def _do_read_file(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        _, payload = await self._request(
            binding,
            "GET",
            f"/v1/runner/workspaces/{binding.workspace_id}/file",
            params={"path": arguments["path"]},
        )
        return ToolResult(True, output=payload)

    async def _do_submit_patch(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        task_id = str(arguments.get("task_id") or "")
        lease_token = str(arguments.get("lease_token") or "")
        if not task_id or not lease_token:
            task_id, lease_token = await self._lease_for(context, binding, task_id)
        body = {
            "task_id": task_id,
            "lease_token": lease_token,
            "git_diff": arguments["git_diff"],
            "touched_symbols": arguments["touched_symbols"],
            "local_test_passed": bool(arguments.get("local_test_passed", True)),
            "idempotency_key": idempotency_key,
            "correlation": context.correlation(),
        }
        status, payload = await self._request(
            binding,
            "POST",
            "/v2/syscalls/submit_patch",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        submission_id = str(payload.get("submission_id") or "")
        if status == 202 and submission_id:
            return ToolResult(
                True,
                output={
                    "accepted": True,
                    "submission_id": submission_id,
                    "status": payload.get("status"),
                    "status_url": payload.get("status_url"),
                    "deduplicated": payload.get("deduplicated", False),
                    "kernel_task_id": task_id,
                },
                deferred={
                    "submission_id": submission_id,
                    "kernel_workspace_id": binding.workspace_id,
                    "kernel_task_id": task_id,
                    "deadline_seconds": self.config.submission_deadline_seconds,
                },
            )
        # A legacy/synchronous Kernel answered inline.
        return self._submission_result(payload, task_id=task_id)

    async def _do_get_submission(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        _, payload = await self._request(
            binding,
            "GET",
            f"/v2/submissions/{arguments['submission_id']}",
        )
        return self._submission_result(payload)

    def _submission_result(
        self, payload: dict[str, Any], *, task_id: str | None = None
    ) -> ToolResult:
        status = str(payload.get("status") or "").upper()
        failed = status in {"FAILED", "REJECTED", "CANCELLED"}
        passed = status in {"COMMITTED", "MERGED", "COMPLETED"}
        output = dict(payload)
        if task_id:
            output.setdefault("kernel_task_id", task_id)
        if failed:
            return ToolResult(
                False,
                output=output,
                error=str(
                    payload.get("error_detail")
                    or payload.get("error_code")
                    or f"submission {status.lower()}"
                )[:800],
                error_class="GATE",
            )
        output["passed"] = passed
        return ToolResult(True, output=output)

    async def _do_get_task_evidence(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        _, payload = await self._request(
            binding,
            "GET",
            f"/v1/runner/tasks/{arguments['task_id']}/evidence",
        )
        return ToolResult(True, output=payload)

    async def _do_get_task_status(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        _, payload = await self._request(
            binding,
            "GET",
            f"/v1/runner/tasks/{arguments['task_id']}",
        )
        return ToolResult(True, output=payload)

    async def _do_cancel_task(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        _, payload = await self._request(
            binding,
            "POST",
            f"/v1/runtime/tasks/{arguments['task_id']}/cancel",
            service=True,
            json_body={"reason": arguments["reason"]},
            idempotency_key=idempotency_key,
        )
        return ToolResult(True, output=payload)

    async def _do_request_task_split(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        task_id = str(arguments.get("task_id") or "")
        lease_token = str(arguments.get("lease_token") or "")
        if not task_id or not lease_token:
            task_id, lease_token = await self._lease_for(context, binding, task_id)
        body = {
            "task_id": task_id,
            "lease_token": lease_token,
            "split_reason": arguments["split_reason"],
            "sub_tasks": arguments["sub_tasks"],
        }
        _, payload = await self._request(
            binding,
            "POST",
            "/v1/syscalls/request_task_split",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        return ToolResult(True, output=payload)

    async def _do_escalate_contract_defect(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        task_id = str(arguments.get("task_id") or "")
        lease_token = str(arguments.get("lease_token") or "")
        if not task_id or not lease_token:
            task_id, lease_token = await self._lease_for(context, binding, task_id)
        body = {
            "task_id": task_id,
            "lease_token": lease_token,
            "defective_artifact_id": arguments["defective_artifact_id"],
            "defect_reason": arguments["defect_reason"],
            "proposed_ast_signature": arguments["proposed_ast_signature"],
            "breaking_symbol_paths": arguments["breaking_symbol_paths"],
        }
        _, payload = await self._request(
            binding,
            "POST",
            "/v1/syscalls/escalate_contract_defect",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        return ToolResult(True, output=payload)

    async def _do_inject_dependency_task(
        self,
        context: ToolContext,
        binding: KernelBinding,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ToolResult:
        task_id = str(arguments.get("task_id") or "")
        lease_token = str(arguments.get("lease_token") or "")
        if not task_id or not lease_token:
            task_id, lease_token = await self._lease_for(context, binding, task_id)
        body = {
            "task_id": task_id,
            "lease_token": lease_token,
            "prerequisite_symbol_path": arguments["prerequisite_symbol_path"],
            "prerequisite_specification": arguments[
                "prerequisite_specification"
            ],
            "prerequisite_write_set": arguments["prerequisite_write_set"],
        }
        _, payload = await self._request(
            binding,
            "POST",
            "/v1/syscalls/inject_dependency_task",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        return ToolResult(True, output=payload)

    # ----------------------------------------------------------------- leases
    async def _remember_lease(
        self,
        context: ToolContext,
        binding: KernelBinding,
        task_id: str,
        token: str,
    ) -> None:
        key = (context.run_id, task_id)
        self._memory_leases[key] = token
        if self.store is None:
            return
        try:
            await self.store.remember_kernel_lease(
                run_id=context.run_id,
                kernel_task_id=task_id,
                kernel_workspace_id=binding.workspace_id,
                lease_token=token,
            )
        except Exception:  # noqa: BLE001 - persistence is best effort here
            logger.exception("could not persist kernel lease for %s", task_id)

    async def _lease_for(
        self, context: ToolContext, binding: KernelBinding, task_id: str
    ) -> tuple[str, str]:
        if task_id:
            token = self._memory_leases.get((context.run_id, task_id))
            if not token and self.store is not None:
                token = await self.store.kernel_lease(
                    context.run_id, task_id
                )
            if token:
                return task_id, token
        if self.store is not None:
            row = await self.store.latest_kernel_lease(context.run_id)
            if row is not None:
                return str(row["kernel_task_id"]), str(row["lease_token"])
        raise KernelToolError(
            "no Kernel lease for this Run; call pull_task_bundle first",
            "VALIDATION",
        )

    # ------------------------------------------------------------- visibility
    def describe(self) -> list[dict[str, Any]]:
        """Capability registry for prompts/UIs (no secrets, no endpoints)."""
        return [
            {
                "name": capability.name,
                "description": capability.description,
                "mutating": capability.mutating,
                "allowed": capability.name in self.config.allowed_tools,
            }
            for capability in CAPABILITIES.values()
        ]


__all__ = [
    "CAPABILITIES",
    "KernelBinding",
    "KernelToolConfig",
    "KernelToolConfigError",
    "KernelToolError",
    "KernelToolRuntime",
    "TOOL_NAMES",
    "classify_status",
]
