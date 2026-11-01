"""Backfill the unified lifecycle columns (migration M1).

Idempotent and safe to run repeatedly against a live database:

* ``collab_tasks.correlation`` from the historical ``input_context`` JSONB;
* ``collab_tasks.active_run_id`` for non-terminal tasks with a Run;
* ``kernel_bindings`` from historical code tasks, so old repositories keep
  routing to the Kernel workspace that executed them.

    DATABASE_URL=postgresql+asyncpg://... python scripts/backfill_unified.py
    ... --dry-run          # report counts, write nothing

It never deletes or rewrites history: only NULL/empty values are filled.
"""

from __future__ import annotations

import argparse
import asyncio
import os

CORRELATION_SQL = """
UPDATE collab_tasks
SET correlation = jsonb_strip_nulls(jsonb_build_object(
      'run_id', input_context->'agent_runtime'->>'run_id',
      'kernel_workspace_id', input_context->'kernel'->>'workspace_id',
      'kernel_task_id', input_context->'kernel'->>'task_id'))
WHERE (correlation = '{}'::jsonb OR correlation IS NULL)
  AND (input_context ? 'agent_runtime' OR input_context ? 'kernel')
"""

ACTIVE_RUN_SQL = """
UPDATE collab_tasks
SET active_run_id = (input_context->'agent_runtime'->>'run_id')::uuid
WHERE active_run_id IS NULL
  AND status IN ('TODO', 'READY', 'IN_PROGRESS')
  AND input_context->'agent_runtime'->>'run_id' IS NOT NULL
"""

BINDINGS_SQL = """
INSERT INTO kernel_bindings (
    workspace_id, project, repo_path, kernel_base_url,
    kernel_workspace_id, credential_ref
)
SELECT DISTINCT ON (
           t.workspace_id, COALESCE(t.project, ''), COALESCE(t.repo, '')
       )
       t.workspace_id, COALESCE(t.project, ''), COALESCE(t.repo, ''),
       $1,
       (t.input_context->'kernel'->>'workspace_id')::uuid,
       'KERNEL_RUNTIME_TOKEN'
FROM collab_tasks t
WHERE t.input_context->'kernel'->>'workspace_id' IS NOT NULL
  AND COALESCE(t.repo, '') <> ''
ON CONFLICT (workspace_id, project, repo_path) DO NOTHING
"""

COUNTS = {
    "correlation": """
        SELECT count(*) FROM collab_tasks
        WHERE (correlation = '{}'::jsonb OR correlation IS NULL)
          AND (input_context ? 'agent_runtime' OR input_context ? 'kernel')
    """,
    "active_run_id": """
        SELECT count(*) FROM collab_tasks
        WHERE active_run_id IS NULL
          AND status IN ('TODO', 'READY', 'IN_PROGRESS')
          AND input_context->'agent_runtime'->>'run_id' IS NOT NULL
    """,
    "kernel_bindings": """
        SELECT count(DISTINCT (t.workspace_id, COALESCE(t.project, ''),
                               COALESCE(t.repo, '')))
        FROM collab_tasks t
        LEFT JOIN kernel_bindings b
          ON b.workspace_id = t.workspace_id
         AND b.project = COALESCE(t.project, '')
         AND b.repo_path = COALESCE(t.repo, '')
        WHERE t.input_context->'kernel'->>'workspace_id' IS NOT NULL
          AND COALESCE(t.repo, '') <> ''
          AND b.binding_id IS NULL
    """,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--kernel-base-url",
        default=os.environ.get("KERNEL_URL", "http://kernel-api:8001"),
    )
    args = parser.parse_args()

    import asyncpg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise SystemExit("DATABASE_URL is required")
    conn = await asyncpg.connect(
        dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    )
    try:
        for name, count_sql in COUNTS.items():
            count = await conn.fetchval(count_sql)
            print(f"{name}: {count} row(s) to backfill")
        if args.dry_run:
            return 0
        async with conn.transaction():
            print("correlation:", await conn.execute(CORRELATION_SQL))
            print("active_run_id:", await conn.execute(ACTIVE_RUN_SQL))
            print(
                "kernel_bindings:",
                await conn.execute(BINDINGS_SQL, args.kernel_base_url),
            )
        print("backfill complete")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
