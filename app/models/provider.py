"""Static configuration models.

These describe the *declared shape* of the system as loaded from YAML. They
are immutable once loaded (Pydantic models, not mutated after startup).
Runtime state (health, counters, cooldowns) lives separately in
`app.core.health` / `app.core.limits` / `app.core.usage` and is never stored
on these objects.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LimitsConfig(BaseModel):
    """Local rate limits OctoProxy enforces itself (not upstream's limits)."""

    requests_per_second: float | None = Field(default=None, gt=0)
    requests_per_minute: int | None = Field(default=None, gt=0)
    requests_per_hour: int | None = Field(default=None, gt=0)
    requests_per_day: int | None = Field(default=None, gt=0)

    tokens_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_hour: int | None = Field(default=None, gt=0)
    tokens_per_day: int | None = Field(default=None, gt=0)

    concurrent_requests: int | None = Field(default=None, gt=0)


class RetryConfig(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_ms: int = Field(default=50, ge=0)
    max_delay_ms: int = Field(default=2000, ge=0)
    jitter_ms: int = Field(default=50, ge=0)


class HealthConfig(BaseModel):
    failure_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: int = Field(default=30, ge=1)
    # Long cooldown applied for quota_exceeded / daily-limit style errors.
    long_cooldown_seconds: int = Field(default=300, ge=1)


class KeyConfig(BaseModel):
    name: str
    value: str
    limits: LimitsConfig | None = None
    enabled: bool = True

    @field_validator("value")
    @classmethod
    def value_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("key value resolved to an empty string (check env var)")
        return v


class ModelConfig(BaseModel):
    id: str
    enabled: bool = True
    priority: int = 100
    # Optional: aliases this model participates in are declared globally
    # (see AliasConfig) rather than here, to keep a single source of truth.


class AuthenticationConfig(BaseModel):
    type: Literal["bearer", "header", "none"] = "bearer"
    header: str = "Authorization"
    prefix: str = "Bearer "


class ProviderConfig(BaseModel):
    id: str
    enabled: bool = True
    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    priority: int = 100

    keys: list[KeyConfig] = Field(default_factory=list)
    models: list[ModelConfig] = Field(default_factory=list)

    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)

    authentication: AuthenticationConfig = Field(default_factory=AuthenticationConfig)
    headers: dict[str, str] = Field(default_factory=dict)

    key_selection: Literal["round_robin", "least_used", "priority"] = "round_robin"

    request_timeout_seconds: float = Field(default=60.0, gt=0)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)

    @field_validator("base_url")
    @classmethod
    def base_url_must_look_like_a_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"base_url must start with http:// or https:// (got {v!r})")
        return v.rstrip("/")

    @model_validator(mode="after")
    def must_have_at_least_one_enabled_key_and_model(self) -> "ProviderConfig":
        if self.enabled and not any(k.enabled for k in self.keys):
            raise ValueError(f"provider '{self.id}': no enabled keys configured")
        if self.enabled and not any(m.enabled for m in self.models):
            raise ValueError(f"provider '{self.id}': no enabled models configured")
        ids = [k.name for k in self.keys]
        if len(ids) != len(set(ids)):
            raise ValueError(f"provider '{self.id}': duplicate key names")
        mids = [m.id for m in self.models]
        if len(mids) != len(set(mids)):
            raise ValueError(f"provider '{self.id}': duplicate model ids")
        return self


class AliasTarget(BaseModel):
    provider: str
    model: str


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    client_keys: list[str] = Field(default_factory=list)
    require_auth: bool = True


class RoutingConfig(BaseModel):
    default_key_selection: Literal["round_robin", "least_used", "priority"] = "round_robin"
    max_total_attempts: int = Field(default=4, ge=1, le=20)
    allow_model_fallback: bool = True
    allow_provider_fallback: bool = True


class OctoProxyConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    aliases: dict[str, list[AliasTarget]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def provider_keys_match_ids_and_are_unique(self) -> "OctoProxyConfig":
        for key, provider in self.providers.items():
            if key != provider.id:
                raise ValueError(
                    f"providers mapping key '{key}' does not match provider.id '{provider.id}'"
                )
        for alias, targets in self.aliases.items():
            for t in targets:
                if t.provider not in self.providers:
                    raise ValueError(
                        f"alias '{alias}' references unknown provider '{t.provider}'"
                    )
        return self

    def enabled_providers(self) -> dict[str, ProviderConfig]:
        return {k: v for k, v in self.providers.items() if v.enabled}
