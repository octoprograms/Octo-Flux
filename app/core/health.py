"""Runtime health state.

Deliberately kept separate from `app.models.provider` config objects: config
is reloadable/immutable, health state is mutable and must survive a config
reload for targets that still exist. Request health is updated lazily;
provider probe results are maintained separately by app.core.provider_checks.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from app.core.model_stats import ModelStatsRegistry


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


@dataclass
class HealthState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_failure_reason: str | None = None
    last_success_at: float | None = None
    total_failures: int = 0
    last_check_at: float | None = None
    last_check_ok: bool | None = None
    last_check_reason: str | None = None
    last_check_latency_ms: float | None = None
    last_check_model_ids: list[str] | None = None
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    resets_at: float | None = None  # wall clock
    # shortcut: largest value ever seen, not per window; reset it on each window if headroom looks off.
    max_remaining_requests: int | None = None

    def record_rate_limit(self, info) -> None:
        """Store upstream x-ratelimit-* values; cool down until reset when a budget hits 0."""
        self.remaining_requests = info.remaining_requests
        self.remaining_tokens = info.remaining_tokens
        self.resets_at = info.resets_at
        if info.remaining_requests is not None:
            self.max_remaining_requests = max(self.max_remaining_requests or 0, info.remaining_requests)
        if 0 in (info.remaining_requests, info.remaining_tokens) and info.resets_at is not None:
            self.record_cooldown(max(0.0, info.resets_at - time.time()))

    def record_check(
        self, ok: bool, reason: str | None, latency_ms: float, model_ids: list[str] | None = None
    ) -> None:
        self.last_check_at = time.time()
        self.last_check_ok = ok
        self.last_check_reason = reason
        self.last_check_latency_ms = round(latency_ms, 1)
        self.last_check_model_ids = model_ids

    def record_failure(self, reason: str, cooldown_seconds: float, failure_threshold: int) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure_reason = reason
        if self.consecutive_failures >= failure_threshold:
            self.cooldown_until = time.monotonic() + cooldown_seconds

    def record_cooldown(self, cooldown_seconds: float) -> None:
        """Apply cooldown directly (e.g. for rate_limited) without requiring
        the failure_threshold to be crossed — a single 429 should pause a key
        immediately."""
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + cooldown_seconds)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.last_success_at = time.monotonic()

    def is_in_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def status(self, failure_threshold: int) -> HealthStatus:
        if self.is_in_cooldown():
            return HealthStatus.COOLDOWN
        if self.consecutive_failures >= failure_threshold:
            return HealthStatus.UNHEALTHY
        if self.consecutive_failures > 0:
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def seconds_until_available(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())



@dataclass
class HealthRegistry:
    """Keyed health state for providers and keys."""

    _provider_health: dict[str, HealthState] = field(default_factory=dict)
    _key_health: dict[tuple[str, str], HealthState] = field(default_factory=dict)
    _model_health: dict[tuple[str, str], HealthState] = field(default_factory=dict)
    model_stats: ModelStatsRegistry = field(default_factory=ModelStatsRegistry)

    def drop_idle_models(self, keep: set[tuple[str, str]]) -> int:
        """Forget health records of discovered models that are idle (not configured, no cooldown, no stats)."""
        idle = [
            k for k, h in self._model_health.items()
            if k not in keep and not h.is_in_cooldown() and self.model_stats.peek(*k) is None
        ]
        for k in idle:
            del self._model_health[k]
        return len(idle)

    def provider(self, provider_id: str) -> HealthState:
        return self._provider_health.setdefault(provider_id, HealthState())

    def key(self, provider_id: str, key_name: str) -> HealthState:
        return self._key_health.setdefault((provider_id, key_name), HealthState())

    def model(self, provider_id: str, model_id: str) -> HealthState:
        return self._model_health.setdefault((provider_id, model_id), HealthState())

    def record_check(
        self,
        provider_id: str,
        key_name: str,
        *,
        ok: bool,
        reason: str | None,
        latency_ms: float,
        model_ids: list[str] | None = None,
    ) -> None:
        self.key(provider_id, key_name).record_check(ok, reason, latency_ms, model_ids)
        provider = self.provider(provider_id)
        provider.last_check_at = time.time()
        provider.last_check_ok = ok if provider.last_check_ok is None else provider.last_check_ok or ok
        provider.last_check_reason = reason if not ok else None
        provider.last_check_latency_ms = round(latency_ms, 1)

    def snapshot(self) -> dict:
        return {
            "providers": {
                pid: {
                    "consecutive_failures": h.consecutive_failures,
                    "total_failures": h.total_failures,
                    "in_cooldown": h.is_in_cooldown(),
                    "seconds_until_available": h.seconds_until_available(),
                    "last_check_at": h.last_check_at,
                    "last_check_ok": h.last_check_ok,
                    "last_check_reason": h.last_check_reason,
                    "last_check_latency_ms": h.last_check_latency_ms,
                    "last_check_model_ids": h.last_check_model_ids,
                }
                for pid, h in self._provider_health.items()
            },
            "keys": {
                f"{pid}:{kname}": {
                    "consecutive_failures": h.consecutive_failures,
                    "total_failures": h.total_failures,
                    "in_cooldown": h.is_in_cooldown(),
                    "seconds_until_available": h.seconds_until_available(),
                    "last_check_at": h.last_check_at,
                    "last_check_ok": h.last_check_ok,
                    "last_check_reason": h.last_check_reason,
                    "last_check_latency_ms": h.last_check_latency_ms,
                    "last_check_model_ids": h.last_check_model_ids,
                }
                for (pid, kname), h in self._key_health.items()
            },
            "models": {
                f"{pid}:{mid}": {
                    "consecutive_failures": h.consecutive_failures,
                    "total_failures": h.total_failures,
                    "in_cooldown": h.is_in_cooldown(),
                    "seconds_until_available": h.seconds_until_available(),
                    "last_failure_reason": h.last_failure_reason,
                    "last_success_at": h.last_success_at,
                }
                for (pid, mid), h in self._model_health.items()
            },
        }
