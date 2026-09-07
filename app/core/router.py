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
from app.models.provider import KeyConfig, OctoFluxConfig, ProviderConfig
from app.providers.base import DiscoveredModel
from app.providers.registry import ProviderRegistry


@dataclass
class Candidate:
    provider_id: str
    model_id: str
    key: KeyConfig
    provider_priority: int
    model_priority: int
    reason: str  # human-readable, for debug logs — never includes the key value
    dynamic: bool = False


@dataclass
class RouteResolution:
    candidates: list[Candidate]
    excluded: list[str] = field(default_factory=list)  # human-readable exclusion reasons
    model_found: bool = True
    capability_found: bool = True


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
    def __init__(
        self, config: OctoFluxConfig, health: HealthRegistry, limits: LimitsRegistry,
        usage: UsageRegistry, providers: ProviderRegistry,
    ) -> None:
        self.config = config
        self.health = health
        self.limits = limits
        self.usage = usage
        self.providers = providers
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

    def _provider_model_pairs(
        self, requested_model: str, capability: str
    ) -> tuple[list[tuple[str, str]], bool, bool]:
        """Returns pairs plus whether the model and capability were found."""
        enabled = self.config.enabled_providers()

        if requested_model == "auto":
            pairs = [
                (pid, m.id)
                for pid, p in sorted(enabled.items(), key=lambda kv: kv[1].priority)
                for m in sorted(p.models, key=lambda m: m.priority)
                if m.enabled and capability in m.capabilities
            ]
            return pairs, True, bool(pairs)

        if requested_model in self.config.aliases:
            pairs = []
            alias_has_model = False
            for target in self.config.aliases[requested_model]:
                p = enabled.get(target.provider)
                if p is None:
                    continue
                model = next((m for m in p.models if m.id == target.model and m.enabled), None)
                if model is not None:
                    alias_has_model = True
                if model is not None and capability in model.capabilities:
                    pairs.append((p.id, model.id))
            return pairs, alias_has_model, bool(pairs)

        # Exact model id — search across all enabled providers (this is
        # provider *failover* for an identical model, not model substitution).
        pairs = [
            (pid, m.id)
            for pid, p in sorted(enabled.items(), key=lambda kv: kv[1].priority)
            for m in p.models
            if m.enabled and m.id == requested_model and capability in m.capabilities
        ]
        model_exists = any(
            m.enabled and m.id == requested_model
            for p in enabled.values()
            for m in p.models
        )
        return pairs, model_exists, bool(pairs)

    def resolve(self, requested_model: str, capability: str = "chat") -> RouteResolution:
        pairs, model_found, capability_found = self._provider_model_pairs(requested_model, capability)
        candidates: list[Candidate] = []
        excluded: list[str] = []

        if not model_found:
            excluded.append(f"model '{requested_model}' not found in any enabled provider or alias")
            return RouteResolution(candidates=[], excluded=excluded, model_found=False, capability_found=False)

        if not capability_found:
            excluded.append(f"model '{requested_model}' has no enabled '{capability}' capability")
            return RouteResolution(candidates=[], excluded=excluded, model_found=True, capability_found=False)

        for provider_id, model_id in pairs:
            provider = self.config.providers[provider_id]

            model_health = self.health.model(provider_id, model_id)
            if model_health.is_in_cooldown():
                excluded.append(
                    f"model '{provider_id}:{model_id}' in cooldown for "
                    f"{model_health.seconds_until_available():.0f}s"
                )
                continue

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

    async def resolve_dynamic(
        self, requested_model: str, *, payload: dict | None = None, capability: str = "chat"
    ) -> RouteResolution:
        resolution = self.resolve(requested_model, capability)
        if requested_model != "auto":
            return resolution

        payload = payload or {}
        for provider_id, provider in sorted(self.config.enabled_providers().items(), key=lambda item: item[1].priority):
            settings = provider.dynamic_models
            if not settings.enabled:
                continue
            adapter = self.providers.adapter(provider_id)
            keys = [key for key in provider.keys if key.enabled]
            if not keys:
                continue
            discovered = await adapter.discover_models(api_key=keys[0].value)
            accepted = [model for model in discovered if _model_suits_request(model, payload)]
            accepted.sort(
                key=lambda model: (
                    not self.health.model(provider_id, model.id).is_promoted(),
                    discovered.index(model),
                )
            )
            accepted = accepted[: settings.max_candidates]
            for rank, model in enumerate(accepted):
                model_health = self.health.model(provider_id, model.id)
                if model_health.is_in_cooldown():
                    resolution.excluded.append(
                        f"dynamic model '{provider_id}:{model.id}' in cooldown for "
                        f"{model_health.seconds_until_available():.0f}s"
                    )
                    continue
                for key in self._order_keys(provider, keys):
                    resolution.candidates.append(
                        Candidate(
                            provider_id=provider_id,
                            model_id=model.id,
                            key=key,
                            provider_priority=provider.priority,
                            model_priority=100000 + rank,
                            reason="dynamically discovered OpenAI-compatible model",
                            dynamic=True,
                        )
                    )
        return resolution


def _model_suits_request(model: DiscoveredModel, payload: dict) -> bool:
    if model.prompt_price is not None and model.completion_price is not None:
        if model.prompt_price != 0 or model.completion_price != 0:
            return False
    if model.output_modalities and "text" not in model.output_modalities:
        return False
    requested_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if requested_tokens is not None and model.context_length is not None:
        messages = payload.get("messages") or []
        if model.context_length < int(requested_tokens) + len(messages) * 4:
            return False
    if payload.get("tools") and model.supported_parameters:
        if "tools" not in model.supported_parameters and "tool_choice" not in model.supported_parameters:
            return False
    return True
