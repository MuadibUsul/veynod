"""Test fixture: a local echo tool endpoint for the Agent Runtime.

NOT a production tool. Production Runs use ``RUNTIME_TOOL_MODE=kernel`` and the
``KernelToolRuntime`` adapter (agent_runtime/tools/kernel.py); this server only
exists so the generic ``HTTPToolRuntime`` protocol has a deterministic target
in tests and local wiring demos.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/tools/"):
            self._send(404, {"error": "not found"})
            return
        name = self.path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        if name != "echo":
            self._send(400, {"ok": False, "error": f"unknown tool {name}"})
            return
        self._send(
            200,
            {
                "ok": True,
                "output": {
                    "tool": "echo",
                    "arguments": body.get("arguments") or {},
                    "idempotency_key": self.headers.get("Idempotency-Key"),
                },
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


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8003), Handler).serve_forever()
