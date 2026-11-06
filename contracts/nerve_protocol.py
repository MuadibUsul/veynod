"""Frames of the /v1/ws/nerve channel between a hub and its agents.

A poke is a promise the platform keeps: the hub does not call it delivered
until the messenger has said it saw the frame (NET_ACK) and the worker it
spawned has survived orientation (EXEC_ACK). Both deadlines are short and hard,
so a half-open socket is found in milliseconds instead of the ~90 s a presence
sweep needs.

Two identities travel on every connection and are never merged: the host (a
computer, ``grok-bot-vm``) and the agent (a directory entry on it,
``grok-dou``). Dispatch routes on ``agent_id`` alone.

Frames are a flat JSON object so either side can log one without a schema:
``{"frame_id": ..., "type": ..., "ts_ms": ..., "payload": {...}}``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NerveFrameType(str, Enum):
    # handshake and in-band credential renewal
    AUTH_ACK = "auth.ack"
    TOKEN_REFRESH_REQ = "auth.token_refresh.req"
    TOKEN_REFRESH_RES = "auth.token_refresh.res"

    # application-level heartbeat, doubling as the host's busy report
    PING = "sys.ping"
    PONG = "sys.pong"

    # dispatch with two receipts (QoS 1)
    TASK_POKE = "task.poke"
    TASK_ACK_NET = "task.ack.net"
    TASK_ACK_EXEC = "task.ack.exec"
    TASK_BUSY_REJECT = "task.busy.reject"

    # lease upkeep and the kill order
    LEASE_HEARTBEAT = "lease.heartbeat"
    LEASE_REVOKED = "lease.revoked"

    # an ordinary addressed event (assignment happened elsewhere): no receipts,
    # just delivery, so the messenger can still wake the agent for it
    EVENT_NOTICE = "event.notice"

    # a subscription sign-in on the computer, started from the console (v2
    # P1-D): the hub asks, the computer reports only what the person must see
    # (a sign-in link, a one-time code), the code the person pastes goes back.
    # The login itself never leaves the computer.
    LOGIN_START = "login.start"
    LOGIN_UPDATE = "login.update"
    LOGIN_CODE = "login.code"
    LOGIN_CANCEL = "login.cancel"


#: The hub closes the socket when the first receipt does not arrive in time:
#: the peer is not reading, whatever TCP thinks. 200 ms is the budget for a
#: near peer; the wait is a network round trip, so a far one (another
#: continent, a proxy) gets a multiple of the round trip it has shown, never
#: more than ``NET_ACK_MAX_S``. A flat 200 ms over a 250 ms link kicked a
#: healthy agent offline on every dispatch, and the task bounced forever.
NET_ACK_TIMEOUT_S = 0.200
#: Before a socket has shown its round trip, its first poke waits this long.
NET_ACK_FIRST_CONTACT_S = 1.000
#: No socket waits longer than this for the first receipt.
NET_ACK_MAX_S = 2.000
#: How many round trips a far peer gets.
NET_ACK_RTT_FACTOR = 3.0


def net_ack_deadline_s(rtt_ms: float | None) -> float:
    """The first-receipt wait for a socket whose smoothed round trip is ``rtt_ms``."""
    if not rtt_ms or rtt_ms <= 0:
        return NET_ACK_FIRST_CONTACT_S
    return min(NET_ACK_MAX_S, max(NET_ACK_TIMEOUT_S, NET_ACK_RTT_FACTOR * rtt_ms / 1000.0))


def smooth_rtt_ms(previous: float | None, sample: float) -> float:
    """Exponentially smoothed round trip (RFC 6298 alpha = 1/8), first sample as is."""
    if not previous or previous <= 0:
        return float(sample)
    return previous + (float(sample) - previous) / 8.0
#: The worker must be past orientation in this window or it is killed; the
#: frame carries the smaller orientation budget for the messenger's watchdog.
EXEC_ACK_TIMEOUT_S = 5.000
ORIENTATION_TIMEOUT_MS = 4_500
#: A heartbeat per this many seconds; three missed is an offline agent.
PING_EVERY_S = 5.0
#: Close codes (4000-4999 are application-defined).
CLOSE_SUPERSEDED = 4000
CLOSE_UNAUTHORIZED = 4001
CLOSE_IDENTITY_MISMATCH = 4002
CLOSE_NET_ACK_TIMEOUT = 4008


class NerveError(str, Enum):
    """Why a dispatch attempt did not become a lease."""

    NOT_CONNECTED = "AGENT_NOT_CONNECTED_ON_WS_NERVE"
    BUSY = "AGENT_WORKER_BUSY"
    NET_ACK_TIMEOUT = "QOS_TIMEOUT_NET_ACK_200MS"
    EXEC_ACK_TIMEOUT = "QOS_TIMEOUT_EXEC_ACK_5000MS"
    REJECTED = "AGENT_REJECTED"


@dataclass
class NerveEnvelope:
    type: NerveFrameType
    payload: dict[str, Any] = field(default_factory=dict)
    frame_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "type": self.type.value,
            "ts_ms": self.ts_ms,
            "payload": self.payload,
        }


def poke_frame(
    *,
    task_key: str,
    lease_epoch: int,
    placement_id: str,
    title: str,
    step: str = "work",
    **extra: Any,
) -> NerveEnvelope:
    """The dispatch frame. ``frame_id`` is the poke's idempotency key on both ends."""
    payload = {
        "task_key": task_key,
        "lease_epoch": lease_epoch,
        "placement_id": placement_id,
        "title": title[:256],
        "step": step,
        "orientation_timeout_ms": ORIENTATION_TIMEOUT_MS,
        **extra,
    }
    return NerveEnvelope(type=NerveFrameType.TASK_POKE, payload=payload)


def parse_frame(raw: Any) -> dict[str, Any] | None:
    """A received frame, or None when it is not one (never raise on client input)."""
    if not isinstance(raw, dict):
        return None
    frame_type = raw.get("type")
    if not isinstance(frame_type, str) or not frame_type:
        return None
    payload = raw.get("payload")
    frame_id = raw.get("frame_id")
    return {
        "type": frame_type,
        "payload": payload if isinstance(payload, dict) else {},
        "frame_id": frame_id if isinstance(frame_id, str) and frame_id else None,
    }
