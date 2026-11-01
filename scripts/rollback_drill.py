"""Disposable Kernel schema upgrade/rollback drill (Phase 8).

Creates a throwaway database, applies v4_001..v4_010, dumps it, applies
v4_011..v4_014, then restores the dump and verifies the new tables/ledger rows
are gone. This proves the documented "restore the previous backup" rollback
path on a real PostgreSQL instance; it is not a production data migration.

    python scripts/rollback_drill.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "veynod_kernel"
ADMIN_DSN = os.environ.get(
    "DRILL_ADMIN_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/postgres"
)
DB_NAME = os.environ.get("DRILL_DB", "veynod_rollback_drill")
CONTAINER = os.environ.get("DRILL_PG_CONTAINER", "veynod-test-pg")


def _dsn(db: str = DB_NAME) -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + "/" + db


def _files() -> list[Path]:
    files = sorted((KERNEL / "sql").glob("v4_*.sql"))
    return files


async def _apply(files: list[Path]) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        for path in files:
            await conn.execute(path.read_text(encoding="utf-8"))
    finally:
        await conn.close()


async def _reset_db() -> None:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(
            f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'
        )
        await admin.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await admin.close()


async def _ledger() -> list[str]:
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch("SELECT version FROM schema_migrations")
        return sorted(row["version"] for row in rows)
    finally:
        await conn.close()


async def _table_exists(name: str) -> bool:
    conn = await asyncpg.connect(_dsn())
    try:
        return bool(
            await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", name)
        )
    finally:
        await conn.close()


def _dump(path: Path) -> None:
    with path.open("wb") as handle:
        subprocess.run(
            [
                "docker", "exec", CONTAINER,
                "pg_dump", "-U", "postgres", "-Fc", "-d", DB_NAME,
            ],
            check=True,
            stdout=handle,
        )


def _restore(path: Path) -> None:
    with path.open("rb") as handle:
        subprocess.run(
            [
                "docker", "exec", "-i", CONTAINER,
                "pg_restore", "-U", "postgres", "-d", DB_NAME, "--clean",
                "--if-exists", "--no-owner",
            ],
            check=True,
            stdin=handle,
        )


async def main() -> int:
    files = _files()
    before = [p for p in files if p.name <= "v4_010_token_workspace.sql"]
    after = [p for p in files if p.name > "v4_010_token_workspace.sql"]
    if not before or not after:
        print("drill needs a v4_010 boundary and later migrations", file=sys.stderr)
        return 2
    await _reset_db()
    await _apply(before)
    before_ledger = await _ledger()
    if "v4_010_token_workspace" not in before_ledger:
        print("baseline ledger missing v4_010", file=sys.stderr)
        return 2
    fd, dump_name = tempfile.mkstemp(prefix="veynod-rollback-", suffix=".dump")
    os.close(fd)
    dump = Path(dump_name)
    try:
        _dump(dump)
        await _apply(after)
        upgraded = await _ledger()
        if "v4_014_delivery_records" not in upgraded:
            print("upgrade did not record v4_014", file=sys.stderr)
            return 2
        if not await _table_exists("delivery_records"):
            print("upgraded schema has no delivery_records", file=sys.stderr)
            return 2
        await _reset_db()
        _restore(dump)
        restored = await _ledger()
        if "v4_014_delivery_records" in restored:
            print("rollback kept v4_014", file=sys.stderr)
            return 2
        if await _table_exists("delivery_records"):
            print("rollback kept delivery_records", file=sys.stderr)
            return 2
        print(
            "rollback drill PASS: baseline=%s upgraded=%s restored=%s"
            % (len(before_ledger), len(upgraded), len(restored))
        )
        return 0
    finally:
        dump.unlink(missing_ok=True)
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'
            )
        finally:
            await admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
