"""Upkeep steps run on their own: one that raises does not skip the others."""

from __future__ import annotations

from app.services import reaper


async def test_a_failing_step_is_named_and_the_rest_still_run(monkeypatch) -> None:
    ran: list[str] = []

    async def ok_first() -> None:
        ran.append("first")

    async def broken() -> None:
        raise RuntimeError("disk full")

    async def ok_last() -> None:
        ran.append("last")

    monkeypatch.setattr(reaper, "STEPS", (("first", ok_first), ("broken", broken),
                                          ("last", ok_last)))
    assert await reaper.run_once() == ["broken"]
    assert ran == ["first", "last"]
    assert reaper.last_errors["broken"] == "RuntimeError: disk full"
    monkeypatch.setattr(reaper, "STEPS", (("broken", ok_last),))
    assert await reaper.run_once() == []
    assert "broken" not in reaper.last_errors
