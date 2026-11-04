#!/usr/bin/env bash
# One migration entrypoint for the whole product.
#
# Every service applies its own idempotent schema at startup (AgentGraph
# apply_schema, Kernel init_db, Runtime init_schema).  This script runs those
# entrypoints to completion *before* the long-running services start, so a
# failed migration stops the deployment instead of being discovered later.
#
#   ./scripts/migrate_all.sh
#
# Requires the images to be built (docker compose build).
set -euo pipefail
cd "$(dirname "$0")/.."

# Run by a deploy computer (veynod up --deploy --before-deploy ./scripts/migrate_all.sh):
# migrate with the images about to be deployed, pinned in VEYNOD_DEPLOY_ENV.
compose=(docker compose)
if [ -n "${VEYNOD_DEPLOY_ENV:-}" ]; then
  [ -f .env ] && compose+=(--env-file .env)
  compose+=(--env-file "$VEYNOD_DEPLOY_ENV")
fi

echo "== kernel schema =="
"${compose[@]}" run --rm --no-deps kernel-api \
  python -c "import asyncio; from app.database import init_db; asyncio.run(init_db()); print('kernel schema ok')"

echo "== runtime schema =="
"${compose[@]}" run --rm --no-deps runtime-api \
  python -c "import asyncio, os; from agent_runtime.store import RunStore; s=RunStore(os.environ['RUNTIME_DATABASE_URL']); asyncio.run(s.init_schema()); print('runtime schema ok')"

echo "== product schema =="
"${compose[@]}" run --rm --no-deps -e AUTO_INIT_DB=true api \
  python -c "import asyncio; from app.database import apply_schema; asyncio.run(apply_schema()); print('product schema ok')"

echo "all schemas are at their current version"
