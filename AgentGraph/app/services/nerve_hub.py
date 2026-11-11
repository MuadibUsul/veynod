"""The full-duplex nerve: one socket per agent, dispatch with two receipts.

The board used to poke through a one-way SSE stream and then trust TCP. A
half-open socket accepts writes for minutes, so "online" was a guess and a
dispatch could sit in a dead subscriber forever. Here:

* an agent holds ``/v1/ws/nerve`` open; the socket **is** presence, and it is
  owned by an in-memory map, not a sweep;
* a dispatch writes the task's ``lease_epoch`` forward, sends ``task.poke``
  carrying that epoch, and waits 200 ms for ``task.ack.net``. No receipt closes
  the socket and marks the agent offline: the peer is not reading;
* after the first receipt it waits for ``task.ack.exec`` (worker past
  orientation, pid attached) within 5 s. Timeout kills the worker tree through
  ``lease.revoked`` and reopens the task. A computer that cannot start the
  worker says so at once (``task.busy.reject`` with the reason), so the hub does
  not wait the timeout out to learn it;
* both receipts say a process is up, not that the placement is being worked:
  the worker then asks the board to start (``scheduler.start``), which checks
  the placement is still open and records ``exec_started_at``. Three times, kept
  apart: the computer read it, the worker is ready, the work began;
* the granted Product lease carries the epoch, so the fence is the same number
  the Kernel uses in spirit: a late holder with an older epoch is refused.

Rollback is defensive by epoch everywhere: the timeout path only rolls back
what this dispatch still owns, so a slow receipt cannot undo the next attempt.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import WebSocket
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.nerve_protocol import (
    NET_ACK_MAX_S,
    NerveEnvelope,
    NerveError,
    NerveFrameType,
    net_ack_deadline_s,
    orientation_timeout_ms,
    poke_frame,
    smooth_rtt_ms,
)

logger = logging.getLogger("agentgraph.nerve_hub")

#: Written to ``agent_presence.transport``; the presence sweep leaves rows at
#: or above this age alone because the socket owner removes its own row.
TRANSPORT = "ws_nerve"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms() -> int:
    return int(time.monotonic() * 1000)


def exec_ack_timeout_s() -> float:
    """The configured second-receipt budget; the poke's watchdog derives from it."""
    from app.config import get_settings

    return float(get_settings().nerve_exec_ack_timeout_seconds)


def exec_ack_deadline_s(rtt_ms: float) -> float:
    """The second-receipt wait: orientation, plus the worker's own hello round trips."""
    return exec_ack_timeout_s() + min(NET_ACK_MAX_S, 4 * max(0.0, rtt_ms) / 1000.0)


@dataclass
class ActiveSocket:
    workspace_id: Any
    agent_id: str
    host_id: str
    conn_id: str
    ws: WebSocket
    connected_at_ms: int = field(default_factory=_ms)
    last_seen_ms: int = field(default_factory=_ms)
    worker_busy: bool = False
    active_task_key: str | None = None
    active_lease_epoch: int | None = None
    #: How many placements this computer runs at once, and how many it runs now
    #: (from its pings, plus what this hub delivered since the last one).
    slots: int = 1
    active: int = 0
    #: A lane of its own for supervise rounds (0 on a CLI that has none): a long
    #: task in the work slot must not stop the Loop from being looked after.
    supervise_slots: int = 0
    supervising: int = 0
    #: The computer is letting its workers finish before it restarts (an update).
    draining: bool = False
    last_rtt_ms: int = 0
    #: Smoothed first-receipt round trip; sizes this socket's next deadlines.
    rtt_ms: float = 0.0
    expires_at: datetime | None = None
    closed: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.workspace_id), self.agent_id)


