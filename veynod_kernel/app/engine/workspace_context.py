"""Request-scoped workspace for the single kernel process.

The 6 syscalls historically resolved a process-wide default workspace.  Multi
workspace routing needs the *request's* workspace instead, but threading a new
parameter through every engine method would touch the scheduler hot path for a
routing concern only.

A yield-dependency sets this context variable for the lifetime of the request
(after the token is validated and bound to exactly one workspace), and the
engine's ``_default_workspace_id`` prefers it over the deployment default.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator
from uuid import UUID

_REQUEST_WORKSPACE: ContextVar[UUID | None] = ContextVar(
    "veynod_syscall_workspace", default=None
)


def current_request_workspace() -> UUID | None:
    return _REQUEST_WORKSPACE.get()


@contextmanager
def request_workspace(workspace_id: UUID | None) -> Iterator[None]:
    token = _REQUEST_WORKSPACE.set(workspace_id)
    try:
        yield
    finally:
        _REQUEST_WORKSPACE.reset(token)


__all__ = ["current_request_workspace", "request_workspace"]
