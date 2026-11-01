"""Veynod Durable Agent Runtime (Phase 2A foundation).

Owns Agent Run / Step / Checkpoint / Event state in the ``agent_runtime``
PostgreSQL schema.  Model Gateway and Tool Runtime are Phase 2B/2C.
"""

from .context import ContextBuilder, ContextSection
from .delegation import DelegationManager
from .gateway import (
    HTTPModelGateway,
    HTTPToolRuntime,
    ModelGatewayConfig,
    ToolGatewayConfig,
)
from .loop import LoopController, ModelAction, ToolResult
from .maintenance import ReconciliationLoop
from .pool import RuntimeWorkerPool
from .quota import QuotaPolicy
from .router import ModelRoute, RoutingModelGateway
from .states import ALLOWED, TERMINAL, RunState, validate_transition
from .store import RunStore
from .worker import RuntimeWorker

__all__ = [
    "ALLOWED",
    "TERMINAL",
    "ContextBuilder",
    "ContextSection",
    "DelegationManager",
    "HTTPModelGateway",
    "HTTPToolRuntime",
    "LoopController",
    "ModelAction",
    "ModelGatewayConfig",
    "ModelRoute",
    "QuotaPolicy",
    "ReconciliationLoop",
    "RoutingModelGateway",
    "RunState",
    "RunStore",
    "RuntimeWorker",
    "RuntimeWorkerPool",
    "ToolGatewayConfig",
    "ToolResult",
    "validate_transition",
]
