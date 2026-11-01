"""OpenAI-compatible scripted model for chaos/load tests.

The Runtime worker talks to a real HTTP model gateway; this server answers with
the same deterministic decision sequence the E2E uses, so a test can kill the
worker, restart it, and observe lease takeover instead of mocking it away.

    python scripts/scripted_model_server.py --port 8190 --delay 0

Modes:
  normal     immediate answers
  slow       --delay seconds before every answer (model timeout scenarios)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from e2e_unified import ScriptedKernelModel, diff_for_calc  # noqa: E402


def build_handler(delay: float, model=None):
    model = model or ScriptedKernelModel(diff_for_calc())
    calls = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send(200, {"status": "ok", "calls": calls["n"]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.endswith("/chat/completions"):
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            run: dict = {}
            checkpoint: dict = {}
            for message in body.get("messages", []):
                if message.get("role") != "user":
                    continue
                try:
                    payload = json.loads(message.get("content") or "{}")
                except ValueError:
                    continue
                run = payload.get("run") or {}
                checkpoint = payload.get("checkpoint") or {}
            slowdown = getattr(model, "delay", None)
            wait_for = max(float(delay or 0.0), float(slowdown or 0.0))
            if wait_for:
                time.sleep(wait_for)
            import asyncio

            action = asyncio.new_event_loop().run_until_complete(
                model.decide(run, checkpoint)
            )
            calls["n"] += 1
            content = json.dumps(
                {
                    "kind": action.kind,
                    "tool": action.tool,
                    "arguments": action.arguments,
                    "idempotency_key": action.idempotency_key,
                    "output": action.output,
                    "reason": action.reason,
                }
            )
            self._send(
                200,
                {
                    "id": f"scripted-{calls['n']}",
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(args.delay))
    print(f"scripted model on 127.0.0.1:{args.port} delay={args.delay}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
