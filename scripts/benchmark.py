"""Quick, dependency-light benchmark of OctoFlux's own overhead.

Measures request/sec and latency for the routing+scheduling hot path with a
mocked upstream (so results reflect OctoFlux's overhead, not real network
latency to a provider). Run:

    python scripts/benchmark.py [--requests 500] [--concurrency 20]
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import respx

from app.core.app_state import AppState
from tests.conftest import make_config
from tests.mock_provider import ok_response

CONFIG_YAML = """
server:
  require_auth: false
routing:
  max_total_attempts: 4
providers:
  bench:
    base_url: "http://bench.test/v1"
    priority: 10
    keys:
      - {name: k1, value: v1}
    models:
      - {id: bench-model, priority: 10}
    limits: {}
"""


async def run(n_requests: int, concurrency: int) -> None:
    config = make_config(CONFIG_YAML)
    state = AppState.build(config)

    with respx.mock:
        respx.post("http://bench.test/v1/chat/completions").mock(return_value=ok_response("bench"))

        latencies: list[float] = []
        sem = asyncio.Semaphore(concurrency)

        async def one_request() -> None:
            async with sem:
                start = time.perf_counter()
                await state.scheduler.execute_chat_completion(
                    requested_model="bench-model",
                    payload={"messages": [{"role": "user", "content": "hi"}]},
                    request_id="bench",
                )
                latencies.append((time.perf_counter() - start) * 1000)

        overall_start = time.perf_counter()
        await asyncio.gather(*(one_request() for _ in range(n_requests)))
        overall_elapsed = time.perf_counter() - overall_start

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    print(f"requests: {n_requests}, concurrency: {concurrency}")
    print(f"throughput: {n_requests / overall_elapsed:.1f} req/s")
    print(f"latency ms — mean: {statistics.mean(latencies):.3f}  p50: {p50:.3f}  p95: {p95:.3f}  max: {max(latencies):.3f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(run(args.requests, args.concurrency))
