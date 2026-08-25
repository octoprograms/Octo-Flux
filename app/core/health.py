"""Runtime health state.

Deliberately kept separate from `app.models.provider` config objects: config
is reloadable/immutable, health state is mutable and must survive a config
reload for targets that still exist. No background polling — cooldown
expiry is checked lazily whenever a candidate is considered (see
app.core.router), so health tracking generates zero extra network traffic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


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

    def provider(self, provider_id: str) -> HealthState:
        return self._provider_health.setdefault(provider_id, HealthState())

    def key(self, provider_id: str, key_name: str) -> HealthState:
        return self._key_health.setdefault((provider_id, key_name), HealthState())

    def snapshot(self) -> dict:
        return {
            "providers": {
                pid: {
                    "consecutive_failures": h.consecutive_failures,
                    "total_failures": h.total_failures,
                    "in_cooldown": h.is_in_cooldown(),
                    "seconds_until_available": h.seconds_until_available(),
                }
                for pid, h in self._provider_health.items()
            },
            "keys": {
                f"{pid}:{kname}": {
                    "consecutive_failures": h.consecutive_failures,
                    "total_failures": h.total_failures,
                    "in_cooldown": h.is_in_cooldown(),
                    "seconds_until_available": h.seconds_until_available(),
                }
                for (pid, kname), h in self._key_health.items()
            },
        }
