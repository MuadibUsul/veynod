"""Context assembly for the Agent Runtime (Phase 2F).

Context is not one growing prompt string.  It is an ordered, budgeted set of
sections with stable references.  Large/low-priority sections are truncated or
omitted; the model always sees the references that survive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(
        value, default=str, separators=(",", ":")
    )
    return max(1, (len(text) + 3) // 4)


@dataclass
class ContextSection:
    kind: str
    ref: str
    content: Any
    priority: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "content": self.content,
            "priority": self.priority,
            "truncated": self.truncated,
        }


class ContextBuilder:
    def __init__(self, token_budget: int = 8_000) -> None:
        self.token_budget = max(128, token_budget)

    def build(
        self,
        *,
        run: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        extra: list[ContextSection] | None = None,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        budget = max(128, token_budget or self.token_budget)
        sections: list[ContextSection] = [
            ContextSection(
                "system_policy",
                "runtime/policy",
                "Be deterministic, use only allowed tools, respect budget and cancellation.",
                priority=100,
            ),
            ContextSection(
                "product_task",
                str(run.get("product_task_id") or run.get("run_id")),
                {
                    "run_id": run.get("run_id"),
                    "product_task_id": run.get("product_task_id"),
                    "goal": run.get("goal"),
                    "workflow_id": run.get("workflow_id"),
                    "workflow_version": run.get("workflow_version"),
                    "budget": run.get("budget") or {},
                    "usage": run.get("usage") or {},
                },
                priority=90,
            ),
        ]
        if checkpoint:
            sections.append(
                ContextSection("checkpoint", "run/checkpoint", checkpoint, priority=80)
            )
        if steps:
            sections.append(
                ContextSection(
                    "recent_steps",
                    "run/steps",
                    [
                        {
                            "step_no": step.get("step_no"),
                            "kind": step.get("kind"),
                            "status": step.get("status"),
                            "payload": step.get("payload") or {},
                        }
                        for step in steps[-20:]
                    ],
                    priority=60,
                )
            )
        sections.extend(extra or [])
        sections.sort(key=lambda section: (-section.priority, section.kind))

        included: list[ContextSection] = []
        omitted: list[str] = []
        used = 0
        for section in sections:
            remaining = budget - used
            if remaining <= 0:
                omitted.append(section.ref)
                continue
            tokens = estimate_tokens(section.content)
            if tokens <= remaining:
                included.append(section)
                used += tokens
                continue
            preview = section.content
            if not isinstance(preview, str):
                preview = json.dumps(preview, default=str, separators=(",", ":"))
            keep_chars = max(0, remaining * 4)
            if keep_chars <= 0:
                omitted.append(section.ref)
                continue
            truncated = ContextSection(
                section.kind,
                section.ref,
                preview[:keep_chars],
                section.priority,
                True,
            )
            included.append(truncated)
            used += estimate_tokens(truncated.content)
        return {
            "sections": [section.to_dict() for section in included],
            "token_estimate": used,
            "token_budget": budget,
            "omitted": omitted,
        }


__all__ = [
    "ContextBuilder",
    "ContextSection",
    "estimate_tokens",
]
