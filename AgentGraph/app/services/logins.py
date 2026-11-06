"""Subscription sign-ins on a computer, started from the console (v2 phase 1, P1-D).

A server has no browser and nobody at its keyboard, so the person signs its
agents in to their own subscription from the console instead:

1. The owner picks a computer and a runtime; the hub sends ``login.start`` over
   that computer's nerve socket.
2. The computer runs the agent's own sign-in (``claude setup-token``, or
   ``codex login --device-auth``) and reports with ``login.update`` only what
   the person must see: a sign-in link on the provider's own site, and for
   Codex a one-time code to enter there.
3. For Claude Code the person pastes the code the sign-in page shows; it goes
   back as ``login.code``. Without the PKCE verifier, which never leaves the
   computer's process, that code signs nothing in.
4. The computer keeps the login (cli/bin/login.mjs) and reports ``connected``.

The login itself never reaches this server. What does is kept in memory for
twenty minutes, shown only to the person who started it, and only links to
the providers' own sign-in pages are passed on, so a computer cannot use this
to send its owner anywhere else.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError, Forbidden, NotFound
from contracts.nerve_protocol import NerveEnvelope, NerveFrameType

logger = logging.getLogger("agentgraph.logins")

RUNTIMES = {"claude": "Claude Code", "codex": "Codex"}
#: What each runtime signs in to, for the console's choice.
SUBSCRIPTIONS = {"claude": "Claude Pro or Max", "codex": "ChatGPT Plus or Pro"}
STATES = frozenset({"starting", "enter_code", "open_url", "verifying", "connected", "failed",
                    "cancelled"})
DONE = frozenset({"connected", "failed", "cancelled"})
LOGIN_TTL_S = 20 * 60
#: The first CLI that answers ``login.start`` (cli/bin/login.mjs).
LOGIN_MIN_VERSION = "0.8.0"

_URL = re.compile(r"^https://(?:claude\.com|claude\.ai|platform\.claude\.com|console\.anthropic\.com"
                  r"|auth\.openai\.com)/[^\s\"'<>]{1,2000}$")
_USER_CODE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4,8}$")
_CODE = re.compile(r"^[A-Za-z0-9#_.~-]{8,2048}$")
_PLAN = re.compile(r"^[A-Za-z0-9 _.+-]{1,32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class Login:
    login_id: str
    workspace_id: str
    agent_id: str
    host_id: str
    runtime: str
    started_by: str
    state: str = "starting"
    url: str | None = None
    user_code: str | None = None
    expires_at: float | None = None
    plan: str | None = None
    message: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


#: login_id -> the sign-in. One API process holds every nerve socket, so the
#: sign-ins live beside them; a restart drops them and the person starts again.
_logins: dict[str, Login] = {}


def _prune(now: float | None = None) -> None:
    now = now or time.time()
    for login_id, login in list(_logins.items()):
        if now - login.updated_at > LOGIN_TTL_S:
            del _logins[login_id]


def _iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value else None


def login_out(login: Login) -> dict[str, Any]:
    return {
        "login_id": login.login_id,
        "agent_id": login.agent_id,
        "host_id": login.host_id,
        "runtime": login.runtime,
        "runtime_name": RUNTIMES[login.runtime],
        "subscription": SUBSCRIPTIONS[login.runtime],
        "state": login.state,
        "url": login.url,
        "user_code": login.user_code,
        "expires_at": _iso(login.expires_at),
        "plan": login.plan,
        "message": login.message,
        "created_at": _iso(login.created_at),
        "updated_at": _iso(login.updated_at),
    }


def _starter(principal: Principal) -> str:
    return f"human:{principal.user_id}" if principal.user_id else "admin"


async def _send(sock: Any, frame_type: NerveFrameType, payload: dict[str, Any]) -> bool:
    try:
        await sock.ws.send_json(NerveEnvelope(type=frame_type, payload=payload).to_dict())
        return True
    except Exception:  # noqa: BLE001 - a dead socket reads as offline
        return False


def _socket(workspace_id: Any, agent_id: str | None) -> Any:
    from app.services.nerve_hub import nerve_hub

    return nerve_hub.get(workspace_id, agent_id) if agent_id else None


async def start(session: AsyncSession, principal: Principal, ref: str,
                runtime: str) -> dict[str, Any]:
    """Owner: ask a computer to sign ``runtime`` in to the owner's own subscription."""
    from app.services import runners

    runners._owner_only(principal)
    if runtime not in RUNTIMES:
        raise ApiError(422, "runtime_unsupported",
                       f"no sign-in for {runtime[:32]!r} (claude or codex)")
    row = await runners._find(session, principal.workspace.id, ref)
    if row["status"] == "revoked":
        raise ApiError(409, "host_revoked", "this computer was taken off the Loop",
                       details={"host_id": row["host_id"]})
    sock = _socket(principal.workspace.id, row["agent_id"])
    name = row["agent_id"] or row["host_id"]
    if sock is None:
        raise ApiError(409, "computer_offline",
                       f"{name} is offline: start veynod on it, then try again",
                       details={"host_id": row["host_id"]})
    report = row.get("capabilities_report") or {}
    can = report.get("logins") if isinstance(report, dict) else None
    if not isinstance(can, list) or runtime not in can:
        if not isinstance(can, list):
            reason = f"update veynod on it to {LOGIN_MIN_VERSION} or newer (`npx veynod@latest`)"
        else:
            reason = f"{RUNTIMES[runtime]} is not installed there"
            if runtime == "claude":
                reason += ", or its sign-in has no terminal to run in (Windows, or no `script`)"
        raise ApiError(409, "login_unavailable",
                       f"{RUNTIMES[runtime]} cannot be signed in from the console on {name}: "
                       f"{reason}",
                       details={"host_id": row["host_id"], "runtime": runtime, "can": can})
    _prune()
    # One sign-in per computer and runtime: a new one replaces the old.
    for other in list(_logins.values()):
        if (other.workspace_id, other.agent_id, other.runtime) == (
                str(principal.workspace.id), row["agent_id"], runtime) and other.state not in DONE:
            other.state, other.updated_at = "cancelled", time.time()
            await _send(sock, NerveFrameType.LOGIN_CANCEL, {"login_id": other.login_id})
    login = Login(login_id=f"lgn_{secrets.token_urlsafe(12)}",
                  workspace_id=str(principal.workspace.id), agent_id=row["agent_id"],
                  host_id=row["host_id"], runtime=runtime, started_by=_starter(principal))
    _logins[login.login_id] = login
    started = {"login_id": login.login_id, "runtime": runtime}
    if not await _send(sock, NerveFrameType.LOGIN_START, started):
        del _logins[login.login_id]
        raise ApiError(409, "computer_offline", f"{row['agent_id']} went offline: try again",
                       details={"host_id": row["host_id"]})
    return login_out(login)