class NerveHub:
    """All live agents of this process with their pending receipts.

    One process owns an agent: a second connection with the same identity
    replaces the first (4000), so two listen loops never split a task's frames.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ActiveSocket] = {}
        self._net_acks: dict[str, asyncio.Future] = {}
        self._exec_acks: dict[str, asyncio.Future] = {}
        # frame id -> the connection it was sent on, so a closing socket fails
        # exactly its own pending receipts.
        self._owner: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- registry

    def get(self, workspace_id: Any, agent_id: str) -> ActiveSocket | None:
        return self._by_key.get((str(workspace_id), agent_id))

    def online_agents(self, workspace_id: Any) -> list[dict[str, Any]]:
        label = str(workspace_id)
        return [
            {"agent_id": sock.agent_id, "host_id": sock.host_id, "conn_id": sock.conn_id,
             "worker_busy": sock.worker_busy, "last_rtt_ms": sock.last_rtt_ms,
             "connected_at": datetime.fromtimestamp(
                 sock.connected_at_ms / 1000, tz=timezone.utc).isoformat()}
            for sock in self._by_key.values() if sock.key[0] == label
        ]

    async def register(self, sock: ActiveSocket) -> ActiveSocket:
        """Put ``sock`` live, replacing any previous socket of the same agent."""
        old: ActiveSocket | None
        async with self._lock:
            old = self._by_key.get(sock.key)
            self._by_key[sock.key] = sock
        if old is not None and old.conn_id != sock.conn_id:
            old.closed = True
            try:
                await old.ws.close(code=4000, reason="SUPERSEDED_BY_NEW_CONNECTION")
            except Exception:  # noqa: BLE001 - the new socket must not wait on the old one
                logger.debug("superseded socket did not close cleanly", exc_info=True)
        return sock

    async def unregister(self, sock: ActiveSocket, reason: str) -> bool:
        """Remove ``sock`` if it is still the live one. True when it was."""
        sock.closed = True
        async with self._lock:
            current = self._by_key.get(sock.key)
            if current is None or current.conn_id != sock.conn_id:
                return False
            del self._by_key[sock.key]
        # Dispatches waiting on the dead peer fail now, not at their deadline.
        for frame_id, future in list(self._net_acks.items()):
            if self._owner.get(frame_id) == sock.conn_id:
                self._fail(future, NerveError.NOT_CONNECTED.value)
        logger.info("agent %s left the nerve (conn=%s, %s)", sock.agent_id, sock.conn_id, reason)
        return True

    # ----------------------------------------------------------------- frames

    def on_frame(self, sock: ActiveSocket, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Fold one client frame into the pending receipts; a reply frame or None."""
        sock.last_seen_ms = _ms()
        frame_type, frame_id, payload = frame["type"], frame["frame_id"], frame["payload"]
        if frame_type == NerveFrameType.PING.value:
            try:
                sock.slots = max(1, int(payload.get("slots") or 1))
            except (TypeError, ValueError):
                sock.slots = 1
            count = payload.get("active_count")
            sock.active = (int(count) if isinstance(count, int) and count >= 0
                           else int(bool(payload.get("worker_busy", False))))
            sock.worker_busy = sock.active >= sock.slots
            lane = payload.get("supervise_slots")
            sock.supervise_slots = lane if isinstance(lane, int) and 0 <= lane <= 8 else 0
            rounds = payload.get("supervising")
            sock.supervising = rounds if isinstance(rounds, int) and rounds >= 0 else 0
            sock.draining = payload.get("draining") is True
            active = payload.get("active_task_key")
            sock.active_task_key = str(active) if active else None
            epoch = payload.get("lease_epoch")
            sock.active_lease_epoch = int(epoch) if isinstance(epoch, int) else None
            return NerveEnvelope(
                type=NerveFrameType.PONG,
                frame_id=frame_id or str(uuid.uuid4()),
                payload={"server_ts_ms": int(time.time() * 1000)},
            ).to_dict()
        if frame_type == NerveFrameType.LOGIN_UPDATE.value:
            # A subscription sign-in this computer runs for the console.
            from app.services import logins

            logins.on_update(sock, payload)
            return None
        if not frame_id:
            return None
        if frame_type == NerveFrameType.TASK_ACK_NET.value:
            self._resolve(self._net_acks, frame_id, payload)
        elif frame_type == NerveFrameType.TASK_ACK_EXEC.value:
            self._resolve(self._exec_acks, frame_id, payload)
        elif frame_type == NerveFrameType.TASK_BUSY_REJECT.value:
            reason = str(payload.get("reason") or NerveError.REJECTED.value)
            self._fail(self._net_acks.get(frame_id), f"{NerveError.REJECTED.value}: {reason}")
            self._fail(self._exec_acks.get(frame_id), f"{NerveError.REJECTED.value}: {reason}")
        return None

    @staticmethod
    def _resolve(pending: dict[str, asyncio.Future], frame_id: str,
                 payload: dict[str, Any]) -> None:
        future = pending.get(frame_id)
        if future is not None and not future.done():
            future.set_result(payload)

    @staticmethod
    def _fail(future: asyncio.Future | None, reason: str) -> None:
        if future is not None and not future.done():
            future.set_exception(RuntimeError(reason))

    # --------------------------------------------------------------- dispatch

    async def dispatch(
        self,
        sock: ActiveSocket,
        *,
        task_key: str,
        placement_id: str,
        title: str,
        step: str = "work",
        epoch: int,
        payload: dict[str, Any] | None = None,
        on_net_ack: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Send one poke and wait for both receipts.

        Returns ``(ok, reason, facts)``. ``facts`` holds the measured RTTs and
        the worker pid on success; the caller records them. The caller has
        already written ``epoch`` on the task, so a stale holder writes
        nowhere: every rollback below is ``AND lease_epoch = :epoch``.
        """
        if sock.closed:
            return False, NerveError.NOT_CONNECTED.value, {}
        if sock.draining:
            return False, NerveError.DRAINING.value, {}
        # A supervise round runs in its own lane when the computer has one.
        own_lane = step == "supervise" and sock.supervise_slots > 0
        if own_lane:
            if sock.supervising >= sock.supervise_slots:
                return False, NerveError.BUSY.value, {}
        elif sock.active >= sock.slots:
            return False, NerveError.BUSY.value, {}

        budget_s = exec_ack_timeout_s()
        frame = poke_frame(
            task_key=task_key, lease_epoch=epoch, placement_id=placement_id,
            title=title, step=step, **{
                **(payload or {}),
                # One budget for both ends: the messenger's watchdog fires before
                # this hub stops waiting, so its refusal arrives with the reason.
                "orientation_timeout_ms": orientation_timeout_ms(budget_s),
                "exec_ack_timeout_ms": int(budget_s * 1000),
            })
        frame.frame_id = placement_id or frame.frame_id
        loop = asyncio.get_running_loop()
        net_future: asyncio.Future = loop.create_future()
        exec_future: asyncio.Future = loop.create_future()
        self._net_acks[frame.frame_id] = net_future
        self._exec_acks[frame.frame_id] = exec_future
        self._owner[frame.frame_id] = sock.conn_id
        # The slot is taken from the moment the poke is sent; the next ping
        # says what the computer really runs.
        if own_lane:
            sock.supervising += 1
        else:
            sock.active += 1
            sock.worker_busy = sock.active >= sock.slots
            sock.active_task_key = frame.frame_id
        delivered = False

        started = time.perf_counter()
        net_received = False
        net_rtt_ms: int | None = None

        def failed() -> dict[str, Any]:
            # A refusal after the first receipt still measured the delivery.
            return {"frame_id": frame.frame_id, "net_acked": net_received,
                    "net_rtt_ms": net_rtt_ms}

        try:
            await sock.ws.send_json(frame.to_dict())
            net_ack = await asyncio.wait_for(net_future, timeout=net_ack_deadline_s(sock.rtt_ms))
            net_received = True
            net_rtt_ms = int((time.perf_counter() - started) * 1000)
            sock.last_rtt_ms = net_rtt_ms
            sock.rtt_ms = smooth_rtt_ms(sock.rtt_ms, net_rtt_ms)
            if on_net_ack is not None:
                await on_net_ack(net_rtt_ms)

            exec_ack = await asyncio.wait_for(exec_future, timeout=exec_ack_deadline_s(sock.rtt_ms))
            facts = {
                "net_rtt_ms": net_rtt_ms,
                "exec_startup_ms": int((time.perf_counter() - started) * 1000),
                "worker_pid": exec_ack.get("worker_pid"),
                # Whether a prepared worker took it, or one had to start cold.
                "warm": exec_ack.get("warm") is True,
                "frame_id": frame.frame_id,
                "net_ack": net_ack,
            }
            sock.active_lease_epoch = epoch
            delivered = True
            return True, "DISPATCHED", facts
        except asyncio.TimeoutError:
            # wait_for() cancels the future, so done() cannot tell the stages
            # apart; the flag can.
            stage = (NerveError.EXEC_ACK_TIMEOUT if net_received
                     else NerveError.NET_ACK_TIMEOUT)
            return False, stage.value, failed()
        except RuntimeError as exc:  # busy reject / socket gone
            return False, str(exc), failed()
        except Exception as exc:  # noqa: BLE001 - a send failure is a dispatch failure
            logger.warning("nerve dispatch failed for %s: %s", task_key, exc)
            return False, f"DISPATCH_ERROR: {exc}"[:200], failed()
        finally:
            self._net_acks.pop(frame.frame_id, None)
            self._exec_acks.pop(frame.frame_id, None)
            self._owner.pop(frame.frame_id, None)
            if not delivered:
                if own_lane:
                    sock.supervising = max(0, sock.supervising - 1)
                else:
                    sock.active = max(0, sock.active - 1)
                    sock.worker_busy = sock.active >= sock.slots
            if sock.active_task_key == frame.frame_id:
                sock.active_task_key = None

    async def revoke(self, sock: ActiveSocket, *, task_key: str, epoch: int,
                     reason: str, placement_id: str | None = None) -> bool:
        """Tell the messenger to kill the worker tree for this lease.

        ``placement_id`` names the one worker of a step that has no task (a
        supervise round), which the task key and epoch cannot.
        """
        sock.active_task_key = None
        sock.active_lease_epoch = None
        try:
            await sock.ws.send_json(NerveEnvelope(
                type=NerveFrameType.LEASE_REVOKED,
                payload={"task_key": task_key, "lease_epoch": epoch, "reason": reason,
                         **({"placement_id": placement_id} if placement_id else {})},
            ).to_dict())
            return True
        except Exception:  # noqa: BLE001 - a dead socket is already revoked in the DB
            return False


#: One hub per process; the socket route and the dispatcher share it.
nerve_hub = NerveHub()


# ------------------------------------------------------------------ dispatch


def dispatchable(task: Any) -> bool:
    """Whether a task may be dispatched now, judged by what holds it.

    ``AWAITING_*`` is another dispatch in flight. ``ACTIVE_LEASE`` only blocks
    while a computer really holds the task: once it is READY again, or its lease
    is gone or expired, that state is a leftover of the previous dispatch. It
    used to block every later one ("already dispatching"), so a task that came
    back could never be placed again.
    """
    state = task["dispatch_state"]
    if state == "IDLE":
        return True
    if state != "ACTIVE_LEASE":
        return False
    held = (task["status"] == "IN_PROGRESS" and task["lease_owner"] is not None
            and bool(task["lease_live"]))
    return not held


async def deliver_task_poke(
    session: AsyncSession,
    *,
    workspace_id: Any,
    task_key: str,
    placement_id: str,
    epoch: int,
    title: str,
    step: str = "work",
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Atomically place one poke on the agent's nerve socket.

    Writes the task's ``dispatch_state`` through AWAITING_NET_ACK →
    AWAITING_EXEC_ACK → ACTIVE_LEASE (or back to IDLE), records a
    ``poke_deliveries`` row keyed by the frame, and on accept grants the
    Product lease carrying ``epoch``.

    ``commit=False`` leaves the caller's transaction open (the scheduler runs
    inside one); everything written here then rolls back with it.
    """
    sock = nerve_hub.get(workspace_id, payload_agent(payload))
    if sock is None:
        return {"dispatched": False, "reason": NerveError.NOT_CONNECTED.value}

    placement = (await session.execute(text(
        "SELECT agent_id, host_id FROM runner_placements WHERE placement_id = :p"),
        {"p": placement_id})).mappings().one_or_none()
    del placement  # the socket is authoritative; the placement is audit only

    task = (await session.execute(text(
        "SELECT t.id, t.task_key, t.status::text AS status, t.dispatch_state, t.title, "
        "t.assignee, t.claimed_by, t.lease_epoch, t.workspace_id, t.lease_owner, "
        "(t.lease_expires_at IS NOT NULL AND t.lease_expires_at > NOW()) AS lease_live "
        "FROM collab_tasks t WHERE t.workspace_id = :ws AND t.task_key = :k FOR UPDATE"),
        {"ws": workspace_id, "k": task_key})).mappings().one_or_none()
    if task is None:
        return {"dispatched": False, "reason": "TASK_NOT_FOUND"}
    if not dispatchable(task):
        return {"dispatched": False, "reason": "TASK_ALREADY_DISPATCHING"}
    if task["status"] not in ("READY", "TODO", "IN_PROGRESS"):
        return {"dispatched": False, "reason": f"TASK_NOT_CLAIMABLE:{task['status']}"}
    if int(task["lease_epoch"] or 0) > int(epoch):
        return {"dispatched": False, "reason": "STALE_EPOCH"}

    await session.execute(text(
        # The reaper reopens a dispatch past this deadline, so it must not be
        # shorter than the hub's own wait for this socket (plus a margin).
        "UPDATE collab_tasks SET lease_epoch = :e, dispatch_state = 'AWAITING_NET_ACK', "
        "ack_deadline_at = NOW() + make_interval(secs => :wait), updated_at = NOW() "
        "WHERE id = :id"),
        {"e": epoch, "id": task["id"], "wait": net_ack_deadline_s(sock.rtt_ms) + 0.05})
    await session.execute(text(
        "INSERT INTO poke_deliveries (workspace_id, event_seq, recipient, channel, status, "
        "detail, lease_epoch, qos_status, task_key, frame_id) "
        # A nerve poke has no event seq. The table's older uniqueness is on
        # (workspace, event_seq, recipient, channel), so a constant 0 let only the
        # first poke to a computer leave a row: every later one had no record,
        # was sent again each second and could not be stamped. A negative number
        # of its own keeps that index out of the way; the frame index dedupes.
        "VALUES (:ws, (SELECT -nextval(pg_get_serial_sequence('poke_deliveries', 'id'))), :r, 'nerve', 'delivered', 'sent', :e, 'SENT', :k, :f) "
        # The frame index is partial, so name no target: both its uniqueness and
        # the old event_seq one then apply, and a retried frame stays one row.
        "ON CONFLICT DO NOTHING"),
        {"ws": workspace_id, "r": f"agent:{payload_agent(payload)}", "e": epoch,
         "k": task_key, "f": placement_id})
    if commit:
        await session.commit()

    async def record_net_ack(rtt_ms: int) -> None:
        await session.execute(text(
            "UPDATE collab_tasks SET dispatch_state = 'AWAITING_EXEC_ACK', "
            "ack_deadline_at = NOW() + make_interval(secs => :wait) "
            "WHERE id = :id AND lease_epoch = :e"),
            {"id": task["id"], "e": epoch, "wait": exec_ack_deadline_s(sock.rtt_ms) + 0.05})
        await session.execute(text(
            "UPDATE poke_deliveries SET qos_status = 'NET_ACKED', net_ack_at = NOW(), "
            "net_rtt_ms = :rtt WHERE workspace_id = :ws AND frame_id = :f"),
            {"rtt": rtt_ms, "ws": workspace_id, "f": placement_id})
        await session.commit()

    ok, reason, facts = await nerve_hub.dispatch(
        sock, task_key=task_key, placement_id=placement_id, title=title,
        step=step, epoch=epoch, payload=payload, on_net_ack=record_net_ack)

    if not ok:
        await _rollback(session, sock, workspace_id=workspace_id, task_key=task_key,
                        task_id=task["id"], epoch=epoch, frame_id=placement_id,
                        reason=reason, kick=reason == NerveError.NET_ACK_TIMEOUT.value,
                        net_rtt_ms=facts.get("net_rtt_ms") if facts.get("net_acked") else None)
        return {"dispatched": False, "reason": reason, **facts}

    await session.execute(text(
        "UPDATE collab_tasks SET dispatch_state = 'ACTIVE_LEASE', ack_deadline_at = NULL, "
        "status = CASE WHEN status = 'IN_PROGRESS' THEN status "
        "ELSE 'IN_PROGRESS'::task_status END, "
        "claimed_by = COALESCE(claimed_by, :agent), lease_owner = :agent, "
        "lease_expires_at = NOW() + make_interval(secs => :ttl), updated_at = NOW() "
        "WHERE id = :id AND lease_epoch = :e"),
        # The holder is the computer's agent signature, the same string a claim
        # by that agent uses. "agent:<id>" made the agent's own claim a 409.
        {"id": task["id"], "e": epoch, "agent": payload_holder(payload),
         "ttl": _lease_ttl()})
    # The Product lease row is the addressable record; the epoch write above
    # already ended the previous one through the v3_15 trigger.
    await session.execute(text(
        "INSERT INTO leases (workspace_id, resource_kind, resource_id, resource_ref, "
        "fencing_token, holder, acquired_at, expires_at, ttl_seconds, heartbeat_interval_s, "
        "last_heartbeat_at) "
        "SELECT t.workspace_id, 'task', t.id, t.canonical_id, t.lease_epoch, t.lease_owner, "
        "NOW(), t.lease_expires_at, :ttl, GREATEST(1, :ttl / 3), NOW() "
        "FROM collab_tasks t WHERE t.id = :id"),
        {"ttl": _lease_ttl(), "id": task["id"]})
    await session.execute(text(
        "UPDATE poke_deliveries SET qos_status = 'EXEC_ACKED', "
        "net_ack_at = COALESCE(net_ack_at, NOW()), "
        "exec_ack_at = NOW(), net_rtt_ms = :rtt, exec_startup_ms = :startup, warm = :warm "
        "WHERE workspace_id = :ws AND frame_id = :f"),
        {"rtt": facts["net_rtt_ms"], "startup": facts["exec_startup_ms"],
         "warm": bool(facts.get("warm")), "ws": workspace_id, "f": placement_id})
    if commit:
        await session.commit()
    logger.info(
        "nerve dispatch %s -> %s epoch=%s net=%sms exec=%sms",
        task_key, payload_agent(payload), epoch, facts["net_rtt_ms"],
        facts["exec_startup_ms"])
    return {"dispatched": True, "reason": "DISPATCHED", **facts}


async def deliver_step_poke(
    session: AsyncSession,
    *,
    workspace_id: Any,
    placement_id: str,
    agent_id: str,
    step: str,
    task_key: str | None,
    title: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deliver a review, merge or supervise placement: the same two receipts as work.

    Unlike work, a step holds no task lease, so a failed delivery has nothing to
    roll back on the task; the caller ends the placement and the next
    scheduling pass places the step again.
    """
    sock = nerve_hub.get(workspace_id, agent_id)
    if sock is None:
        return {"dispatched": False, "reason": NerveError.NOT_CONNECTED.value}
    await session.execute(text(
        "INSERT INTO poke_deliveries (workspace_id, event_seq, recipient, channel, status, "
        "detail, lease_epoch, qos_status, task_key, frame_id) "
        "VALUES (:ws, (SELECT -nextval(pg_get_serial_sequence('poke_deliveries', 'id'))), :r, 'nerve', 'delivered', 'sent', 0, 'SENT', :k, :f) "
        "ON CONFLICT DO NOTHING"),
        {"ws": workspace_id, "r": f"agent:{agent_id}", "k": task_key, "f": placement_id})
    await session.commit()
    ok, reason, facts = await nerve_hub.dispatch(
        sock, task_key=task_key or "", placement_id=placement_id, title=title or step,
        step=step, epoch=0, payload={"agent_id": agent_id, **(extra or {})})
    acked = ok or bool(facts.get("net_acked"))
    await session.execute(text(
        "UPDATE poke_deliveries SET qos_status = :q, detail = :d, net_rtt_ms = :rtt, "
        "net_ack_at = CASE WHEN :acked THEN NOW() ELSE net_ack_at END, "
        "exec_ack_at = CASE WHEN :ok THEN NOW() ELSE exec_ack_at END, "
        "exec_startup_ms = :startup, warm = :warm "
        "WHERE workspace_id = :ws AND frame_id = :f"),
        {"q": "EXEC_ACKED" if ok else "TIMEOUT_REVOKED", "d": reason[:500],
         "rtt": facts.get("net_rtt_ms") if acked else None, "acked": acked, "ok": ok,
         "startup": facts.get("exec_startup_ms"), "warm": facts.get("warm") if ok else None,
         "ws": workspace_id, "f": placement_id})
    await session.commit()
    if not ok and acked:
        # The computer read the poke but no worker came up in time: a worker
        # that comes up late must not start a round the board already moved.
        await nerve_hub.revoke(sock, task_key=task_key or "", epoch=0, reason=reason,
                               placement_id=placement_id)
    if not ok and reason == NerveError.NET_ACK_TIMEOUT.value:
        await mark_offline(session, sock, reason)
    return {"dispatched": ok, "reason": reason, **facts}


async def record_exec_started(session: AsyncSession, *, workspace_id: Any,
                              placement_id: str) -> None:
    """The third time: the placement was checked and work on it began.

    Measured on the board's clock from the moment the poke was written, like
    the two receipts, so the three can be compared. Recorded once.
    """
    await session.execute(text(
        "UPDATE poke_deliveries SET exec_started_at = NOW(), "
        "exec_started_ms = GREATEST(0, (EXTRACT(EPOCH FROM (NOW() - at)) * 1000)::int) "
        "WHERE workspace_id = :ws AND frame_id = :f AND exec_started_at IS NULL"),
        {"ws": workspace_id, "f": placement_id})


async def _rollback(
    session: AsyncSession,
    sock: ActiveSocket,
    *,
    workspace_id: Any,
    task_key: str,
    task_id: Any,
    epoch: int,
    frame_id: str,
    reason: str,
    kick: bool,
    net_rtt_ms: int | None = None,
) -> None:
    """Reopen the task only if this dispatch still owns it, and revoke the worker."""
    await nerve_hub.revoke(sock, task_key=task_key, epoch=epoch, reason=reason,
                           placement_id=frame_id)
    await session.execute(text(
        "UPDATE collab_tasks SET dispatch_state = 'IDLE', ack_deadline_at = NULL, "
        "status = CASE WHEN status = 'IN_PROGRESS' THEN 'READY'::task_status ELSE status END, "
        "claimed_by = NULL, lease_owner = NULL, lease_expires_at = NULL, updated_at = NOW() "
        "WHERE id = :id AND lease_epoch = :e"), {"id": task_id, "e": epoch})
    await session.execute(text(
        "UPDATE poke_deliveries SET qos_status = 'TIMEOUT_REVOKED', detail = :d, "
        "net_rtt_ms = COALESCE(:rtt, net_rtt_ms) "
        "WHERE workspace_id = :ws AND frame_id = :f"),
        {"d": reason[:500], "rtt": net_rtt_ms, "ws": workspace_id, "f": frame_id})
    await session.commit()
    if kick:
        await mark_offline(session, sock, reason)


async def mark_offline(session: AsyncSession, sock: ActiveSocket, reason: str) -> None:
    """The socket did not read: take the agent offline at once."""
    await session.execute(text(
        "UPDATE agent_presence SET status = 'offline', worker_busy = FALSE "
        "WHERE workspace_id = :ws AND active_conn_id = CAST(:c AS UUID)"),
        {"ws": sock.workspace_id, "c": sock.conn_id})
    await session.commit()
    try:
        await sock.ws.close(code=4008, reason="QOS_NET_ACK_TIMEOUT")
    except Exception:  # noqa: BLE001
        pass
    logger.warning("agent %s offline: %s", sock.agent_id, reason)


def payload_agent(payload: dict[str, Any] | None) -> str:
    return str((payload or {}).get("agent_id") or "")


def payload_holder(payload: dict[str, Any] | None) -> str:
    """Who holds a lease granted on the agent's behalf: its token's signature."""
    return str((payload or {}).get("holder") or payload_agent(payload))


def _lease_ttl() -> int:
    from app.config import get_settings

    return int(get_settings().lease_default_ttl_seconds)


async def presence_row(session: AsyncSession, *, workspace_id: Any, token_id: str,
                       agent_id: str, host_id: str, conn_id: str) -> None:
    """Upsert the presence row for a fresh socket (agent identity, host beside it).

    ``token_id`` is the token's canonical id because presence joins on the
    directory entry's token list; ``instance`` is per connection so two sockets
    of one token (a busy host) do not overwrite each other.
    """
    await session.execute(text(
        "INSERT INTO agent_presence (workspace_id, token_id, agent_ref, instance, transport, "
        "host_id, agent_id, active_conn_id, transport_kind, status, worker_busy) "
        "VALUES (:ws, :t, :a, :i, 'ws_nerve', :h, :a, CAST(:c AS UUID), 'ws_nerve', "
        "'online', FALSE) "
        "ON CONFLICT (token_id, instance) DO UPDATE SET "
        "host_id = EXCLUDED.host_id, agent_id = EXCLUDED.agent_id, "
        "active_conn_id = EXCLUDED.active_conn_id, status = 'online', "
        "transport_kind = 'ws_nerve', worker_busy = FALSE, seen_at = NOW()"),
        {"ws": workspace_id, "t": token_id, "a": agent_id, "i": f"ws:{conn_id}",
         "h": host_id, "c": conn_id})
    await session.commit()


async def touch_presence(session: AsyncSession, workspace_id: Any, conn_id: str) -> None:
    """Keep the row younger than the presence window while frames arrive."""
    await session.execute(text(
        "UPDATE agent_presence SET seen_at = NOW() "
        "WHERE workspace_id = :ws AND active_conn_id = CAST(:c AS UUID)"),
        {"ws": workspace_id, "c": conn_id})
    await session.commit()


async def clear_presence(session: AsyncSession, *, workspace_id: Any, conn_id: str) -> None:
    await session.execute(text(
        "UPDATE agent_presence SET status = 'offline', active_conn_id = NULL, "
        "worker_busy = FALSE "
        "WHERE workspace_id = :ws AND active_conn_id = CAST(:c AS UUID)"),
        {"ws": workspace_id, "c": conn_id})
    await session.commit()


async def reap_stale(older_than_seconds: float = 12.0) -> dict[str, int]:
    """Sweep sockets that stopped answering PINGs, and dispatches past their deadline.

    The presence sweep only sees timestamps; a socket whose process is SIGSTOPped
    still looks recent for its window. This runs every few seconds and closes
    from the owning side. A dispatch whose process died between the two receipts
    would otherwise sit in AWAITING_* and block its task forever.
    """
    cutoff = _ms() - int(older_than_seconds * 1000)
    stale = [sock for sock in list(nerve_hub._by_key.values()) if sock.last_seen_ms < cutoff]
    reaped = 0
    for sock in stale:
        if await nerve_hub.unregister(sock, "HEARTBEAT_TIMEOUT"):
            reaped += 1
            try:
                await sock.ws.close(code=4008, reason="HEARTBEAT_TIMEOUT")
            except Exception:  # noqa: BLE001
                pass
    abandoned = await _reopen_expired_dispatches()
    return {"checked": len(nerve_hub._by_key), "reaped": reaped, "reopened": abandoned}


async def _reopen_expired_dispatches() -> int:
    """Return tasks whose ACK deadline passed to the board (deadline, not wall clock)."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "UPDATE collab_tasks SET dispatch_state = 'IDLE', ack_deadline_at = NULL, "
            "status = CASE WHEN status = 'IN_PROGRESS' THEN 'READY'::task_status ELSE status END, "
            "claimed_by = NULL, lease_owner = NULL, lease_expires_at = NULL, updated_at = NOW() "
            "WHERE dispatch_state IN ('AWAITING_NET_ACK', 'AWAITING_EXEC_ACK') "
            "AND ack_deadline_at IS NOT NULL AND ack_deadline_at < NOW() "
            "RETURNING task_key"))).scalars().all()
        await session.commit()
        if rows:
            logger.warning("reopened %s dispatch(es) past their ACK deadline: %s",
                           len(rows), ", ".join(rows[:10]))
        return len(rows)


async def nerve_reaper_loop(interval_seconds: float = 3.0) -> None:
    # It imported a `sleep_or_stop` that never existed, so this task died at
    # start-up and nothing ever reopened a dispatch past its ACK deadline.
    while True:
        try:
            await reap_stale()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep never stops the loop
            logger.exception("nerve reaper pass failed")
        await asyncio.sleep(interval_seconds)


__all__ = [
    "ActiveSocket",
    "NerveHub",
    "NerveEnvelope",
    "deliver_step_poke",
    "deliver_task_poke",
    "dispatchable",
    "exec_ack_timeout_s",
    "record_exec_started",
    "mark_offline",
    "nerve_hub",
    "nerve_reaper_loop",
    "presence_row",
    "clear_presence",
    "reap_stale",
    "timedelta",
]
