from __future__ import annotations

import asyncio
from contextlib import suppress

from app.core.health import HealthRegistry
from app.models.provider import OctoFluxConfig
from app.observability.logging import log_event
from app.providers.base import UpstreamFailure, UpstreamSuccess
from app.providers.registry import ProviderRegistry


def mask_api_key(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


class ProviderHealthMonitor:
    def __init__(
        self,
        config: OctoFluxConfig,
        providers: ProviderRegistry,
        health: HealthRegistry,
    ) -> None:
        self.config = config
        self.providers = providers
        self.health = health
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="provider-health-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def check_once(self) -> None:
        checks = [
            self.check_key(provider_id, key.name, key.value)
            for provider_id, provider in self.config.enabled_providers().items()
            for key in provider.keys
            if key.enabled
        ]
        await asyncio.gather(*checks)

    async def check_key(self, provider_id: str, key_name: str, api_key: str) -> UpstreamSuccess | UpstreamFailure:
        result = await self.providers.health_check(provider_id, api_key)
        if isinstance(result, UpstreamSuccess):
            ok = True
            reason = None
        else:
            ok = False
            reason = _failure_reason(result)

        self.health.record_check(
            provider_id,
            key_name,
            ok=ok,
            reason=reason,
            latency_ms=result.latency_ms,
        )
        log_event(
            "provider_health_check",
            provider=provider_id,
            key_name=key_name,
            key_hint=mask_api_key(api_key),
            status="working" if ok else "unhealthy",
            status_code=result.status_code,
            reason=reason,
            latency_ms=round(result.latency_ms, 1),
        )
        return result

    async def _run(self) -> None:
        interval = min(
            (provider.health.check_interval_seconds for provider in self.config.enabled_providers().values()),
            default=43200,
        )
        while True:
            await self.check_once()
            await asyncio.sleep(interval)


def _failure_reason(result: UpstreamFailure) -> str:
    if result.status_code is not None:
        return f"upstream returned HTTP {result.status_code}"
    return result.body_text[:200] or "connection failed"