def _mine(principal: Principal, ref: str, login_id: str) -> Login:
    from app.services import runners

    runners._owner_only(principal)
    _prune()
    login = _logins.get(login_id)
    ref = ref.removeprefix("host:")
    if (login is None or login.workspace_id != str(principal.workspace.id)
            or ref not in (login.host_id, login.agent_id)):
        raise NotFound("login")
    # The link and code are for the person who asked for them.
    if login.started_by != _starter(principal):
        raise Forbidden("only the person who started this sign-in can see or finish it")
    return login


async def get(principal: Principal, ref: str, login_id: str) -> dict[str, Any]:
    return login_out(_mine(principal, ref, login_id))


async def submit_code(principal: Principal, ref: str, login_id: str, code: str) -> dict[str, Any]:
    """The code Claude's sign-in page showed the person, for the computer's sign-in."""
    login = _mine(principal, ref, login_id)
    if login.runtime != "claude" or login.state != "enter_code":
        raise ApiError(409, "login_not_waiting", "this sign-in is not waiting for a code",
                       details={"state": login.state})
    code = code.strip()
    if not _CODE.fullmatch(code):
        raise ApiError(422, "login_code_invalid", "paste the whole code the sign-in page shows")
    sock = _socket(login.workspace_id, login.agent_id)
    if sock is None or not await _send(sock, NerveFrameType.LOGIN_CODE,
                                       {"login_id": login.login_id, "code": code}):
        raise ApiError(409, "computer_offline",
                       f"{login.agent_id} is offline: start the sign-in again")
    login.state, login.message, login.updated_at = "verifying", None, time.time()
    return login_out(login)


