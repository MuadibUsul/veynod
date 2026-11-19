"""Every state machine of the platform, declared in one place.

A status column is a state machine whether or not anyone writes it down. Here
each one is: its states, the moves between them, and why each move happens.
This module is the only source:

* the database holds every writer to it (``sync``): the moves are copied into
  ``state_transitions`` and ``agentgraph_guard_transition`` sits on each
  column, so an ORM write, raw SQL in a background loop and a migration are
  held alike;
* a refused move is a 409 ``invalid_state_transition`` naming the machine, the
  current state, the requested one and the moves that are allowed from it
  (``from_database_error``), never a 503 that tells a caller to retry;
* ``GET /v1/state-machines`` and docs/STATE_MACHINES.md (generated) show them
  to people and agents.

To add a move: add it to its machine with the reason, run
``scripts/gen_state_machines.py``. A move the code makes that is not declared
is refused under ``enforce`` and counted under ``record``
(``STATE_GUARD_MODE``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agentgraph.state_machines")


@dataclass(frozen=True)
class Machine:
    """One state column: its states and the moves between them."""

    name: str
    table: str
    column: str
    summary: str
    states: dict[str, str]
    #: (from, to) -> why the move happens (who or what makes it).
    moves: dict[tuple[str, str], str]
    initial: tuple[str, ...] = ()
    terminal: tuple[str, ...] = ()
    #: Moves into any state from any other are allowed (a setting, not a lifecycle).
    free: bool = False
    tags: tuple[str, ...] = field(default=())

    def allowed_from(self, state: str) -> list[str]:
        if self.free:
            return sorted(s for s in self.states if s != state)
        return sorted(to for (frm, to) in self.moves if frm == state)

    def allows(self, current: str, target: str) -> bool:
        return current == target or target in self.allowed_from(current)

    def pairs(self) -> list[tuple[str, str]]:
        if self.free:
            return [(a, b) for a in self.states for b in self.states if a != b]
        return sorted(self.moves)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "table": self.table, "column": self.column,
            "summary": self.summary,
            "states": [{"state": s, "meaning": why, "initial": s in self.initial,
                        "terminal": s in self.terminal,
                        "next": self.allowed_from(s)} for s, why in self.states.items()],
            "moves": ([{"from": "*", "to": "*", "why": "any value may replace any other"}]
                      if self.free else
                      [{"from": a, "to": b, "why": why} for (a, b), why in
                       sorted(self.moves.items())]),
        }


def _m(name: str, table: str, column: str, summary: str, states: dict[str, str],
       moves: list[tuple[str, str | tuple[str, ...], str]], *,
       initial: tuple[str, ...] = (), terminal: tuple[str, ...] = (),
       free: bool = False, tags: tuple[str, ...] = ()) -> Machine:
    """``moves`` rows are (from, to or several tos, why); "*" as from is every
    non-terminal state."""
    table_moves: dict[tuple[str, str], str] = {}
    for frm, to, why in moves:
        sources = [s for s in states if s not in terminal] if frm == "*" else [frm]
        for source in sources:
            for target in (to if isinstance(to, tuple) else (to,)):
                if source == target:
                    continue
                if source not in states or target not in states:
                    raise ValueError(f"{name}: unknown state in {source} -> {target}")
                table_moves.setdefault((source, target), why)
    for state in states:
        if state in terminal:
            continue
        if not free and not any(frm == state for frm, _ in table_moves) and state not in initial:
            raise ValueError(f"{name}: {state} is neither terminal nor left by any move")
    return Machine(name, table, column, summary, states, table_moves, initial, terminal,
                   free, tags)


from app.state_machine_catalog import MACHINES  # noqa: E402  (data, after the types)


def by_table(table: str, column: str) -> Machine | None:
    for machine in MACHINES:
        if machine.table == table and machine.column == column:
            return machine
    return None


def by_name(name: str) -> Machine | None:
    return next((machine for machine in MACHINES if machine.name == name), None)


def describe_all() -> list[dict[str, Any]]:
    return [machine.describe() for machine in MACHINES]


# ------------------------------------------------------------------ database


def _trigger_name(machine: Machine) -> str:
    return f"trg_state_{machine.column}"


async def sync(conn: Any, mode: str) -> None:
    """Copy the declared moves into the database and guard each column.

    Runs at every start (``apply_schema``), under the schema lock. Rows are
    replaced in one transaction; a trigger is only created where it is missing,
    so a start beside running servers takes no DDL lock it does not need.
    """
    async with conn.transaction():
        await conn.execute("DELETE FROM state_transitions")
        rows = [(m.table, m.column, a, b) for m in MACHINES for a, b in m.pairs()]
        await conn.executemany(
            "INSERT INTO state_transitions (table_name, column_name, from_state, to_state) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING", rows)
        await conn.execute("UPDATE state_guard SET mode = $1 WHERE singleton", mode)
    present = {(row["table"], row["name"]) for row in await conn.fetch(
        "SELECT c.relname AS table, t.tgname AS name FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid WHERE t.tgname LIKE 'trg_state_%'")}
    for machine in MACHINES:
        if (machine.table, _trigger_name(machine)) in present:
            continue
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", machine.table):
            continue
        await conn.execute(
            f'CREATE TRIGGER {_trigger_name(machine)} BEFORE UPDATE OF "{machine.column}" '
            f'ON "{machine.table}" FOR EACH ROW '
            f"EXECUTE FUNCTION agentgraph_guard_transition('{machine.column}')")
    logger.info("state machines: %s declared, %s moves, guard %s",
                len(MACHINES), sum(len(m.pairs()) for m in MACHINES), mode)


async def violations(session: Any, days: int = 30) -> list[dict[str, Any]]:
    """Moves let through undeclared, grouped, most recent first. ``by_application``
    false: a person or tool on the database (never refused, only recorded)."""
    from sqlalchemy import text

    rows = (await session.execute(text(
        "SELECT table_name, column_name, from_state, to_state, "
        "(writer IS NOT DISTINCT FROM 'veynod') AS by_application, count(*) AS count, "
        "min(at) AS first_seen_at, max(at) AS last_seen_at FROM state_transition_violations "
        "WHERE at > NOW() - make_interval(days => :days) "
        "GROUP BY 1, 2, 3, 4, 5 ORDER BY max(at) DESC"), {"days": days})).mappings().all()
    out = []
    for row in rows:
        machine = by_table(row["table_name"], row["column_name"])
        out.append({**dict(row), "machine": machine.name if machine else None})
    return out


# ------------------------------------------------------------------ errors

#: Messages a database trigger raises, and the API error each one is.
_TRIGGER_ERRORS = {
    "ERR_TERMINAL_STATE_IMMUTABLE": (409, "terminal_state_immutable"),
    "ERR_ACCEPTANCE_LOCKED": (403, "acceptance_locked"),
    "ERR_CONTEXT_DOCS_LOCKED": (403, "acceptance_locked"),
    "ERR_ILLEGAL_TRANSITION": (409, "invalid_state_transition"),
}


def from_database_error(exc: BaseException) -> Any:
    """The ApiError a refusal raised by a database trigger stands for, or None.

    These used to reach the generic database handler and come back as 503
    ``database_unavailable``, which tells a caller to retry a request that can
    never succeed.
    """
    from app.errors import ApiError

    orig = getattr(exc, "orig", None) or exc
    message = str(orig)
    for prefix, (status, code) in _TRIGGER_ERRORS.items():
        at = message.find(prefix)
        if at < 0:
            continue
        text_ = message[at + len(prefix):].lstrip(": ").split("\n")[0].strip()
        details: dict[str, Any] = {}
        raw = getattr(getattr(orig, "__cause__", None), "detail", None) \
            or getattr(orig, "detail", None)
        if prefix == "ERR_ILLEGAL_TRANSITION" and raw:
            try:
                details = json.loads(raw)
            except (TypeError, ValueError):
                details = {}
            machine = by_table(details.get("table", ""), details.get("column", ""))
            if machine is not None:
                details["machine"] = machine.name
                details["see"] = f"/v1/state-machines/{machine.name}"
        return ApiError(status, code, text_ or prefix.lower(), details=details or None)
    return None
