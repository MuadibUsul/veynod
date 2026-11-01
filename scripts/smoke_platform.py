"""Verify the Veynod Platform integration end to end.

Prereqs:
  * AgentGraph on :8000
  * Veynod Kernel on :8001
  * Veynod Platform gateway on :8010
  * At least one code run that reached SYNCED

The script is read-only; it does not create or mutate tasks.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any


def get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default="http://127.0.0.1:8010")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()
    base = args.platform.rstrip("/")

    health = get(f"{base}/healthz")
    if not health.get("ok"):
        print(json.dumps(health, indent=2))
        return 1

    runs = get(f"{base}/v1/platform/code-runs")
    if not runs:
        print("no code runs; create one through POST /v1/platform/code-runs")
        return 1
    run = next((item for item in runs if item["id"] == args.run_id), None) if args.run_id else runs[0]
    if run is None:
        print(f"run {args.run_id!r} not found")
        return 1
    if run.get("status") != "SYNCED":
        print(json.dumps(run, indent=2))
        print("run is not SYNCED yet")
        return 1

    audit = get(f"{base}/v1/platform/code-runs/{run['id']}/audit")
    gates = audit.get("gates") or {}
    if not all((gates.get(kind) or {}).get("status") == "PASS" for kind in ("CI", "TESTS")):
        print(json.dumps(audit, indent=2))
        print("AgentGraph gates are not PASS")
        return 1

    print(json.dumps({
        "ok": True,
        "run": run["id"],
        "agentgraph_task": run["agentgraph_task_key"],
        "kernel_task": run["kernel_task_id"],
        "commit_sha": run["commit_sha"],
        "kernel_status": run["kernel_status"],
        "platform_status": run["status"],
        "gates": {kind: gates[kind]["status"] for kind in ("CI", "TESTS")},
        "services": {
            "agentgraph": health["agentgraph"].get("version"),
            "kernel": health["kernel"].get("version"),
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