async def cancel(principal: Principal, ref: str, login_id: str) -> dict[str, Any]:
    login = _mine(principal, ref, login_id)
    if login.state not in DONE:
        sock = _socket(login.workspace_id, login.agent_id)
        if sock is not None:
            await _send(sock, NerveFrameType.LOGIN_CANCEL, {"login_id": login.login_id})
        login.state, login.updated_at = "cancelled", time.time()
    return login_out(login)


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL.sub(" ", value).strip()[:limit]
    return cleaned or None


def on_update(sock: Any, payload: dict[str, Any]) -> None:
    """A ``login.update`` from the computer: fold in what may be shown, drop the rest."""
    login = _logins.get(str(payload.get("login_id") or ""))
    # Only the computer the sign-in was sent to may move it.
    if (login is None or login.workspace_id != str(sock.workspace_id)
            or login.agent_id != sock.agent_id):
        return
    state = payload.get("state")
    if state not in STATES or login.state in DONE:
        return
    login.updated_at = time.time()
    url = payload.get("url")
    if state in ("enter_code", "open_url"):
        if not (isinstance(url, str) and _URL.fullmatch(url)):
            login.state = "failed"
            login.message = ("The computer sent a sign-in link that is not the provider's own, "
                             "so Veynod does not show it.")
            logger.warning("login %s: refused a sign-in link from %s",
                           login.login_id, login.agent_id)
            return
        login.url = url
        code = payload.get("user_code")
        login.user_code = code if isinstance(code, str) and _USER_CODE.fullmatch(code) else None
        if state == "open_url" and login.user_code is None:
            login.state, login.message = "failed", "The computer sent no code to enter."
            return
        seconds = payload.get("expires_in_s")
        if isinstance(seconds, int) and 0 < seconds <= 3600:
            login.expires_at = time.time() + seconds
    if state == "connected":
        plan = payload.get("plan")
        login.plan = plan if isinstance(plan, str) and _PLAN.fullmatch(plan) else None
        login.url = login.user_code = None
        _record_connected(login)
    login.message = _clean_text(payload.get("message"), 300) if state == "failed" else None
    login.state = state


def _record_connected(login: Login) -> None:
    """The timeline says which computer now runs on whose subscription (never the login)."""
    async def record() -> None:
        import uuid

        from app.database import async_session_factory
        from app.services.common import append_event

        async with async_session_factory() as session:
            await append_event(
                session, workspace_id=uuid.UUID(login.workspace_id), agent="runners",
                event_type="SUBSCRIPTION_CONNECTED",
                payload={"agent_id": login.agent_id, "host_id": login.host_id,
                         "runtime": login.runtime, "plan": login.plan, "by": login.started_by,
                         "targets": [f"agent:{login.agent_id}"]})
            await session.commit()

    def logged(done: asyncio.Task) -> None:
        if not done.cancelled() and done.exception() is not None:
            logger.warning("could not record a connected subscription", exc_info=done.exception())

    try:
        asyncio.get_running_loop().create_task(record()).add_done_callback(logged)
    except RuntimeError:
        pass  # no loop (a test calling on_update directly)


def reset_for_tests() -> None:
    _logins.clear()
