"""Durable Agent Loop Controller (Phase 2B).

The controller never runs a model or tool inside an HTTP request.  Each
``step`` is one persisted decision: model invocation -> optional tool
invocation -> checkpoint.  Tool side effects are keyed by an idempotency key
and recorded in ``agent_runtime.invocations``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .context import ContextBuilder
from .errors import InvalidTransition, RunNotFound
from .quota import QuotaPolicy
from .store import RunStore

logger = logging.getLogger("agent_runtime.loop")


class ModelGateway(Protocol):
    async def decide(
        self, run: dict[str, Any], context: dict[str, Any]
    ) -> "ModelAction": ...


class ToolRuntime(Protocol):
    async def execute(
        self, name: str, arguments: dict[str, Any], idempotency_key: str
    ) -> "ToolResult": ...


@dataclass
class ModelAction:
    kind: str  # tool | finish | pause
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    output: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    reason: str = ""


@dataclass
class ToolResult:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def budget_exceeded(run: dict[str, Any]) -> str | None:
    budget = run.get("budget") or {}
    usage = run.get("usage") or {}
    checks = (
        ("max_model_calls", "model_calls"),
        ("max_prompt_tokens", "prompt_tokens"),
        ("max_completion_tokens", "completion_tokens"),
        ("max_cost_usd", "cost_usd"),
    )
    for limit_key, usage_key in checks:
        limit = budget.get(limit_key)
        if limit is None:
            continue
        if float(usage.get(usage_key, 0) or 0) > float(limit):
            return f"{usage_key} exceeded {limit_key}={limit}"
    return None


class LoopController:
    def __init__(
        self,
        store: RunStore,
        model: ModelGateway,
        tools: ToolRuntime,
        *,
        guard: Callable[[], Awaitable[None]] | None = None,
        context_builder: ContextBuilder | None = None,
        quota: QuotaPolicy | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.guard = guard
        self.context_builder = context_builder
        self.quota = quota

    async def _check_guard(self) -> None:
        if self.guard is not None:
            await self.guard()

    async def step(self, run_id: str) -> dict[str, Any]:
        run = await self.store.get_run(run_id)
        if run["status"] != "RUNNING":
            return {"run": run, "action": None, "executed": False}
        await self._check_guard()
        exceeded = budget_exceeded(run)
        if exceeded:
            await self.store.transition(
                run_id,
                "BUDGET_EXCEEDED",
                expected_version=run["version"],
                reason=exceeded,
            )
            await self._checkpoint(run_id, {"budget": exceeded})
            return {
                "run": await self.store.get_run(run_id),
                "action": None,
                "executed": False,
            }
        if self.quota is not None:
            tenant_usage = await self.store.tenant_usage(run["tenant_id"])
            quota_reason = self.quota.exceeded(tenant_usage)
            if quota_reason:
                await self.store.transition(
                    run_id,
                    "BUDGET_EXCEEDED",
                    expected_version=run["version"],
                    reason=quota_reason,
                )
                await self._checkpoint(run_id, {"tenant_quota": quota_reason})
                return {
                    "run": await self.store.get_run(run_id),
                    "action": None,
                    "executed": False,
                }

        step_no = await self.store.next_step_no(run_id)
        checkpoints = await self.store.list_checkpoints(run_id)
        checkpoint_state = checkpoints[-1]["state"] if checkpoints else {}
        if self.context_builder is not None:
            context = self.context_builder.build(
                run=run,
                checkpoint=checkpoint_state,
                steps=await self.store.list_steps(run_id, 50),
            )
        else:
            context = checkpoint_state
        action = await self.model.decide(run, context)
        if action.kind not in {"tool", "finish", "pause"}:
            raise ValueError(f"unsupported model action {action.kind!r}")

        await self._check_guard()
        model_step = await self.store.record_step(
            run_id,
            step_no,
            "MODEL",
            "SUCCEEDED",
            {"action": asdict(action)},
            idempotency_key=f"model:{run_id}:{step_no}",
        )
        await self.store.add_usage(
            run_id,
            model_calls=1,
            prompt_tokens=action.prompt_tokens,
            completion_tokens=action.completion_tokens,
            cost_usd=action.cost_usd,
        )
        run = await self.store.get_run(run_id)
        exceeded = budget_exceeded(run)
        if exceeded:
            await self.store.transition(
                run_id,
                "BUDGET_EXCEEDED",
                expected_version=run["version"],
                reason=exceeded,
            )
            await self._checkpoint(run_id, {"step": step_no, "budget": exceeded})
            return {
                "run": await self.store.get_run(run_id),
                "action": action,
                "executed": False,
            }
        if run["status"] != "RUNNING":
            return {"run": run, "action": action, "executed": False}

        if action.kind == "finish":
            await self._check_guard()
            await self.store.transition(
                run_id,
                "COMPLETED",
                expected_version=run["version"],
                reason=action.reason or "model finished",
            )
            await self._checkpoint(
                run_id, {"step": step_no, "result": action.output}
            )
            return {
                "run": await self.store.get_run(run_id),
                "action": action,
                "executed": True,
            }

        if action.kind == "pause":
            await self._check_guard()
            await self.store.transition(
                run_id,
                "PAUSING",
                expected_version=run["version"],
                reason=action.reason or "model requested pause",
            )
            run = await self.store.get_run(run_id)
            await self.store.transition(
                run_id,
                "PAUSED",
                expected_version=run["version"],
                reason=action.reason or "model requested pause",
            )
            await self._checkpoint(run_id, {"step": step_no, "paused": True})
            return {
                "run": await self.store.get_run(run_id),
                "action": action,
                "executed": False,
            }

        if not action.tool:
            raise ValueError("tool action requires a tool name")
        key = action.idempotency_key or f"tool:{run_id}:{step_no}"
        invocation = await self.store.begin_invocation(
            run_id,
            kind="tool",
            name=action.tool,
            idempotency_key=key,
            request=action.arguments,
            step_id=model_step["step_id"],
        )
        if invocation["status"] == "SUCCEEDED":
            return {
                "run": run,
                "action": action,
                "executed": False,
                "reused_invocation": invocation,
            }

        run = await self.store.get_run(run_id)
        if run["status"] != "RUNNING":
            return {"run": run, "action": action, "executed": False}
        await self._check_guard()
        await self.store.transition(
            run_id,
            "WAITING_TOOL",
            expected_version=run["version"],
            reason=f"tool {action.tool}",
        )
        result = await self.tools.execute(action.tool, action.arguments, key)
        await self._check_guard()
        await self.store.finish_invocation(
            invocation["invocation_id"],
            status="SUCCEEDED" if result.ok else "FAILED",
            result=result.output,
            error=result.error,
        )
        await self.store.record_step(
            run_id,
            step_no + 1,
            "TOOL",
            "SUCCEEDED" if result.ok else "FAILED",
            {
                "tool": action.tool,
                "arguments": action.arguments,
                "output": result.output,
                "error": result.error,
            },
            idempotency_key=f"tool-step:{key}",
        )
        run = await self.store.get_run(run_id)
        await self.store.transition(
            run_id,
            "RUNNING" if result.ok else "FAILED",
            expected_version=run["version"],
            reason=(
                f"tool {action.tool} completed"
                if result.ok
                else f"tool {action.tool} failed: {result.error or ''}"
            ),
        )
        await self._checkpoint(
            run_id,
            {
                "step": step_no,
                "tool": action.tool,
                "output": result.output,
                "ok": result.ok,
            },
        )
        return {
            "run": await self.store.get_run(run_id),
            "action": action,
            "executed": True,
            "tool_result": result,
        }

    async def _checkpoint(self, run_id: str, state: dict[str, Any]) -> None:
        await self.store.checkpoint(
            run_id,
            state,
            idempotency_key=f"checkpoint:{run_id}:{state.get('step')}:"
            f"{state.get('tool') or state.get('result') or state.get('paused') or state.get('budget')}",
        )


__all__ = [
    "LoopController",
    "ModelAction",
    "ModelGateway",
    "ToolResult",
    "ToolRuntime",
    "budget_exceeded",
]
