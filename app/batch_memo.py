"""Lookups the items of one batch write share.

A batch runs up to 25 writes for one principal, usually into one namespace.
Inside ``batch_scope()`` the lookups that depend only on those (the writer's
provenance context, path policies, namespace registration and readme) run
once per batch instead of once per item. Outside it they run every time.

A lookup may be shared only if no item of the batch can change its answer,
or if the item that does records the new answer with ``remember``. A failed
item of a non-atomic batch rolls back its savepoint, so the batch calls
``forget_all`` before the next item.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")

_memo: ContextVar[dict[Hashable, Any] | None] = ContextVar(
    "agentgraph_batch_memo", default=None
)


@contextmanager
def batch_scope() -> Iterator[None]:
    token = _memo.set({})
    try:
        yield
    finally:
        _memo.reset(token)


async def shared(key: Hashable, compute: Callable[[], Awaitable[T]]) -> T:
    memo = _memo.get()
    if memo is None:
        return await compute()
    if key not in memo:
        memo[key] = await compute()
    return memo[key]


def remember(key: Hashable, value: Any) -> None:
    memo = _memo.get()
    if memo is not None:
        memo[key] = value


def forget_all() -> None:
    memo = _memo.get()
    if memo is not None:
        memo.clear()
