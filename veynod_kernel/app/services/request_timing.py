"""Request timing middleware and benchmark latency headers."""

from __future__ import annotations

import time

from fastapi import FastAPI, Request

from app.services.runtime_metrics import get_runtime_metrics


def install_request_timing(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_timing(request: Request, call_next):
        started = time.perf_counter()
        sent_at = request.headers.get("X-Bench-Sent-At")
        if sent_at:
            try:
                get_runtime_metrics().observe(
                    "http.pre_middleware_ms",
                    time.time() * 1000.0 - float(sent_at),
                )
            except ValueError:
                pass
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        path = request.url.path
        if path.endswith("/syscalls/submit_patch"):
            get_runtime_metrics().observe("http.submit_ms", elapsed_ms)
        elif path.endswith("/syscalls/pull_task_bundle"):
            get_runtime_metrics().observe("http.claim_ms", elapsed_ms)
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.3f}"
        return response
