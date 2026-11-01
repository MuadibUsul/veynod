"""Process supervisor for the Agent Runtime worker (Phase 2K).

``agent_runtime.worker_main`` already supervises its own in-process worker
loops; this script supervises the **process**.  It restarts a crashed worker
with exponential backoff, optionally restarts it when the worker's metrics
endpoint stops answering, and forwards SIGINT/SIGTERM for a clean shutdown.

    python scripts/runtime_supervisor.py
    python scripts/runtime_supervisor.py --metrics-port 8031 --health-checks 3

On Windows, run it under NSSM/Task Scheduler, or keep it in a terminal.  On
Linux, a systemd unit with ``Restart=always`` is equivalent and preferred.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _metrics_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/metrics", timeout=timeout
        ) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not healthy"
        return False


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-port", type=int, default=int(
        os.environ.get("RUNTIME_WORKER_METRICS_PORT", "8031")
    ))
    parser.add_argument(
        "--health-interval",
        type=float,
        default=30.0,
        help="seconds between metrics checks; 0 disables the check",
    )
    parser.add_argument(
        "--health-checks",
        type=int,
        default=3,
        help="consecutive failed checks before the worker is restarted",
    )
    parser.add_argument("--min-backoff", type=float, default=1.0)
    parser.add_argument("--max-backoff", type=float, default=30.0)
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=60.0,
        help="uptime after which the backoff resets",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    env["RUNTIME_WORKER_METRICS_PORT"] = str(args.metrics_port)
    command = [sys.executable, "-m", "agent_runtime.worker_main"]
    stopping = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            pass

    if args.health_interval and _port_open(args.metrics_port):
        print(
            f"runtime supervisor: port {args.metrics_port} is already in use; "
            "refusing to start a second worker",
            file=sys.stderr,
        )
        return 2

    backoff = args.min_backoff
    while not stopping:
        started = time.monotonic()
        child = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        print(f"runtime supervisor: worker started pid={child.pid}", flush=True)
        last_health = time.monotonic()
        failed_checks = 0
        reason = "exited"
        while not stopping:
            code = child.poll()
            if code is not None:
                reason = f"exited with code {code}"
                break
            if not args.health_interval:
                time.sleep(1.0)
                continue
            time.sleep(min(1.0, args.health_interval))
            if time.monotonic() - last_health < args.health_interval:
                continue
            last_health = time.monotonic()
            if _metrics_ok(args.metrics_port):
                failed_checks = 0
                continue
            failed_checks += 1
            if failed_checks >= max(1, args.health_checks):
                reason = f"metrics unhealthy {failed_checks}x"
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                break
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if stopping:
            break
        uptime = time.monotonic() - started
        if uptime >= args.stable_seconds:
            backoff = args.min_backoff
        print(
            f"runtime supervisor: worker {reason} after {uptime:.1f}s; "
            f"restarting in {backoff:.0f}s",
            flush=True,
        )
        deadline = time.monotonic() + backoff
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.2)
        backoff = min(args.max_backoff, backoff * 2)
    print("runtime supervisor: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
