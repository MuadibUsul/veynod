"""Merge duplicate agent identities in one workspace.

Usage (inside the API container, or with DATABASE_URL set):

    python scripts/merge_identities.py --workspace PowerK \
        --group claude=claude-2 --group cursor=cursor-2 --apply

Without --apply it only prints the plan. The target keeps one active credential;
other active generations are revoked. Task assignee/mentions are migrated and
document references are reported, never rewritten.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.database import async_session_factory  # noqa: E402
from app.models import Workspace  # noqa: E402
from app.services.agent_merge import merge_identities  # noqa: E402


def _groups(values: list[str]) -> list[tuple[str, list[str]]]:
    groups = []
    for value in values:
        target, _, sources = value.partition("=")
        names = [item.strip() for item in sources.split(",") if item.strip()]
        if not target.strip() or not names:
            raise SystemExit(f"bad --group {value!r}; expected target=source1,source2")
        groups.append((target.strip(), names))
    return groups


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--group", action="append", default=[], required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    groups = _groups(args.group)

    async with async_session_factory() as session:
        workspace = await session.scalar(
            select(Workspace).where(Workspace.name == args.workspace)
        )
        if workspace is None:
            raise SystemExit(f"workspace {args.workspace!r} not found")
        results = []
        for target, sources in groups:
            result = await merge_identities(
                session, workspace, target, sources, dry_run=not args.apply
            )
            results.append(result)
    print(json.dumps({"workspace": args.workspace, "apply": args.apply,
                      "groups": results}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
