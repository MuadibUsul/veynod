#!/usr/bin/env bash
# Export the public CLI / MCP bridge (npm `veynod`, github.com/veynod/veynod)
# from its single source, AgentGraph/cli, into a checkout of the public repo.
#
#   scripts/export_public_bridge.sh /path/to/veynod-public
#
# Tracked files only, so node_modules and local state never leave. Files the
# public repo has but the source no longer does are removed, except the
# public repo's own .github. Then review, commit and push
# there; its workflow publishes npm. The old hand-synced copy in
# AgentGraph/open-source-repo drifted from the source and is gone.
set -euo pipefail

target="${1:?usage: scripts/export_public_bridge.sh <public-repo-checkout>}"
root="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
source="$root/AgentGraph/cli"

[ -d "$target/.git" ] || { echo "not a git checkout: $target" >&2; exit 1; }
(cd "$source" && npm test --silent)

list="$(mktemp)"
trap 'rm -f "$list"' EXIT
git -C "$source" ls-files > "$list"
tar -C "$source" -cf - -T "$list" | tar -C "$target" -xf -
# Remove what the source no longer tracks.
(cd "$target" && git ls-files | while read -r file; do
  case "$file" in .github/*) continue ;; esac
  grep -qxF "$file" "$list" || git rm -q "$file"
done)
echo "exported $(wc -l < "$list") files to $target; review with: git -C $target status"
