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


async def test_a_step_failing_three_rounds_tells_the_operator_once(monkeypatch) -> None:
    from app.services import alerts

    raised: list[dict] = []

    async def fake_raise(kind, message, **fields):
        raised.append({"kind": kind, "message": message, **fields})
        return {"alert_id": "alr_x"}

    monkeypatch.setattr(alerts, "raise_alert", fake_raise)
    monkeypatch.setattr(reaper, "health", {})
    broken = {"on": True}

    async def flaky() -> None:
        if broken["on"]:
            raise ConnectionError("kernel unreachable")

    monkeypatch.setattr(reaper, "STEPS", (("exports", flaky),))
    for _ in range(2):
        await reaper.run_once()
    assert raised == []  # one or two bad rounds are noise
    await reaper.run_once()
    await reaper.run_once()
    assert len(raised) == 1, raised
    alert = raised[0]
    assert alert["kind"] == "upkeep_step_failing" and "exports" in alert["message"]
    assert alert["details"]["failures"] == 3 and "kernel unreachable" in alert["details"]["error"]
    status = reaper.status()
    assert status["ok"] is False and status["steps"][0]["failures"] == 4

    broken["on"] = False
    await reaper.run_once()
    step = reaper.status()["steps"][0]
    assert step["ok"] and step["failures"] == 0 and step["last_ok_at"]
    assert step["last_error"] is None
    # A new outage is a new alert.
    broken["on"] = True
    for _ in range(3):
        await reaper.run_once()
    assert len(raised) == 2
    assert raised[0]["dedupe_key"] != raised[1]["dedupe_key"]


async def test_the_operator_reads_every_step(client) -> None:
    listed = await client.get("/v1/admin/upkeep", headers={"Authorization": "Bearer test-admin"})
    assert listed.status_code == 200, listed.text
    names = [item["step"] for item in listed.json()["steps"]]
    assert names == [name for name, _ in reaper.STEPS]
    refused = await client.get("/v1/admin/upkeep")
    assert refused.status_code in (401, 403)
