from __future__ import annotations

from dataclasses import dataclass

from app.core.health import HealthRegistry
from app.core.limits import LimitsRegistry
from app.core.retry import Scheduler
from app.core.usage import UsageRegistry
from app.models.provider import OctoProxyConfig
from app.providers.registry import ProviderRegistry


@dataclass
class AppState:
    config: OctoProxyConfig
    providers: ProviderRegistry
    health: HealthRegistry
    limits: LimitsRegistry
    usage: UsageRegistry
    scheduler: Scheduler

    @classmethod
    def build(cls, config: OctoProxyConfig) -> "AppState":
        providers = ProviderRegistry(config)
        health = HealthRegistry()
        limits = LimitsRegistry()
        usage = UsageRegistry()
        scheduler = Scheduler(config, providers, health, limits, usage)
        return cls(config=config, providers=providers, health=health, limits=limits, usage=usage, scheduler=scheduler)

    def replace_config(self, new_config: OctoProxyConfig) -> "AppState":
        """Used by /admin/reload: rebuild provider adapters/config but keep
        the existing health/limits/usage registries so live state (cooldowns,
        counters) survives for targets that still exist."""
        providers = ProviderRegistry(new_config)
        scheduler = Scheduler(new_config, providers, self.health, self.limits, self.usage)
        return AppState(
            config=new_config, providers=providers, health=self.health,
            limits=self.limits, usage=self.usage, scheduler=scheduler,
        )
