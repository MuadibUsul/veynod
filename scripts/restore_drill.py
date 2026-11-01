"""Backup + restore drill for the three bounded-context databases.

    python scripts/restore_drill.py --source-prefix veynod_chaos_

For each database:
  1. pg_dump -Fc inside the test PostgreSQL container;
  2. create <db>_drill, pg_restore into it;
  3. compare per-table row counts (source vs restored) for every user table.

It is a real drill on real data: the dump file is what an operator would store,
and the restored database is what they would boot the service against.  It
never touches the source database.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "veynod-test-pgvector"
USER = "agentgraph"


def psql(db: str, sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", USER, "-d", db, "-tAc", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:400])
    return result.stdout.strip()


def run(cmd: list[str], *, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=not binary)


def table_counts(db: str) -> dict[str, int]:
    rows = psql(
        db,
        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
        "WHERE table_type='BASE TABLE' "
        "AND table_schema NOT IN ('pg_catalog','information_schema') "
        "ORDER BY 1",
    )
    counts: dict[str, int] = {}
    for table in [line for line in rows.splitlines() if line]:
        schema, name = table.split(".", 1)
        counts[table] = int(psql(db, f'SELECT count(*) FROM "{schema}"."{name}"'))
    return counts


def drill(database: str, dumps: Path) -> dict:
    dump = dumps / f"{database}.dump"
    with dump.open("wb") as handle:
        result = subprocess.run(
            ["docker", "exec", CONTAINER, "pg_dump", "-U", USER, "-Fc", database],
            stdout=handle,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode()[:400])
    restored = f"{database}_drill"
    psql("postgres", f'DROP DATABASE IF EXISTS "{restored}"')
    psql("postgres", f'CREATE DATABASE "{restored}"')
    with dump.open("rb") as handle:
        result = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "pg_restore", "-U", USER,
             "-d", restored, "--no-owner", "--no-acl"],
            stdin=handle,
            capture_output=True,
            text=True,
        )
    # pg_restore returns non-zero on benign notices in some versions; the row
    # counts below are the actual assertion.
    before = table_counts(database)
    after = table_counts(restored)
    mismatched = {
        table: (count, after.get(table))
        for table, count in before.items()
        if after.get(table) != count
    }
    source_rows = sum(before.values())
    psql("postgres", f'DROP DATABASE "{restored}"')
    return {
        "database": database,
        "dump_bytes": dump.stat().st_size,
        "tables": len(before),
        "rows": source_rows,
        "restore_exit": result.returncode,
        "mismatched_tables": mismatched,
        "pass": not mismatched,
        "restore_stderr": (result.stderr or "").strip()[:300],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-prefix", default="veynod_chaos_")
    parser.add_argument("--databases", default="product,kernel,runtime")
    parser.add_argument("--out", default="docs/evidence/restore_drill.txt")
    args = parser.parse_args()
    dumps = ROOT / "docs" / "evidence" / "dumps"
    dumps.mkdir(parents=True, exist_ok=True)
    results = []
    for suffix in args.databases.split(","):
        database = f"{args.source_prefix}{suffix.strip()}"
        try:
            result = drill(database, dumps)
        except Exception as exc:  # noqa: BLE001 - report the failure
            result = {"database": database, "pass": False, "error": str(exc)[:400]}
        results.append(result)
        print(
            f"{result['database']}: {'PASS' if result.get('pass') else 'FAIL'} "
            f"tables={result.get('tables')} rows={result.get('rows')}",
            flush=True,
        )
    lines = ["# Backup / restore drill", ""]
    for result in results:
        lines.append(f"## {result['database']}")
        lines.append("")
        for key, value in result.items():
            if key == "database":
                continue
            lines.append(f"- {key}: {value}")
        lines.append("")
    passed = sum(1 for item in results if item.get("pass"))
    lines.append(f"**{passed}/{len(results)} databases restored with identical row counts.**")
    out = ROOT / args.out
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-1:]))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
