"""Local rate limiting.

Enforces operator-declared limits (LimitsConfig) purely in-memory using
bucketed sliding-window counters. This is *not* about respecting the upstream
provider's own limits (we don't know those precisely) — it's about letting
the operator say "treat this provider/key as 30 RPM" and having OctoFlux
hold requests to that budget locally, proactively, before ever calling
upstream.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from app.models.provider import KeyConfig, LimitsConfig, OctoFluxConfig, ProviderConfig

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
    """Fixed ring of `buckets` counts covering one window; memory is constant.

    Accurate to one bucket (window / buckets). Uses wall-clock time so a
    dump() survives a restart."""

    __slots__ = ("counts", "window_seconds", "limit", "bucket_seconds", "last_slot")

    def __init__(self, window_seconds: float, limit: int, buckets: int = 60) -> None:
        self.counts = [0] * buckets
        self.window_seconds = window_seconds
        self.limit = limit
        self.bucket_seconds = window_seconds / buckets
        self.last_slot = 0

    def _advance(self, now: float) -> int:
        slot = int(now // self.bucket_seconds)
        size = len(self.counts)
        if slot - self.last_slot >= size:
            self.counts = [0] * size
        else:
            for s in range(self.last_slot + 1, slot + 1):
                self.counts[s % size] = 0
        self.last_slot = max(slot, self.last_slot)
        return slot

    def current_total(self, now: float | None = None) -> int:
        self._advance(now if now is not None else time.time())
        return sum(self.counts)

    def would_exceed(self, amount: int, now: float | None = None) -> bool:
        return self.current_total(now) + amount > self.limit

    def record(self, amount: int, now: float | None = None) -> None:
        slot = self._advance(now if now is not None else time.time())
        self.counts[slot % len(self.counts)] += amount

    def dump(self) -> dict:
        return {"start": self.last_slot * self.bucket_seconds, "counts": list(self.counts)}

    def load(self, data: dict) -> None:
        counts = data.get("counts")
        if isinstance(counts, list) and len(counts) == len(self.counts):
            self.counts = [int(c) for c in counts]
            self.last_slot = int(round(float(data["start"]) / self.bucket_seconds))


@dataclass
class LimitState:
    """All active counters for one target (a provider or a key)."""

    counters: dict[str, _SlidingWindowCounter] = field(default_factory=dict)
    in_flight: int = 0
    concurrency_limit: int | None = None
    cfg: LimitsConfig | None = None

    @classmethod
    def from_config(cls, cfg: LimitsConfig) -> "LimitState":
        state = cls(concurrency_limit=cfg.concurrent_requests, cfg=cfg)
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


def effective_key_limits(provider: ProviderConfig, key: KeyConfig) -> LimitsConfig:
    """Provider `limits` (per key) with the key's own non-None fields overriding."""
    if key.limits is None:
        return provider.limits
    return LimitsConfig(**{**provider.limits.model_dump(), **key.limits.model_dump(exclude_none=True)})


def _content_chars(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(part.get("text") or "") if isinstance(part, dict) else len(str(part)) for part in content)
    return 0


def estimate_tokens(payload: dict) -> int:
    """ceil(chars / 4) of message contents (or embeddings input) + requested output tokens."""
    chars = sum(_content_chars(m.get("content")) for m in payload.get("messages") or [] if isinstance(m, dict))
    chars += _content_chars(payload.get("input"))
    max_out = payload.get("max_tokens") or payload.get("max_completion_tokens") or 0
    return math.ceil(chars / 4) + int(max_out)


def _sync(state: LimitState | None, cfg: LimitsConfig) -> LimitState:
    """Return a state for cfg, keeping counters whose window still exists after a config edit."""
    if state is not None and state.cfg == cfg:
        return state
    fresh = LimitState.from_config(cfg)
    if state is not None:
        fresh.in_flight = state.in_flight
        for name, counter in fresh.counters.items():
            old = state.counters.get(name)
            if old is not None:
                old.limit = counter.limit
                fresh.counters[name] = old
    return fresh


@dataclass
class LimitsRegistry:
    _provider_limits: dict[str, LimitState] = field(default_factory=dict)
    _key_limits: dict[tuple[str, str], LimitState] = field(default_factory=dict)

    def provider(self, provider_id: str, cfg: LimitsConfig | None) -> LimitState:
        """Combined limit for all keys of a provider (its `total_limits`)."""
        state = _sync(self._provider_limits.get(provider_id), cfg or LimitsConfig())
        self._provider_limits[provider_id] = state
        return state

    def key(self, provider_id: str, key_name: str, cfg: LimitsConfig) -> LimitState:
        state = _sync(self._key_limits.get((provider_id, key_name)), cfg)
        self._key_limits[(provider_id, key_name)] = state
        return state

    def dump(self) -> list[tuple[str, str, str, dict]]:
        rows = [("provider", pid, "", {n: c.dump() for n, c in s.counters.items()}) for pid, s in self._provider_limits.items()]
        rows += [("key", pid, name, {n: c.dump() for n, c in s.counters.items()}) for (pid, name), s in self._key_limits.items()]
        return [row for row in rows if row[3]]

    def load(self, rows: list[tuple[str, str, str, dict]], config: OctoFluxConfig) -> None:
        """Restore counters saved by dump(); rows for providers/keys no longer configured are dropped."""
        for scope, pid, key_name, payload in rows:
            provider = config.providers.get(pid)
            if provider is None:
                continue
            if scope == "provider":
                state = self.provider(pid, provider.total_limits)
            else:
                key = next((k for k in provider.keys if k.name == key_name), None)
                if key is None:
                    continue
                state = self.key(pid, key_name, effective_key_limits(provider, key))
            for name, data in payload.items():
                if name in state.counters:
                    state.counters[name].load(data)

    def snapshot(self) -> dict:
        return {
            "providers": {k: v.snapshot() for k, v in self._provider_limits.items()},
            "keys": {f"{k[0]}:{k[1]}": v.snapshot() for k, v in self._key_limits.items()},
        }
