"""Routing engine.

Deterministic pipeline (see ARCHITECTURE.md "Routing strategy"):
resolve model -> filter disabled -> filter cooldown/unhealthy -> filter
exhausted limits -> sort by priority -> apply key-selection policy.

This module only *builds the ordered candidate list* and explains why each
candidate is present or excluded. It does not perform any I/O — that's
app.core.retry's job.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from itertools import count

from app.core.health import HealthRegistry
from app.core.limits import LimitsRegistry, effective_key_limits, estimate_tokens
from app.core.model_stats import ModelStats, score
from app.core.usage import UsageRegistry
from app.models.provider import DynamicModelsConfig, KeyConfig, LimitsConfig, ModelConfig, OctoFluxConfig, ProviderConfig
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
        self._last_evict = time.time()

    def is_stale(self, provider: ProviderConfig, model: ModelConfig) -> bool:
        """A configured ':free' chat model missing from the latest successful discovery."""
        if not provider.dynamic_models.enabled or not model.id.endswith(":free") or "chat" not in model.capabilities:
            return False
        known = getattr(self.providers.adapter(provider.id), "known_model_ids", lambda: None)()
        return known is not None and model.id not in known

    def rank_free_models(
        self, provider: ProviderConfig, discovered: list[DiscoveredModel], payload: dict
    ) -> tuple[list[tuple[DiscoveredModel, int, float]], dict[str, str]]:
        """Accepted (model, position, score) best first (pins, then score, then upstream order) and rejection reasons."""
        now = time.time()
        settings = provider.dynamic_models
        accepted: list[tuple[DiscoveredModel, int, float]] = []
        seen: set[str] = set()
        rejected: dict[str, str] = {}
        for position, model in enumerate(discovered):
            if model.id in seen:
                continue
            seen.add(model.id)
            reason = _model_suits_request(model, payload, settings)
            if reason is not None:
                rejected[model.id] = reason
                continue
            stats = self.health.model_stats.peek(provider.id, model.id) or ModelStats()
            accepted.append((model, position, score(position, stats, now)))
        pins = {mid: i for i, mid in enumerate(settings.pinned)}
        accepted.sort(key=lambda t: (pins.get(t[0].id, len(pins)), t[2], t[1]))
        return accepted, rejected

    def _evict_idle(self) -> None:
        now = time.time()
        if now - self._last_evict < 3600:
            return
        self._last_evict = now
        self.health.model_stats.evict_idle(now)
        configured = {(pid, m.id) for pid, p in self.config.providers.items() for m in p.models}
        self.health.drop_idle_models(configured)

    def _headroom(self, provider_id: str, key: KeyConfig, cfg: LimitsConfig) -> float:
        """Smallest remaining fraction across local windows and the upstream budget; 1.0 when unknown."""
        fractions = [
            1 - counter.current_total() / counter.limit
            for counter in self.limits.key(provider_id, key.name, cfg).counters.values()
            if counter.limit > 0
        ]
        health = self.health.key(provider_id, key.name)
        reset_passed = health.resets_at is not None and time.time() >= health.resets_at
        if health.remaining_requests is not None and health.max_remaining_requests and not reset_passed:
            fractions.append(health.remaining_requests / health.max_remaining_requests)
        return max(0.0, min(fractions, default=1.0))

    def _order_keys(self, provider: ProviderConfig, keys: list[KeyConfig]) -> list[KeyConfig]:
        policy = provider.key_selection or self.config.routing.default_key_selection
        if policy == "smart" and len(keys) > 1:
            start = self._rr.next_index(provider.id, len(keys))
            rotated = keys[start:] + keys[:start]
            # Stable sort keeps round-robin order between keys with equal headroom.
            return sorted(rotated, key=lambda k: -self._headroom(provider.id, k, effective_key_limits(provider, k)))
        if policy == "priority" or len(keys) <= 1:
            return keys
        if policy == "least_used":
            def used(k: KeyConfig) -> int:
                return self.usage._key_counters.get((provider.id, k.name), None).requests if (provider.id, k.name) in self.usage._key_counters else 0  # noqa: SLF001
            return sorted(keys, key=used)
        # round_robin (default)
        start = self._rr.next_index(provider.id, len(keys))
        return keys[start:] + keys[:start]

    def _key_block_reason(self, provider: ProviderConfig, key: KeyConfig, estimate: int) -> str | None:
        """Why this key can't take the request right now (cooldown or local limits), or None."""
        key_health = self.health.key(provider.id, key.name)
        if key_health.is_in_cooldown():
            return f"key '{provider.id}:{key.name}' in cooldown for {key_health.seconds_until_available():.0f}s"
        state = self.limits.key(provider.id, key.name, effective_key_limits(provider, key))
        if not state.has_request_budget():
            return f"key '{provider.id}:{key.name}' exhausted its local request rate limit"
        if not state.has_token_budget(estimate):
            return f"key '{provider.id}:{key.name}' exhausted its local token limit"
        return None

    def _provider_model_pairs(
        self, requested_model: str, capability: str
    ) -> tuple[list[tuple[str, str]], bool, bool]:
        """Returns pairs plus whether the model and capability were found."""
        enabled = self.config.enabled_providers()

        if requested_model == "auto":
            pairs = []
            for pid, provider in sorted(enabled.items(), key=lambda kv: kv[1].priority):
                pairs.extend(
                    (pid, model.id)
                    for model in sorted(provider.models, key=lambda m: m.priority)
                    if model.enabled and capability in model.capabilities
                )
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

    def resolve(self, requested_model: str, capability: str = "chat", payload: dict | None = None) -> RouteResolution:
        estimate = estimate_tokens(payload or {})
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
            model_cfg = next((m for m in provider.models if m.id == model_id), None)
            if model_cfg is not None and self.is_stale(provider, model_cfg):
                excluded.append(f"model '{provider_id}:{model_id}' no longer listed as free upstream")
                continue

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

            provider_limit_state = self.limits.provider(provider_id, provider.total_limits)
            if not provider_limit_state.has_request_budget():
                excluded.append(f"provider '{provider_id}' exhausted its local request rate limit")
                continue
            if not provider_limit_state.has_token_budget(estimate):
                excluded.append(f"provider '{provider_id}' exhausted its local token limit")
                continue

            enabled_keys = [k for k in provider.keys if k.enabled]
            ordered_keys = self._order_keys(provider, enabled_keys)

            for key in ordered_keys:
                block = self._key_block_reason(provider, key, estimate)
                if block is not None:
                    excluded.append(block)
                    continue

                model_cfg = next((m for m in provider.models if m.id == model_id), None)
                model_priority = model_cfg.priority if model_cfg is not None else 0
                candidates.append(
                    Candidate(
                        provider_id=provider_id,
                        model_id=model_id,
                        key=key,
                        provider_priority=provider.priority,
                        model_priority=model_priority,
                        reason=(
                            f"provider healthy, model enabled, key '{key.name}' healthy, "
                            f"within local limits, priority=({provider.priority},{model_priority})"
                        ),
                    )
                )

        candidates.sort(key=lambda c: (c.provider_priority, c.model_priority))
        return RouteResolution(candidates=candidates, excluded=excluded, model_found=True)

    async def resolve_dynamic(
        self, requested_model: str, *, payload: dict | None = None, capability: str = "chat"
    ) -> RouteResolution:
        if requested_model != "auto":
            return self.resolve(requested_model, capability, payload)

        payload = payload or {}
        estimate = estimate_tokens(payload)
        providers = sorted(self.config.enabled_providers().items(), key=lambda item: item[1].priority)
        # Discover first so the stale check in resolve() sees the fresh list.
        discovered: dict[str, list[DiscoveredModel]] = {}
        for provider_id, provider in providers:
            keys = [key for key in provider.keys if key.enabled]
            if keys and provider.dynamic_models.enabled:
                adapter = self.providers.adapter(provider_id)
                if hasattr(adapter, "discover_models_cached"):
                    discovered[provider_id] = await adapter.discover_models_cached(api_key=keys[0].value, wait=DISCOVERY_WAIT_SECONDS)
                else:
                    discovered[provider_id] = await adapter.discover_models(api_key=keys[0].value)
        self._evict_idle()
        resolution = self.resolve(requested_model, capability, payload)

        for provider_id, provider in providers:
            keys = [key for key in provider.keys if key.enabled]
            if not keys:
                continue
            extra: list[tuple[str, int, str]] = []  # (model id, model priority, reason)
            if provider_id in discovered:
                existing = {c.model_id for c in resolution.candidates if c.provider_id == provider_id}
                ranked, _ = self.rank_free_models(provider, discovered[provider_id], payload)
                ranked = [m for m, _, _ in ranked if m.id not in existing][: provider.dynamic_models.max_candidates]
                extra += [(m.id, 100000 + rank, "dynamically discovered OpenAI-compatible model") for rank, m in enumerate(ranked)]
            if provider_id == "openrouter" and capability == "chat":
                extra.append(("openrouter/free", 200000, "openrouter free router fallback"))

            for model_id, model_priority, reason in extra:
                model_health = self.health.model(provider_id, model_id)
                if model_health.is_in_cooldown():
                    resolution.excluded.append(
                        f"dynamic model '{provider_id}:{model_id}' in cooldown for "
                        f"{model_health.seconds_until_available():.0f}s"
                    )
                    continue
                for key in self._order_keys(provider, keys):
                    block = self._key_block_reason(provider, key, estimate)
                    if block is not None:
                        resolution.excluded.append(block)
                        continue
                    resolution.candidates.append(
                        Candidate(
                            provider_id=provider_id,
                            model_id=model_id,
                            key=key,
                            provider_priority=provider.priority,
                            model_priority=model_priority,
                            reason=reason,
                            dynamic=True,
                        )
                    )
        # Group discovered/fallback candidates with their provider: static, discovered, free router.
        resolution.candidates.sort(key=lambda c: (c.provider_priority, c.model_priority))
        return resolution


