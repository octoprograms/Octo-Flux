"""Usage tracking.

In-memory by default (no mandatory database). Behind a small interface so a
persistent backend (e.g. SQLite) can be added later without touching the
router or API layers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Counters:
    requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_latency_ms: float = 0.0
    rate_limit_events: int = 0
    fallback_count: int = 0
    timeout_count: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def average_latency_ms(self) -> float:
        if self.successful_requests == 0:
            return 0.0
        return self.total_latency_ms / self.successful_requests

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "average_latency_ms": round(self.average_latency_ms, 2),
            "rate_limit_events": self.rate_limit_events,
            "fallback_count": self.fallback_count,
            "timeout_count": self.timeout_count,
        }


class UsageStore(Protocol):
    def record_request(self, provider: str, model: str, key: str, success: bool) -> None: ...
    def record_tokens(self, provider: str, model: str, key: str, input_tokens: int, output_tokens: int) -> None: ...
    def record_latency(self, provider: str, model: str, ms: float) -> None: ...


@dataclass
class UsageRegistry:
    """In-memory implementation of UsageStore, with per-target breakdown."""

    global_counters: Counters = field(default_factory=Counters)
    _provider_counters: dict[str, Counters] = field(default_factory=dict)
    _model_counters: dict[tuple[str, str], Counters] = field(default_factory=dict)
    _key_counters: dict[tuple[str, str], Counters] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def _get(self, store: dict, key) -> Counters:
        if key not in store:
            store[key] = Counters()
        return store[key]

    def record_attempt(self, provider: str, model: str, key: str, success: bool, latency_ms: float) -> None:
        for c in (
            self.global_counters,
            self._get(self._provider_counters, provider),
            self._get(self._model_counters, (provider, model)),
            self._get(self._key_counters, (provider, key)),
        ):
            c.requests += 1
            if success:
                c.successful_requests += 1
                c.total_latency_ms += latency_ms
            else:
                c.failed_requests += 1

    def record_tokens(self, provider: str, model: str, key: str, input_tokens: int, output_tokens: int) -> None:
        for c in (
            self.global_counters,
            self._get(self._provider_counters, provider),
            self._get(self._model_counters, (provider, model)),
            self._get(self._key_counters, (provider, key)),
        ):
            c.input_tokens += input_tokens
            c.output_tokens += output_tokens

    def record_rate_limited(self, provider: str) -> None:
        self.global_counters.rate_limit_events += 1
        self._get(self._provider_counters, provider).rate_limit_events += 1

    def record_timeout(self, provider: str) -> None:
        self.global_counters.timeout_count += 1
        self._get(self._provider_counters, provider).timeout_count += 1

    def record_fallback(self) -> None:
        self.global_counters.fallback_count += 1

    def snapshot(self) -> dict:
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "global": self.global_counters.as_dict(),
            "providers": {k: v.as_dict() for k, v in self._provider_counters.items()},
            "models": {f"{k[0]}/{k[1]}": v.as_dict() for k, v in self._model_counters.items()},
            "keys": {f"{k[0]}:{k[1]}": v.as_dict() for k, v in self._key_counters.items()},
        }
