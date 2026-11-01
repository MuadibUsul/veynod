"""Apply the genesis schema and all versioned migrations to a database.

Usage:
    python scripts/migrate_db.py --database-url postgresql+asyncpg://...

This is the explicit operations path for deployments that set
VN_AUTO_INIT_DB=false.  Normal kernel startup also applies the same idempotent
files, so a restart is enough to upgrade an existing v4 database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.database import init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="SQLAlchemy PostgreSQL URL (postgresql+asyncpg://...).",
    )
    args = parser.parse_args()
    settings.database_url = args.database_url
    settings.auto_init_db = True
    asyncio.run(init_db())
    for path in settings.schema_paths:
        print(f"applied {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
