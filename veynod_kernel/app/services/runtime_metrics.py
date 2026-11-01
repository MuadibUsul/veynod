"""Process-local runtime metrics for capacity acceptance.

The benchmark harness needs numbers that are independent of the client's view:
pool wait time, gate wait, merge wait and submit outcomes that finish after the
client already timed out.  Everything here is additive and in-memory; nothing
is persisted and no database migration is required.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import exc as sa_exc
from sqlalchemy.pool import AsyncAdaptedQueuePool

_MAX_SAMPLES = 200_000


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


class Histogram:
    """Exact count/sum/min/max plus bounded reservoir samples for percentiles."""

    def __init__(self, max_samples: int = _MAX_SAMPLES) -> None:
        self._lock = threading.Lock()
        self._max_samples = max_samples
        self._samples: list[float] = []
        self._seen = 0
        self.count = 0
        self.total = 0.0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self._rng = random.Random(0xC0FFEE)

    def record(self, value: float) -> None:
        value = float(value)
        with self._lock:
            self._seen += 1
            self.count += 1
            self.total += value
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)
            if len(self._samples) < self._max_samples:
                self._samples.append(value)
            else:
                index = self._rng.randrange(self._seen)
                if index < self._max_samples:
                    self._samples[index] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ordered = sorted(self._samples)
        return {
            "count": self.count,
            "sampled": len(ordered),
            "sum": round(self.total, 3),
            "min": round(self.minimum, 3) if self.minimum is not None else None,
            "max": round(self.maximum, 3) if self.maximum is not None else None,
            "p50": round(_percentile(ordered, 0.50), 3),
            "p90": round(_percentile(ordered, 0.90), 3),
            "p95": round(_percentile(ordered, 0.95), 3),
            "p99": round(_percentile(ordered, 0.99), 3),
        }

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._seen = 0
            self.count = 0
            self.total = 0.0
            self.minimum = None
            self.maximum = None


class PoolMetrics:
    """Cumulative SQLAlchemy pool checkout wait/timeout metrics."""

    def __init__(self) -> None:
        self.wait_ms = Histogram()
        self.checkouts = 0
        self.timeouts = 0
        self._lock = threading.Lock()

    def record_wait(self, elapsed_ms: float, *, timed_out: bool) -> None:
        self.wait_ms.record(elapsed_ms)
        with self._lock:
            self.checkouts += 1
            if timed_out:
                self.timeouts += 1

    def snapshot(self, pool: Any | None) -> dict[str, Any]:
        status: dict[str, Any] = {
            "checkouts": self.checkouts,
            "timeouts": self.timeouts,
            "wait_ms": self.wait_ms.snapshot(),
        }
        if pool is not None:
            try:
                size = int(pool.size())
                checked_out = int(pool.checkedout())
                overflow = int(pool.overflow())
                max_overflow = int(getattr(pool, "_max_overflow", -1))
                status.update(
                    {
                        "size": size,
                        "checkedout": checked_out,
                        "overflow": overflow,
                        "max_overflow": max_overflow,
                        "capacity": (
                            size + max_overflow if max_overflow >= 0 else None
                        ),
                        "status": str(pool.status()),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - metrics must never fail a request
                status["status_error"] = str(exc)
        return status

    def reset(self) -> None:
        self.wait_ms.reset()
        with self._lock:
            self.checkouts = 0
            self.timeouts = 0


POOL_METRICS = PoolMetrics()

_POOL_TLS = threading.local()


class InstrumentedAsyncAdaptedQueuePool(AsyncAdaptedQueuePool):
    """Async pool that measures how long a logical checkout waits.

    ``_do_get`` is the point where a caller blocks for a free connection (or a
    new one when under the pool limit), so the elapsed time captures queueing
    plus connect time.  A re-entrant call from QueuePool's overflow path is
    counted once.
    """

    def _do_get(self):  # type: ignore[override]
        if getattr(_POOL_TLS, "active", False):
            return super()._do_get()
        _POOL_TLS.active = True
        started = time.perf_counter()
        timed_out = False
        try:
            return super()._do_get()
        except sa_exc.TimeoutError:
            timed_out = True
            raise
        finally:
            _POOL_TLS.active = False
            POOL_METRICS.record_wait(
                (time.perf_counter() - started) * 1000.0, timed_out=timed_out
            )


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, int] = {}
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._reset_at: str | None = None

    # ------------------------------------------------------------- primitives
    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def observe(self, name: str, value_ms: float) -> None:
        with self._lock:
            histogram = self._histograms.get(name)
            if histogram is None:
                histogram = Histogram()
                self._histograms[name] = histogram
        histogram.record(value_ms)

    def gauge_set(self, name: str, value: int) -> None:
        with self._lock:
            self._gauges[name] = value

    def gauge_add(self, name: str, delta: int) -> None:
        with self._lock:
            self._gauges[name] = self._gauges.get(name, 0) + delta

    # ------------------------------------------------------------- semantics
    def record_claim(self, leased: bool, timing: dict[str, int] | None) -> None:
        self.inc("claim.attempts")
        self.inc("claim.leased" if leased else "claim.empty")
        for key, value in (timing or {}).items():
            self.observe(f"claim.{key}", float(value))

    def record_claim_error(self, reason: str) -> None:
        self.inc("claim.errors")
        self.inc(f"claim.error.{reason}")

    def submit_started(self) -> None:
        self.inc("submit.started")

    def record_submit_finished(
        self,
        status: str,
        merge_mode: str | None,
        timings: dict[str, int] | None,
        gate_results: list[Any] | None,
    ) -> None:
        self.inc("submit.finished")
        self.inc(f"submit.status.{status}")
        if merge_mode:
            self.inc(f"submit.merge_mode.{merge_mode}")
        for key, value in (timings or {}).items():
            self.observe(f"merge.{key}", float(value))
        if timings and "gate_pool_wait_ms" in timings:
            self.observe("submit.gate_pool_wait_ms", float(timings["gate_pool_wait_ms"]))
        self.record_gate_results(gate_results)

    def record_gate_results(self, gate_results: list[Any] | None) -> None:
        for result in gate_results or []:
            stage = getattr(result, "stage", None)
            stage_name = getattr(stage, "value", str(stage))
            passed = bool(getattr(result, "passed", False))
            duration = float(getattr(result, "duration_ms", 0) or 0)
            self.inc("gate.runs")
            self.inc(f"gate.{stage_name}.runs")
            self.inc(f"gate.{stage_name}.{'passed' if passed else 'failed'}")
            self.observe(f"gate.{stage_name}.duration_ms", duration)

    def sse_opened(self) -> None:
        self.inc("sse.total_opened")
        self.gauge_add("sse.active", 1)
        with self._lock:
            active = self._gauges.get("sse.active", 0)
            maximum = self._gauges.get("sse.max_active", 0)
            if active > maximum:
                self._gauges["sse.max_active"] = active

    def sse_closed(self) -> None:
        self.gauge_add("sse.active", -1)
        self.inc("sse.total_closed")

    # --------------------------------------------------------------- snapshot
    def snapshot(self, pool: Any | None = None) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            histograms = dict(self._histograms)
            gauges = dict(self._gauges)
        return {
            "started_at": self._started_at,
            "reset_at": self._reset_at,
            "counters": dict(sorted(counters.items())),
            "histograms_ms": {
                name: histogram.snapshot()
                for name, histogram in sorted(histograms.items())
            },
            "gauges": dict(sorted(gauges.items())),
            "db_pool": POOL_METRICS.snapshot(pool),
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            for histogram in self._histograms.values():
                histogram.reset()
            self._reset_at = datetime.now(timezone.utc).isoformat()
        # The active SSE gauge is a live measurement, not a counter; keep it.
        POOL_METRICS.reset()


RUNTIME_METRICS = RuntimeMetrics()


def get_runtime_metrics() -> RuntimeMetrics:
    return RUNTIME_METRICS


__all__ = [
    "Histogram",
    "InstrumentedAsyncAdaptedQueuePool",
    "POOL_METRICS",
    "RUNTIME_METRICS",
    "RuntimeMetrics",
    "get_runtime_metrics",
]
