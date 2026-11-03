"""A stored key is not a connection: `up` must ask the board before keeping it."""

from __future__ import annotations

import pytest

from tests.conftest import run_server


class _RuntimeStub:
    """Enough of the runtime API for /run to create and start a Run."""

    def __init__(self) -> None:
        import threading
        import uuid
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        state = {"runs": {}, "lock": threading.Lock()}

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, payload) -> None:
                import json

                body = json.dumps(payload, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/healthz":
                    return self._send(200, {"status": "ok"})
                if "/runs/" in path:
                    run_id = path.rsplit("/", 1)[-1]
                    run = state["runs"].get(run_id)
                    return self._send(200, run) if run else self._send(404, {"detail": "no"})
                return self._send(404, {"detail": "no"})

            def do_POST(self) -> None:  # noqa: N802
                import json

                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                if path == "/v1/runs":
                    run_id = str(uuid.uuid4())
                    with state["lock"]:
                        state["runs"][run_id] = {"run_id": run_id, "status": "CREATED",
                                                 "version": 1, "budget": body.get("budget") or {}}
                    return self._send(201, state["runs"][run_id])
                if path.endswith("/start"):
                    run_id = path.split("/")[3]
                    with state["lock"]:
                        state["runs"][run_id]["status"] = "RUNNING"
                    return self._send(200, state["runs"][run_id])
                return self._send(404, {"detail": "no"})

            def log_message(self, format, *args):  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture(scope="module")
def owned_workspace():
    """A workspace key the test controls, so revocation is observable."""
    stub = _RuntimeStub()

    async def _run():
        async with run_server(
            {"AGENT_RUNTIME_URL": stub.base_url, "AGENT_RUNTIME_KEY": "k",
             "AGENT_RUNTIME_PROJECTION_ENABLED": "false",
             "CODE_PROJECTION_ENABLED": "false",
             "EVENT_DISPATCH_ENABLED": "false",
             "SCHEDULER_ENABLED": "false"},
            ".server-run-gate.log",
        ) as base_url:
            yield base_url

    import asyncio

    loop = asyncio.new_event_loop()
    gen = _run()
    base = loop.run_until_complete(gen.__anext__())
    try:
        yield base, loop
    finally:
        try:
            loop.run_until_complete(gen.__anext__())
        except StopAsyncIteration:
            pass
        loop.close()
        stub.close()


def test_run_requires_an_agent_side_claim(owned_workspace) -> None:
    """An agent (workspace key) may start a Run; a WEB_UI user session may not."""
    import asyncio

    import httpx

    base, loop = owned_workspace

    async def scenario() -> None:
        async with httpx.AsyncClient(base_url=base, timeout=60) as client:
            created = await client.post(
                "/v1/admin/workspaces",
                headers={"Authorization": "Bearer test-admin"},
                json={"name": "run-gate", "tier": "developer_5usd"},
            )
            assert created.status_code == 201, created.text
            key = created.json()["api_key"]
            agent = {"Authorization": f"Bearer {key}"}
            key_task = "run-gate-1"
            made = await client.post(
                "/v1/tasks", headers=agent,
                json={"task_key": key_task, "title": "t", "description": "do it"},
            )
            assert made.status_code in (200, 201), made.text
            started = await client.post(f"/v1/tasks/{key_task}/run", headers=agent, json={})
            assert started.status_code == 200, started.text

    loop.run_until_complete(scenario())
