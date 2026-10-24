"""Fixed-size per-provider stats for the dashboard: 24 h of per-minute counts and a latency histogram."""
from __future__ import annotations

import time

SLOTS = 1440  # one per minute, 24 h
LATENCY_EDGES_MS = [50, 100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600]  # + overflow bucket
_PERCENTILES = {"p50": 0.50, "p75": 0.75, "p90": 0.90, "p95": 0.95, "p99": 0.99}


class MinuteSeries:
    """Per provider: flat lists [minute index, requests, errors, tokens] × 1440, indexed by minute % 1440."""

    def __init__(self) -> None:
        self._data: dict[str, list[list[int]]] = {}

    def _row(self, provider: str) -> list[list[int]]:
        return self._data.setdefault(provider, [[-1] * SLOTS, [0] * SLOTS, [0] * SLOTS, [0] * SLOTS])

    def record(self, provider: str, ok: bool, tokens: int, now: float | None = None) -> None:
        minute = int((now if now is not None else time.time()) // 60)
        stamp, requests, errors, toks = self._row(provider)
        i = minute % SLOTS
        if stamp[i] != minute:
            stamp[i], requests[i], errors[i], toks[i] = minute, 0, 0, 0
        requests[i] += 1
        errors[i] += 0 if ok else 1
        toks[i] += tokens

    def series(self, provider: str, minutes: int = SLOTS, now: float | None = None) -> list[list[int]]:
        """[requests, errors, tokens] per minute, oldest first, ending at the current minute."""
        current = int((now if now is not None else time.time()) // 60)
        stamp, requests, errors, toks = self._data.get(provider) or ([-1] * SLOTS, [0] * SLOTS, [0] * SLOTS, [0] * SLOTS)
        out = []
        for minute in range(current - min(minutes, SLOTS) + 1, current + 1):
            i = minute % SLOTS
            out.append([requests[i], errors[i], toks[i]] if stamp[i] == minute else [0, 0, 0])
        return out

    def providers(self) -> list[str]:
        return list(self._data)

    def dump(self) -> dict:
        return {p: [list(col) for col in cols] for p, cols in self._data.items()}

    def load(self, data: dict) -> None:
        for provider, cols in (data or {}).items():
            if isinstance(cols, list) and len(cols) == 4 and all(len(c) == SLOTS for c in cols):
                self._data[provider] = [[int(v) for v in col] for col in cols]


class LatencyHistogram:
    """Counts per fixed latency bucket since start; percentiles are reported as the bucket's upper edge."""

    def __init__(self) -> None:
        self._counts: dict[str, list[int]] = {}

    def observe(self, provider: str, ms: float) -> None:
        counts = self._counts.setdefault(provider, [0] * (len(LATENCY_EDGES_MS) + 1))
        index = next((i for i, edge in enumerate(LATENCY_EDGES_MS) if ms <= edge), len(LATENCY_EDGES_MS))
        counts[index] += 1

    @staticmethod
    def _from_counts(counts: list[int] | None) -> dict[str, int | None]:
        total = sum(counts) if counts else 0
        if not total:
            return {name: None for name in _PERCENTILES}
        out = {}
        for name, fraction in _PERCENTILES.items():
            target, seen = fraction * total, 0
            for i, count in enumerate(counts):
                seen += count
                if seen >= target:
                    # Overflow bucket reports the last edge (">25.6 s").
                    out[name] = LATENCY_EDGES_MS[min(i, len(LATENCY_EDGES_MS) - 1)]
                    break
        return out

    def percentiles(self, provider: str) -> dict[str, int | None]:
        return self._from_counts(self._counts.get(provider))

    def percentiles_all(self) -> dict[str, int | None]:
        if not self._counts:
            return self._from_counts(None)
        return self._from_counts([sum(col) for col in zip(*self._counts.values())])

    def providers(self) -> list[str]:
        return list(self._counts)
