from __future__ import annotations

from app.models.provider import OctoFluxConfig
from app.providers.base import UpstreamFailure, UpstreamSuccess
from app.providers.openai_compatible import OpenAICompatibleProvider


class ProviderRegistry:
    """Holds one adapter instance per configured provider (built once at
    startup so each keeps its own pooled httpx.AsyncClient)."""

    def __init__(self, config: OctoFluxConfig) -> None:
        self.config = config
        self._adapters: dict[str, OpenAICompatibleProvider] = {
            pid: OpenAICompatibleProvider(pcfg) for pid, pcfg in config.providers.items()
        }

    def adapter(self, provider_id: str) -> OpenAICompatibleProvider:
        return self._adapters[provider_id]

    async def health_check(self, provider_id: str, api_key: str) -> UpstreamSuccess | UpstreamFailure:
        return await self._adapters[provider_id].health_check(api_key=api_key)

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()
