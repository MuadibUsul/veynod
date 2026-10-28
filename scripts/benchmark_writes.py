"""Write-path benchmark: single writes, batches of 25, and a concurrent burst.

Creates an isolated workspace through the admin API, then measures 20 single
PUTs, 5 sequential batches of 25 and 40 concurrent batches of 25 (1000
documents, the shape of test_seq_is_gap_free_under_concurrent_writes).
Prints JSON with times in milliseconds (the burst in seconds).

    python scripts/benchmark_writes.py http://127.0.0.1:8000 --admin-token "$ADMIN_TOKEN"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN", ""))
    parser.add_argument("--tier", default="developer_5usd")
    parser.add_argument("--no-burst", action="store_true")
    args = parser.parse_args()
    admin = {"Authorization": f"Bearer {args.admin_token}"}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=900) as client:
        created = await client.post("/v1/admin/workspaces", headers=admin, json={
            "name": f"bench-writes-{uuid.uuid4().hex[:8]}", "tier": args.tier})
        created.raise_for_status()
        headers = {"Authorization": f"Bearer {created.json()['api_key']}"}
        result: dict[str, float] = {}

        singles = []
        for index in range(20):
            start = time.perf_counter()
            response = await client.put(f"/v1/documents/single-{index}", headers=headers,
                                        json={"content": f"single {index}", "expected_version": 0})
            response.raise_for_status()
            singles.append((time.perf_counter() - start) * 1000)
        batches = []
        for number in range(5):
            start = time.perf_counter()
            response = await client.post("/v1/documents/batch", headers=headers, json={
                "items": [{"doc_key": f"seq-{number}-{index}", "namespace": "bench",
                           "content": f"batch {number} {index}", "expected_version": 0}
                          for index in range(25)]})
            response.raise_for_status()
            batches.append((time.perf_counter() - start) * 1000)
        result.update(single_put_ms_median=round(statistics.median(singles), 1),
                      single_put_ms_p90=round(statistics.quantiles(singles, n=10)[-1], 1),
                      batch25_ms_median=round(statistics.median(batches), 1))

        if not args.no_burst:
            namespace = f"burst{uuid.uuid4().hex[:4]}"
            failures: list[str] = []

            async def batch(first: int) -> None:
                response = await client.post("/v1/documents/batch", headers=headers, json={
                    "items": [{"doc_key": f"b-{first + index:04d}", "namespace": namespace,
                               "content": f"burst {first + index}", "expected_version": 0}
                              for index in range(25)]})
                if response.status_code != 200:
                    failures.append(f"{response.status_code} {response.text[:200]}")

            start = time.perf_counter()
            await asyncio.gather(*(batch(first) for first in range(0, 1000, 25)))
            result.update(burst_1000_total_s=round(time.perf_counter() - start, 1),
                          burst_failed_batches=len(failures))
            for failure in failures[:3]:
                print(failure)
        print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
