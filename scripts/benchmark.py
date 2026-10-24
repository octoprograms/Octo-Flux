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


MEMORY_LIMIT_MB = 80.0
MAX_GROWTH_MB = 2.0


def rss_mb() -> float | None:
    """Current resident set size in MB (Linux /proc, or Windows working set via ctypes)."""
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        counters = Counters(cb=ctypes.sizeof(Counters))
        get_info = ctypes.WinDLL("psapi").GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if get_info(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / (1024 * 1024)
    return None


async def run_memory(n_requests: int = 10_000, concurrency: int = 20) -> int:
    """Send n mocked requests; print RSS at 0 / half / end. Exit code 1 if over budget or growing."""
    from app.observability.logging import configure_logging

    configure_logging("WARNING")
    state = AppState.build(make_config(CONFIG_YAML), usage_db=":memory:")
    readings: list[float] = []
    with respx.mock as mock:
        respx.post("http://bench.test/v1/chat/completions").mock(return_value=ok_response("bench"))
        sem = asyncio.Semaphore(concurrency)

        async def one_request() -> None:
            async with sem:
                await state.scheduler.execute_chat_completion(
                    requested_model="bench-model",
                    payload={"messages": [{"role": "user", "content": "hi"}]},
                    request_id="bench",
                )

        half = n_requests // 2
        for done, batch in ((0, 0), (half, half), (n_requests, n_requests - half)):
            for start in range(0, batch, 500):
                await asyncio.gather(*(one_request() for _ in range(min(500, batch - start))))
                mock.reset()  # respx keeps every mocked call; drop them so only OctoFlux is measured
            mb = rss_mb()
            if mb is None:
                print("memory mode needs Linux/Docker (or Windows)")
                return 1
            readings.append(mb)
            print(f"after {done:>6} requests: RSS {mb:.1f} MB")

    growth = readings[2] - readings[1]
    ok = readings[2] < MEMORY_LIMIT_MB and growth <= MAX_GROWTH_MB
    print(f"growth {half}->{n_requests}: {growth:+.1f} MB - {'PASS' if ok else 'FAIL'} "
          f"(limit {MEMORY_LIMIT_MB:.0f} MB, max growth {MAX_GROWTH_MB:.0f} MB)")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--memory", action="store_true", help="10 000 requests; check RSS < 80 MB and no growth")
    args = parser.parse_args()
    if args.memory:
        sys.exit(asyncio.run(run_memory()))
    asyncio.run(run(args.requests, args.concurrency))
