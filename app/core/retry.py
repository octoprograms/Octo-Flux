"""Retry / failover engine.

Walks the ordered candidates produced by app.core.router, sends the upstream
request, classifies the result, and decides whether to retry (same target
after backoff, another key, another provider) or return to the client — see
ARCHITECTURE.md "Retry strategy". Bounded by routing.max_total_attempts
(a per-request budget independent of any single provider's retry.max_attempts)
and never retries the exact same (provider, model, key) twice for one
inbound request.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

from app.core.errors import (
    ErrorCategory,
    classify_exception,
    classify_http_status,
    decision_for,
    parse_retry_after,
)
from app.core.health import HealthRegistry
from app.core.limits import LimitsRegistry
from app.core.router import Candidate, Router
from app.core.usage import UsageRegistry
from app.models.provider import OctoFluxConfig
from app.observability.logging import log_event
from app.providers.base import UpstreamFailure, UpstreamSuccess
from app.providers.registry import ProviderRegistry


class ClientFacingError(Exception):
    """Carries an OpenAI-shaped error straight back to the caller — no more
    retries are appropriate for this outcome."""

    def __init__(self, status_code: int, message: str, error_type: str, tried: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.tried = tried or []


@dataclass
class AttemptTrace:
    provider_id: str
    model_id: str
    key_name: str
    outcome: str  # "success" | error category value
    latency_ms: float


@dataclass
class ChatCompletionResult:
    json_body: dict[str, Any]
    provider_id: str
    model_id: str
    key_name: str
    trace: list[AttemptTrace]


def _backoff_seconds(attempt: int, base_ms: int, max_ms: int, jitter_ms: int) -> float:
    delay_ms = min(max_ms, base_ms * (2**attempt))
    delay_ms += random.uniform(0, jitter_ms)
    return delay_ms / 1000.0


class Scheduler:
    def __init__(
        self,
        config: OctoFluxConfig,
        providers: ProviderRegistry,
        health: HealthRegistry,
        limits: LimitsRegistry,
        usage: UsageRegistry,
    ) -> None:
        self.config = config
        self.providers = providers
        self.health = health
        self.limits = limits
        self.usage = usage
        self.router = Router(config, health, limits, usage)

    def apply_failure(self, candidate: Candidate, category: ErrorCategory, retry_after: float | None) -> None:
        decision = decision_for(category)
        provider_cfg = self.config.providers[candidate.provider_id]

        cooldown_seconds = retry_after
        if cooldown_seconds is None:
            cooldown_seconds = (
                provider_cfg.health.long_cooldown_seconds
                if decision.long_cooldown
                else provider_cfg.health.cooldown_seconds
            )

        if decision.mark_unhealthy == "key":
            key_health = self.health.key(candidate.provider_id, candidate.key.name)
            key_health.record_failure(category.value, cooldown_seconds, provider_cfg.health.failure_threshold)
        elif decision.mark_unhealthy == "provider":
            provider_health = self.health.provider(candidate.provider_id)
            provider_health.record_failure(category.value, cooldown_seconds, provider_cfg.health.failure_threshold)
        elif decision.cooldown:
            # e.g. rate_limited on a key without crossing failure_threshold —
            # still worth cooling the key down immediately.
            key_health = self.health.key(candidate.provider_id, candidate.key.name)
            key_health.record_cooldown(cooldown_seconds)

        if category in (ErrorCategory.RATE_LIMITED, ErrorCategory.QUOTA_EXCEEDED):
            self.usage.record_rate_limited(candidate.provider_id)
        if category == ErrorCategory.TIMEOUT:
            self.usage.record_timeout(candidate.provider_id)

    async def execute_chat_completion(
        self, *, requested_model: str, payload: dict[str, Any], request_id: str
    ) -> ChatCompletionResult:
        resolution = self.router.resolve(requested_model)
        if not resolution.model_found:
            raise ClientFacingError(
                404, f"The model '{requested_model}' does not exist or is not configured.", "model_not_found"
            )
        if not resolution.candidates:
            log_event(
                "no_candidates_available", level="warning", request_id=request_id,
                model=requested_model, excluded=resolution.excluded,
            )
            raise ClientFacingError(
                503,
                "No healthy provider/key available for this model right now.",
                "service_unavailable",
                tried=resolution.excluded,
            )

        max_attempts = self.config.routing.max_total_attempts
        attempted: set[tuple[str, str, str]] = set()
        trace: list[AttemptTrace] = []
        attempt_number = 0
        last_error: ClientFacingError | None = None

        for candidate in resolution.candidates:
            target_key = (candidate.provider_id, candidate.model_id, candidate.key.name)
            if target_key in attempted:
                continue
            if attempt_number >= max_attempts:
                break
            attempted.add(target_key)
            attempt_number += 1

            if attempt_number > 1:
                self.usage.record_fallback()

            log_event(
                "routing_decision", request_id=request_id, attempt=attempt_number,
                provider=candidate.provider_id, model=candidate.model_id,
                key=candidate.key.name, reason=candidate.reason,
            )

            provider_limit_state = self.limits.provider(candidate.provider_id, self.config.providers[candidate.provider_id].limits)
            key_limit_state = self.limits.key(candidate.provider_id, candidate.key.name, candidate.key.limits)
            provider_limit_state.record_request_start()
            if key_limit_state is not None:
                key_limit_state.record_request_start()

            adapter = self.providers.adapter(candidate.provider_id)
            result = await adapter.chat_completion(
                model=candidate.model_id, payload=payload, api_key=candidate.key.value, request_id=request_id
            )

            provider_limit_state.record_request_end()
            if key_limit_state is not None:
                key_limit_state.record_request_end()

            if isinstance(result, UpstreamSuccess):
                self.health.provider(candidate.provider_id).record_success()
                self.health.key(candidate.provider_id, candidate.key.name).record_success()
                self.usage.record_attempt(candidate.provider_id, candidate.model_id, candidate.key.name, True, result.latency_ms)
                self.usage.record_tokens(
                    candidate.provider_id, candidate.model_id, candidate.key.name,
                    result.input_tokens, result.output_tokens,
                )
                trace.append(AttemptTrace(candidate.provider_id, candidate.model_id, candidate.key.name, "success", result.latency_ms))
                log_event(
                    "upstream_success", request_id=request_id, provider=candidate.provider_id,
                    model=candidate.model_id, key=candidate.key.name, latency_ms=round(result.latency_ms, 1),
                    attempt=attempt_number,
                )
                return ChatCompletionResult(
                    json_body=result.json_body,
                    provider_id=candidate.provider_id,
                    model_id=candidate.model_id,
                    key_name=candidate.key.name,
                    trace=trace,
                )

            # Failure path
            assert isinstance(result, UpstreamFailure)
            self.usage.record_attempt(candidate.provider_id, candidate.model_id, candidate.key.name, False, result.latency_ms)

            if result.status_code is None:
                category = classify_exception(result.exception) if result.exception else ErrorCategory.UNKNOWN
                retry_after = None
            else:
                category = classify_http_status(result.status_code, result.body_text)
                retry_after = parse_retry_after(result.headers) if result.headers is not None else None

            trace.append(AttemptTrace(candidate.provider_id, candidate.model_id, candidate.key.name, category.value, result.latency_ms))
            self.apply_failure(candidate, category, retry_after)

            decision = decision_for(category)
            log_event(
                "upstream_failure", level="warning", request_id=request_id, provider=candidate.provider_id,
                model=candidate.model_id, key=candidate.key.name, category=category.value,
                status_code=result.status_code, attempt=attempt_number, retryable=decision.retryable,
            )

            last_error = ClientFacingError(
                result.status_code or 502,
                _safe_error_message(category, result.body_text),
                category.value,
            )

            if not decision.retryable:
                raise last_error

            if category in (ErrorCategory.SERVER_ERROR, ErrorCategory.TIMEOUT, ErrorCategory.CONNECTION_ERROR, ErrorCategory.SERVICE_UNAVAILABLE, ErrorCategory.OVERLOADED):
                provider_cfg = self.config.providers[candidate.provider_id]
                backoff = _backoff_seconds(
                    attempt_number - 1, provider_cfg.retry.base_delay_ms, provider_cfg.retry.max_delay_ms, provider_cfg.retry.jitter_ms
                )
                await asyncio.sleep(backoff)

        # Exhausted candidates or attempt budget.
        if last_error is not None:
            last_error.tried = [f"{t.provider_id}/{t.model_id}:{t.key_name}={t.outcome}" for t in trace]
            raise last_error
        raise ClientFacingError(503, "No candidates could be attempted (limits/cooldowns).", "service_unavailable")


def _safe_error_message(category: ErrorCategory, upstream_body: str) -> str:
    # Return a stable, provider-agnostic message; do not echo raw upstream
    # bodies verbatim (they may contain provider-identifying detail we don't
    # want to leak, and are frequently not useful to the caller anyway).
    messages = {
        ErrorCategory.AUTHENTICATION_ERROR: "Upstream authentication failed.",
        ErrorCategory.AUTHORIZATION_ERROR: "Upstream authorization failed.",
        ErrorCategory.RATE_LIMITED: "Upstream rate limit reached.",
        ErrorCategory.QUOTA_EXCEEDED: "Upstream quota exceeded.",
        ErrorCategory.MODEL_NOT_FOUND: "Requested model not found upstream.",
        ErrorCategory.INVALID_REQUEST: "The request was rejected as invalid.",
        ErrorCategory.CONTEXT_LENGTH_EXCEEDED: "The request exceeds the model's context length.",
        ErrorCategory.SERVER_ERROR: "Upstream server error.",
        ErrorCategory.TIMEOUT: "Upstream request timed out.",
        ErrorCategory.CONNECTION_ERROR: "Could not connect to upstream provider.",
        ErrorCategory.SERVICE_UNAVAILABLE: "Upstream service unavailable.",
        ErrorCategory.OVERLOADED: "Upstream provider is overloaded.",
        ErrorCategory.UNKNOWN: "Upstream request failed.",
    }
    return messages.get(category, "Upstream request failed.")
