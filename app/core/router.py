"""Routing engine.

Deterministic pipeline (see ARCHITECTURE.md "Routing strategy"):
resolve model -> filter disabled -> filter cooldown/unhealthy -> filter
exhausted limits -> sort by priority -> apply key-selection policy.

This module only *builds the ordered candidate list* and explains why each
candidate is present or excluded. It does not perform any I/O — that's
app.core.retry's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from app.core.health import HealthRegistry
from app.core.limits import LimitsRegistry
from app.core.usage import UsageRegistry
from app.models.provider import KeyConfig, OctoProxyConfig, ProviderConfig


@dataclass
class Candidate:
    provider_id: str
    model_id: str
    key: KeyConfig
    provider_priority: int
    model_priority: int
    reason: str  # human-readable, for debug logs — never includes the key value


@dataclass
class RouteResolution:
    candidates: list[Candidate]
    excluded: list[str] = field(default_factory=list)  # human-readable exclusion reasons
    model_found: bool = True


class _RoundRobinCursor:
    """Tracks the next key index per provider for the round_robin policy."""

    def __init__(self) -> None:
        self._counters: dict[str, count] = {}

    def next_index(self, provider_id: str, n_keys: int) -> int:
        if n_keys == 0:
            return 0
        counter = self._counters.setdefault(provider_id, count())
        return next(counter) % n_keys


class Router:
    def __init__(self, config: OctoProxyConfig, health: HealthRegistry, limits: LimitsRegistry, usage: UsageRegistry) -> None:
        self.config = config
        self.health = health
        self.limits = limits
        self.usage = usage
        self._rr = _RoundRobinCursor()

    def _order_keys(self, provider: ProviderConfig, keys: list[KeyConfig]) -> list[KeyConfig]:
        policy = provider.key_selection
        if policy == "priority" or len(keys) <= 1:
            return keys
        if policy == "least_used":
            def used(k: KeyConfig) -> int:
                return self.usage._key_counters.get((provider.id, k.name), None).requests if (provider.id, k.name) in self.usage._key_counters else 0  # noqa: SLF001
            return sorted(keys, key=used)
        # round_robin (default)
        start = self._rr.next_index(provider.id, len(keys))
        return keys[start:] + keys[:start]

    def _provider_model_pairs(self, requested_model: str) -> tuple[list[tuple[str, str]], bool]:
        """Returns (list of (provider_id, model_id) in priority order, model_found)."""
        enabled = self.config.enabled_providers()

        if requested_model == "auto":
            pairs = [
                (pid, m.id)
                for pid, p in sorted(enabled.items(), key=lambda kv: kv[1].priority)
                for m in sorted(p.models, key=lambda m: m.priority)
                if m.enabled
            ]
            return pairs, True

        if requested_model in self.config.aliases:
            pairs = []
            for target in self.config.aliases[requested_model]:
                p = enabled.get(target.provider)
                if p is None:
                    continue
                model = next((m for m in p.models if m.id == target.model and m.enabled), None)
                if model is not None:
                    pairs.append((p.id, model.id))
            return pairs, len(pairs) > 0

        # Exact model id — search across all enabled providers (this is
        # provider *failover* for an identical model, not model substitution).
        pairs = [
            (pid, m.id)
            for pid, p in sorted(enabled.items(), key=lambda kv: kv[1].priority)
            for m in p.models
            if m.enabled and m.id == requested_model
        ]
        return pairs, len(pairs) > 0

    def resolve(self, requested_model: str) -> RouteResolution:
        pairs, model_found = self._provider_model_pairs(requested_model)
        candidates: list[Candidate] = []
        excluded: list[str] = []

        if not model_found:
            excluded.append(f"model '{requested_model}' not found in any enabled provider or alias")
            return RouteResolution(candidates=[], excluded=excluded, model_found=False)

        for provider_id, model_id in pairs:
            provider = self.config.providers[provider_id]

            provider_health = self.health.provider(provider_id)
            if provider_health.is_in_cooldown():
                excluded.append(
                    f"provider '{provider_id}' in cooldown for "
                    f"{provider_health.seconds_until_available():.0f}s"
                )
                continue

            provider_limit_state = self.limits.provider(provider_id, provider.limits)
            if not provider_limit_state.has_request_budget():
                excluded.append(f"provider '{provider_id}' exhausted its local request rate limit")
                continue

            enabled_keys = [k for k in provider.keys if k.enabled]
            ordered_keys = self._order_keys(provider, enabled_keys)

            for key in ordered_keys:
                key_health = self.health.key(provider_id, key.name)
                if key_health.is_in_cooldown():
                    excluded.append(
                        f"key '{provider_id}:{key.name}' in cooldown for "
                        f"{key_health.seconds_until_available():.0f}s"
                    )
                    continue

                key_limit_state = self.limits.key(provider_id, key.name, key.limits)
                if key_limit_state is not None and not key_limit_state.has_request_budget():
                    excluded.append(f"key '{provider_id}:{key.name}' exhausted its local request rate limit")
                    continue

                model_cfg = next(m for m in provider.models if m.id == model_id)
                candidates.append(
                    Candidate(
                        provider_id=provider_id,
                        model_id=model_id,
                        key=key,
                        provider_priority=provider.priority,
                        model_priority=model_cfg.priority,
                        reason=(
                            f"provider healthy, model enabled, key '{key.name}' healthy, "
                            f"within local limits, priority=({provider.priority},{model_cfg.priority})"
                        ),
                    )
                )

        candidates.sort(key=lambda c: (c.provider_priority, c.model_priority))
        return RouteResolution(candidates=candidates, excluded=excluded, model_found=True)
