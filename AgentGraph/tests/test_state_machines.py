"""The declared state machines match the schema, and the database holds every writer."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from app import state_machines

ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"Authorization": "Bearer test-admin"}


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def test_the_generated_page_is_current() -> None:
    ran = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_state_machines.py"),
                          "--check"], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_every_machine_is_well_formed() -> None:
    names = [m.name for m in state_machines.MACHINES]
    assert len(names) == len(set(names))
    columns = [(m.table, m.column) for m in state_machines.MACHINES]
    assert len(columns) == len(set(columns))
    for machine in state_machines.MACHINES:
        assert machine.summary and all(machine.states.values()), machine.name
        assert all(why for why in machine.moves.values()), machine.name
        for state in machine.terminal:
            assert not machine.allowed_from(state), (machine.name, state)


@pytest.mark.integration
async def test_declared_states_are_exactly_what_the_schema_allows(client) -> None:
    del client  # the server has started, so the schema exists (on a fresh database too)
    conn = await asyncpg.connect(_db())
    try:
        for machine in state_machines.MACHINES:
            kind = await conn.fetchval(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2",
                machine.table, machine.column)
            assert kind, f"{machine.table}.{machine.column} does not exist"
            if kind == "task_status":
                allowed = set(await conn.fetchval("SELECT enum_range(NULL::task_status)::text[]"))
            else:
                checks = await conn.fetch(
                    "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                    "WHERE conrelid = $1::regclass AND contype = 'c'", machine.table)
                allowed = set()
                for row in checks:
                    found = re.search(
                        rf"\(+\(?{machine.column}\)?(?:::text)? = ANY \(+ARRAY\[(.*?)\]", row["d"])
                    if found:
                        allowed = set(re.findall(r"'([^']*)'", found.group(1)))
            if not allowed:
                # No list in the schema: the guard is what keeps the values in the set.
                continue
            assert set(machine.states) == allowed, (machine.name, set(machine.states) ^ allowed)
    finally:
        await conn.close()


@pytest.mark.integration
async def test_no_state_column_is_left_implicit(client) -> None:
    """Every status-like column is declared, or listed with why it is not a machine."""
    del client  # on an empty database this would find nothing and pass for nothing
    from app.state_machine_catalog import NOT_MACHINES

    conn = await asyncpg.connect(_db())
    try:
        rows = await conn.fetch(
            "SELECT c.table_name AS t, c.column_name AS c FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_name = c.table_name "
            "AND t.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
            "WHERE c.table_schema = 'public' AND c.table_name NOT LIKE 'workspace_events%' "
            "AND c.table_name NOT LIKE '\\_%' AND c.table_name NOT LIKE 'state\\_%' "
            # A *_history table records the moves of a machine; it holds no state.
            "AND c.table_name NOT LIKE '%\\_history' "
            "AND (c.column_name IN ('status', 'state', 'reported', 'verdict', 'outcome', "
            "     'result', 'mode') OR c.column_name LIKE '%\\_state' "
            "     OR c.column_name LIKE '%\\_status') "
            "AND c.data_type IN ('character varying', 'text', 'USER-DEFINED')")
    finally:
        await conn.close()
    declared = {f"{m.table}.{m.column}" for m in state_machines.MACHINES}
    missing = sorted(f"{row['t']}.{row['c']}" for row in rows
                     if f"{row['t']}.{row['c']}" not in declared | set(NOT_MACHINES))
    assert missing == [], ("declare these in app/state_machine_catalog.py, or say in "
                           f"NOT_MACHINES why they are not a lifecycle: {missing}")
    assert not declared & set(NOT_MACHINES)


@pytest.mark.integration
async def test_the_database_guards_every_declared_column(client) -> None:
    del client  # the server has started, so it has synced the declarations
    conn = await asyncpg.connect(_db())
    try:
        assert await conn.fetchval("SELECT mode FROM state_guard") == "enforce"
        guarded = {(row["t"], row["g"]) for row in await conn.fetch(
            "SELECT c.relname AS t, t.tgname AS g FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid WHERE t.tgname LIKE 'trg_state_%'")}
        for machine in state_machines.MACHINES:
            assert (machine.table, f"trg_state_{machine.column}") in guarded, machine.name
        declared = await conn.fetchval("SELECT count(*) FROM state_transitions")
        assert declared == sum(len(m.pairs()) for m in state_machines.MACHINES)
    finally:
        await conn.close()


@pytest.mark.integration
async def test_an_undeclared_move_is_a_409_that_names_the_way_out(client) -> None:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"sm-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    key = {"Authorization": f"Bearer {created['api_key']}"}
    made = await client.post("/v1/tasks", headers=key, json={
        "task_key": f"sm-{uuid.uuid4().hex[:8]}", "title": "guarded",
        "assigned_role": "EXECUTOR", "namespace": "default"})
    assert made.status_code == 201, made.text
    task_key = made.json()["task_key"]
    # The application's connections are held to the declared moves.
    app = await asyncpg.connect(_db(), server_settings={"application_name": "veynod"})
    try:
        # READY straight to AWAITING_HUMAN skips the work: the application may not.
        with pytest.raises(asyncpg.RaiseError) as refused:
            await app.execute(
                "UPDATE collab_tasks SET status = 'AWAITING_HUMAN' WHERE task_key = $1",
                task_key)
        assert "ERR_ILLEGAL_TRANSITION" in str(refused.value)
        assert '"allowed"' in (refused.value.detail or "")
        # An undo restores an earlier state, whatever move that is (rolled back here).
        tx = app.transaction()
        await tx.start()
        await app.execute("SELECT set_config('veynod.state_override', 'undo', true)")
        await app.execute(
            "UPDATE collab_tasks SET status = 'AWAITING_HUMAN' WHERE task_key = $1", task_key)
        assert await app.fetchval("SELECT status::text FROM collab_tasks WHERE task_key = $1",
                                  task_key) == "AWAITING_HUMAN"
        await tx.rollback()
    finally:
        await app.close()
    # Anyone else (an operator repairing a row) is let through and recorded.
    operator = await asyncpg.connect(_db())
    try:
        tx = operator.transaction()
        await tx.start()
        await operator.execute(
            "UPDATE collab_tasks SET status = 'AWAITING_HUMAN' WHERE task_key = $1", task_key)
        writer = await operator.fetchval(
            "SELECT writer FROM state_transition_violations WHERE table_name = "
            "'collab_tasks' AND from_state = 'READY' AND to_state = 'AWAITING_HUMAN' "
            "ORDER BY id DESC LIMIT 1")
        assert writer is not None and writer != "veynod"
        await tx.rollback()
    finally:
        await operator.close()
    # The same refusal through the API's error handler.
    from sqlalchemy.exc import DBAPIError

    error = state_machines.from_database_error(DBAPIError("UPDATE", {}, refused.value))
    assert error is not None and error.status_code == 409
    assert error.code == "invalid_state_transition"
    assert error.details["machine"] == "task" and "IN_PROGRESS" in error.details["allowed"]
    assert error.details["current"] == "READY"
    assert error.details["requested"] == "AWAITING_HUMAN"


@pytest.mark.integration
async def test_the_machines_are_readable_by_any_caller(client) -> None:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"sm-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    key = {"Authorization": f"Bearer {created['api_key']}"}
    listed = await client.get("/v1/state-machines", headers=key)
    assert listed.status_code == 200, listed.text
    names = {item["name"] for item in listed.json()["items"]}
    assert {"task", "runner_placement"} <= names
    task = (await client.get("/v1/state-machines/task", headers=key)).json()
    ready = next(item for item in task["states"] if item["state"] == "READY")
    assert "IN_PROGRESS" in ready["next"]
    missing = await client.get("/v1/state-machines/nope", headers=key)
    assert missing.status_code == 404 and "task" in missing.json()["error"]["details"]["known"]
    seen = await client.get("/v1/admin/state-machines/violations", headers=ADMIN)
    assert seen.status_code == 200 and seen.json()["mode"] == "enforce"
