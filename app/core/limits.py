"""Local rate limiting.

Enforces operator-declared limits (LimitsConfig) purely in-memory using
sliding-window counters. This is *not* about respecting the upstream
provider's own limits (we don't know those precisely) — it's about letting
the operator say "treat this provider/key as 30 RPM" and having OctoFlux
hold requests to that budget locally, proactively, before ever calling
upstream.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from app.models.provider import LimitsConfig

_WINDOWS = {
    "requests_per_second": 1.0,
    "requests_per_minute": 60.0,
    "requests_per_hour": 3600.0,
    "requests_per_day": 86400.0,
    "tokens_per_minute": 60.0,
    "tokens_per_hour": 3600.0,
    "tokens_per_day": 86400.0,
}

_REQUEST_FIELDS = {"requests_per_second", "requests_per_minute", "requests_per_hour", "requests_per_day"}
_TOKEN_FIELDS = {"tokens_per_minute", "tokens_per_hour", "tokens_per_day"}


class _SlidingWindowCounter:
    """Timestamped-event deque; O(1) amortized on trim."""

    __slots__ = ("events", "window_seconds", "limit")

    def __init__(self, window_seconds: float, limit: int) -> None:
        self.events: deque[tuple[float, int]] = deque()
        self.window_seconds = window_seconds
        self.limit = limit

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def current_total(self, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        self._trim(now)
        return sum(v for _, v in self.events)

    def would_exceed(self, amount: int, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return self.current_total(now) + amount > self.limit

    def record(self, amount: int, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self.events.append((now, amount))


@dataclass
class LimitState:
    """All active counters for one target (a provider or a key)."""

    counters: dict[str, _SlidingWindowCounter] = field(default_factory=dict)
    in_flight: int = 0
    concurrency_limit: int | None = None

    @classmethod
    def from_config(cls, cfg: LimitsConfig) -> "LimitState":
        state = cls(concurrency_limit=cfg.concurrent_requests)
        for field_name, window in _WINDOWS.items():
            limit = getattr(cfg, field_name)
            if limit is not None:
                state.counters[field_name] = _SlidingWindowCounter(window, int(limit) if field_name != "requests_per_second" else int(limit))
        return state

    def has_request_budget(self) -> bool:
        if self.concurrency_limit is not None and self.in_flight >= self.concurrency_limit:
            return False
        for name in _REQUEST_FIELDS:
            counter = self.counters.get(name)
            if counter is not None and counter.would_exceed(1):
                return False
        return True

    def has_token_budget(self, estimated_tokens: int) -> bool:
        for name in _TOKEN_FIELDS:
            counter = self.counters.get(name)
            if counter is not None and counter.would_exceed(estimated_tokens):
                return False
        return True

    def record_request_start(self) -> None:
        self.in_flight += 1
        for name in _REQUEST_FIELDS:
            counter = self.counters.get(name)
            if counter is not None:
                counter.record(1)

    def record_request_end(self, tokens_used: int = 0) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        if tokens_used:
            for name in _TOKEN_FIELDS:
                counter = self.counters.get(name)
                if counter is not None:
                    counter.record(tokens_used)

    def snapshot(self) -> dict:
        return {
            "in_flight": self.in_flight,
            "concurrency_limit": self.concurrency_limit,
            **{name: {"used": c.current_total(), "limit": c.limit} for name, c in self.counters.items()},
        }


@dataclass
class LimitsRegistry:
    _provider_limits: dict[str, LimitState] = field(default_factory=dict)
    _key_limits: dict[tuple[str, str], LimitState] = field(default_factory=dict)

    def provider(self, provider_id: str, cfg: LimitsConfig) -> LimitState:
        if provider_id not in self._provider_limits:
            self._provider_limits[provider_id] = LimitState.from_config(cfg)
        return self._provider_limits[provider_id]

    def key(self, provider_id: str, key_name: str, cfg: LimitsConfig | None) -> LimitState | None:
        if cfg is None:
            return None
        cache_key = (provider_id, key_name)
        if cache_key not in self._key_limits:
            self._key_limits[cache_key] = LimitState.from_config(cfg)
        return self._key_limits[cache_key]

    def snapshot(self) -> dict:
        return {
            "providers": {k: v.snapshot() for k, v in self._provider_limits.items()},
            "keys": {f"{k[0]}:{k[1]}": v.snapshot() for k, v in self._key_limits.items()},
        }
