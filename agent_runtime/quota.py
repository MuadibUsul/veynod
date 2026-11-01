"""Tenant-level runtime quotas (Phase 2J).

Run budgets are per-run; this policy is an additional ceiling over all runs of
a tenant.  The check is deliberately before a model call, so an over-quota
tenant does not spend more.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QuotaPolicy:
    max_model_calls: float | None = None
    max_prompt_tokens: float | None = None
    max_completion_tokens: float | None = None
    max_cost_usd: float | None = None

    def exceeded(self, usage: dict[str, Any]) -> str | None:
        checks = (
            ("max_model_calls", "model_calls"),
            ("max_prompt_tokens", "prompt_tokens"),
            ("max_completion_tokens", "completion_tokens"),
            ("max_cost_usd", "cost_usd"),
        )
        for limit_key, usage_key in checks:
            limit = getattr(self, limit_key)
            if limit is None:
                continue
            if float(usage.get(usage_key, 0) or 0) > float(limit):
                return f"tenant {usage_key} exceeded {limit_key}={limit}"
        return None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "QuotaPolicy":
        import os

        values = env or os.environ

        def _optional(name: str) -> float | None:
            raw = values.get(name, "").strip()
            return float(raw) if raw else None

        return cls(
            max_model_calls=_optional("RUNTIME_TENANT_MODEL_CALL_LIMIT"),
            max_prompt_tokens=_optional("RUNTIME_TENANT_PROMPT_TOKEN_LIMIT"),
            max_completion_tokens=_optional("RUNTIME_TENANT_COMPLETION_TOKEN_LIMIT"),
            max_cost_usd=_optional("RUNTIME_TENANT_COST_LIMIT_USD"),
        )


__all__ = ["QuotaPolicy"]
