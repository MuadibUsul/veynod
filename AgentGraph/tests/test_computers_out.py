"""How host keys become computers on the Loop's Computers card."""

from __future__ import annotations

from app.services.runners import computers_out


def _host(host_id: str, agent_id: str, *, state: str, status: str = "active") -> dict:
    return {
        "host_id": host_id, "agent_id": agent_id, "token_id": f"tok-{host_id}",
        "hostname_digest": "abc123", "state": state, "status": status,
        "running": [], "max_concurrency": 1, "free_slots": 1 if state == "online" else 0,
        "last_seen_at": None, "os": "windows", "arch": "x64", "runner_version": "0.5.2",
        "runtimes": [{"id": "codex", "available": True}], "repos": [],
        "capabilities_report": None,
    }


ROLES = {"tok-w": "EXECUTOR", "tok-r": "AUDITOR"}


def test_a_kernel_computer_drops_its_leftover_review_seat() -> None:
    """No computer has a -review seat any more: an old revoked or silent one is not a fault."""
    for leftover in ({"state": "revoked", "status": "revoked"}, {"state": "offline"}):
        [computer] = computers_out([
            _host("w", "desk-5", state="online"),
            _host("r", "desk-review-3", **leftover),
        ], ROLES)
        assert [seat["seat"] for seat in computer["seats"]] == ["work"], leftover
        assert computer["state"] == "online"


def test_a_running_or_paused_review_seat_stays() -> None:
    """A seat still answering, or paused (it can be resumed), stays listed."""
    for state, status in (("online", "active"), ("paused", "paused")):
        [computer] = computers_out([
            _host("w", "desk-5", state="online"),
            _host("r", "desk-review-3", state=state, status=status),
        ], ROLES)
        assert [seat["seat"] for seat in computer["seats"]] == ["work", "review"], state
