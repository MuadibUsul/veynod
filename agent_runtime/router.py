"""Provider routing with ordered fallback (Phase 2J)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .gateway import ModelGatewayError
from .loop import ModelAction, ModelGateway

logger = logging.getLogger("agent_runtime.router")


@dataclass
class ModelRoute:
    name: str
    gateway: ModelGateway
    priority: int = 0


class RoutingModelGateway:
    """Try a preferred/allowed provider first, then fall back in priority order."""

    def __init__(self, routes: list[ModelRoute]) -> None:
        if not routes:
            raise ValueError("RoutingModelGateway needs at least one route")
        self.routes = sorted(routes, key=lambda route: -route.priority)

    async def decide(self, run: dict[str, Any], context: dict[str, Any]) -> ModelAction:
        budget = run.get("budget") or {}
        preferred = budget.get("model_provider")
        allowed = set(budget.get("allowed_providers") or [])
        routes = [
            route for route in self.routes if not allowed or route.name in allowed
        ]
        if preferred:
            routes.sort(key=lambda route: 0 if route.name == preferred else 1)
        if not routes:
            raise ModelGatewayError("no model provider is allowed for this run")

        errors: list[str] = []
        for route in routes:
            try:
                return await route.gateway.decide(run, context)
            except ModelGatewayError as exc:
                errors.append(f"{route.name}: {exc}")
                logger.warning("model route %s failed: %s", route.name, exc)
                continue
        raise ModelGatewayError("all model providers failed: " + "; ".join(errors))


__all__ = ["ModelRoute", "RoutingModelGateway"]
