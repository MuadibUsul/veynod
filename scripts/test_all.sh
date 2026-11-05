#!/usr/bin/env bash
# Every suite in the repository, one command. CI runs exactly this.
#
#   PG_URL=postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:5432 scripts/test_all.sh
#   scripts/test_all.sh runtime kernel      # only some suites
#
# Suites: contracts runtime kernel product cli dashboard serendipity. Each
# database suite gets its own disposable database on PG_URL (created if
# missing), so suites never share state. PG_URL needs pgvector for the product
# suite.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
python="${PYTHON:-python}"
pg="${PG_URL:-postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:5432}"
suites=("$@")
[ ${#suites[@]} -gt 0 ] || suites=(contracts runtime kernel product cli dashboard serendipity)
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"

database() {
  "$python" - "$pg" "$1" <<'PY'
import asyncio, sys
import asyncpg
base, name = sys.argv[1].replace("+asyncpg", ""), sys.argv[2]
async def main():
    conn = await asyncpg.connect(f"{base}/postgres")
    if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
        await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
asyncio.run(main())
PY
  echo "$pg/$1"
}

run() { echo; echo "== $1"; shift; "$@"; }

for suite in "${suites[@]}"; do
  case "$suite" in
    contracts) run contracts "$python" -m pytest -q "$root/contracts/tests" ;;
    runtime) url="$(database veynod_test_runtime)"
      run runtime env TEST_DATABASE_URL="$url" "$python" -m pytest -q -c "$root/agent_runtime/pytest.ini" "$root/agent_runtime/tests" ;;
    kernel) url="$(database veynod_test_kernel)"
      run kernel env TEST_DATABASE_URL="$url" bash -c "cd '$root/veynod_kernel' && '$python' -m pytest -q tests" ;;
    product)
      # The real-Kernel delivery test runs the five gates, and STATIC_TYPECHECK
      # needs pyright (deploy/Dockerfile.kernel pins it); say so up front
      # instead of failing later as an unexplained gate_rejected.
      command -v pyright >/dev/null || { echo "product suite needs pyright: npm install -g pyright@1.1.380" >&2; exit 2; }
      url="$(database veynod_test_product)"
      run product env TEST_DATABASE_URL="$url" KERNEL_PYTHON="$(command -v "$python")" \
        bash -c "cd '$root/AgentGraph' && '$python' -m pytest -q tests" ;;
    cli) run cli npm test --prefix "$root/AgentGraph/cli" --silent ;;
    dashboard) run dashboard bash -c "cd '$root/AgentGraph/dashboard' && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npx tsc --noEmit -p ." ;;
    # Serendipity (serendipity/, its own npm workspace): format, lint, types, tests.
    serendipity) run serendipity bash -c "cd '$root/serendipity' && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run check" ;;
    *) echo "unknown suite: $suite" >&2; exit 2 ;;
  esac
done
echo; echo "all suites passed: ${suites[*]}"
