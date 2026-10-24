"""Per-model outcome stats used to rank discovered free models.

Bounded memory: 24 hourly (successes, failures) buckets and the last 32
latencies per model; idle models are evicted after 24 h.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field

_HOUR = 3600.0
_DAY = 86400.0


@dataclass
class ModelStats:
    # [hour slot, successes, failures] per bucket, indexed by slot % 24
    buckets: list[list[int]] = field(default_factory=lambda: [[-1, 0, 0] for _ in range(24)])
    latencies: deque = field(default_factory=lambda: deque(maxlen=32))  # (ts, ms)
    last_used: float = 0.0

    def record(self, success: bool, latency_ms: float, now: float) -> None:
        slot = int(now // _HOUR)
        bucket = self.buckets[slot % 24]
        if bucket[0] != slot:
            bucket[:] = [slot, 0, 0]
        bucket[1 if success else 2] += 1
        if success:
            self.latencies.append((now, latency_ms))
        self.last_used = max(self.last_used, now)

    def _sum(self, index: int, now: float) -> int:
        current = int(now // _HOUR)
        return sum(b[index] for b in self.buckets if current - 24 < b[0] <= current)

    def successes_24h(self, now: float) -> int:
        return self._sum(1, now)

    def failures_24h(self, now: float) -> int:
        return self._sum(2, now)

    def p50_ms(self, now: float) -> float | None:
        recent = [ms for ts, ms in self.latencies if ts > now - _DAY]
        return statistics.median(recent) if recent else None


def score(position: int, stats: ModelStats, now: float) -> float:
    """Lower is better: OpenRouter's position, pulled up by recent successes, pushed down by failures and slowness."""
    p50 = stats.p50_ms(now)
    latency_penalty = min(max(0, math.ceil((p50 / 1000 - 5) / 5)), 5) if p50 is not None else 0
    return (
        position
        - 3 * min(stats.successes_24h(now), 5)
        + 5 * min(stats.failures_24h(now), 5)
        + latency_penalty
    )


@dataclass
class ModelStatsRegistry:
    _stats: dict[tuple[str, str], ModelStats] = field(default_factory=dict)

    def get(self, provider_id: str, model_id: str) -> ModelStats:
        return self._stats.setdefault((provider_id, model_id), ModelStats())

    def peek(self, provider_id: str, model_id: str) -> ModelStats | None:
        return self._stats.get((provider_id, model_id))

    def evict_idle(self, now: float, max_idle: float = _DAY) -> int:
        idle = [k for k, s in self._stats.items() if s.last_used < now - max_idle]
        for k in idle:
            del self._stats[k]
        return len(idle)
