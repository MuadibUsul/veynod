"""Agent Runtime errors and command outcomes."""

from __future__ import annotations


class RuntimeErrorBase(RuntimeError):
    pass


class InvalidTransition(RuntimeErrorBase):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"cannot transition from {current} to {target}")
        self.current = current
        self.target = target


class RunNotFound(RuntimeErrorBase):
    pass


class RunConflict(RuntimeErrorBase):
    pass


class LeaseLost(RuntimeErrorBase):
    pass


class IdempotencyConflict(RuntimeErrorBase):
    pass


__all__ = [
    "IdempotencyConflict",
    "InvalidTransition",
    "LeaseLost",
    "RunConflict",
    "RunNotFound",
    "RuntimeErrorBase",
]
