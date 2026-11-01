"""Agent Run state machine (Phase 2A).

This module contains only the state vocabulary and transition matrix.  It does
not execute models or tools; Phase 2B wires the Loop Controller on top.
"""

from __future__ import annotations

from enum import Enum

from .errors import InvalidTransition


class RunState(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    INITIALIZING = "INITIALIZING"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_MODEL = "WAITING_MODEL"
    WAITING_TOOL = "WAITING_TOOL"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    WAITING_CHILDREN = "WAITING_CHILDREN"
    BLOCKED = "BLOCKED"
    CHECKPOINTING = "CHECKPOINTING"
    REPLANNING = "REPLANNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


TERMINAL = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.TIMED_OUT,
        RunState.BUDGET_EXCEEDED,
    }
)

ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset(
        {RunState.QUEUED, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.QUEUED: frozenset(
        {RunState.INITIALIZING, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.INITIALIZING: frozenset(
        {RunState.PLANNING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.PLANNING: frozenset(
        {
            RunState.READY,
            RunState.REPLANNING,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.READY: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.WAITING_MODEL,
            RunState.WAITING_TOOL,
            RunState.WAITING_EXTERNAL,
            RunState.WAITING_CHILDREN,
            RunState.BLOCKED,
            RunState.CHECKPOINTING,
            RunState.REPLANNING,
            RunState.PAUSING,
            RunState.VERIFYING,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.TIMED_OUT,
            RunState.BUDGET_EXCEEDED,
        }
    ),
    RunState.WAITING_MODEL: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT}
    ),
    RunState.WAITING_TOOL: frozenset(
        {
            RunState.RUNNING,
            RunState.WAITING_EXTERNAL,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.TIMED_OUT,
        }
    ),
    RunState.WAITING_EXTERNAL: frozenset(
        {
            RunState.RUNNING,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.TIMED_OUT,
        }
    ),
    RunState.WAITING_CHILDREN: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.BLOCKED: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.CHECKPOINTING: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.REPLANNING: frozenset(
        {RunState.PLANNING, RunState.READY, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.PAUSING: frozenset(
        {RunState.PAUSED, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.PAUSED: frozenset(
        {RunState.RESUMING, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.RESUMING: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.VERIFYING: frozenset(
        {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
    ),
}


def validate_transition(current: str | RunState, target: str | RunState) -> None:
    try:
        source = current if isinstance(current, RunState) else RunState(current)
        destination = target if isinstance(target, RunState) else RunState(target)
    except ValueError as exc:
        raise InvalidTransition(str(current), str(target)) from exc
    if destination not in ALLOWED.get(source, frozenset()):
        raise InvalidTransition(source.value, destination.value)


__all__ = [
    "ALLOWED",
    "RunState",
    "TERMINAL",
    "validate_transition",
]