# Longest a request waits for a cold discovery before routing without it.
DISCOVERY_WAIT_SECONDS = 5.0

_NON_CHAT_RE = re.compile(r"safety|guard|embed|moderation|rerank", re.I)


def _model_suits_request(model: DiscoveredModel, payload: dict, settings: DynamicModelsConfig) -> str | None:
    """Why a discovered model can't serve this request, or None when it can."""
    if model.id in settings.blocked:
        return "blocked"
    if model.deprecated:
        return "deprecated"
    if model.prompt_price is not None and model.completion_price is not None:
        if model.prompt_price != 0 or model.completion_price != 0:
            return "not free"
    if model.output_modalities and "text" not in model.output_modalities:
        return "no text output"
    if _NON_CHAT_RE.search(model.id):
        return "not a chat model"
    if model.context_length is not None and model.context_length < settings.min_context:
        return f"context {model.context_length} < {settings.min_context}"
    requested_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if requested_tokens is not None and model.context_length is not None:
        messages = payload.get("messages") or []
        if model.context_length < int(requested_tokens) + len(messages) * 4:
            return "context too small for this request"
    if payload.get("tools") and model.supported_parameters:
        if "tools" not in model.supported_parameters and "tool_choice" not in model.supported_parameters:
            return "no tool support"
    return None
