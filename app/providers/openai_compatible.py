"""A single generic adapter for any OpenAI-compatible upstream.

This is the whole point of the config-first design: adding a new provider is
a YAML entry, not a new Python class. Provider-specific quirks that can't be
expressed in config (truly bespoke auth schemes, non-standard endpoints)
would get their own adapter implementing the same `ProviderAdapter`
interface — but for the OpenAI-compatible family (Groq, OpenRouter, NVIDIA
NIM, Together, Cerebras, ...) this one class is sufficient.
"""
from __future__ import annotations

import time
import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.models.provider import AuthenticationConfig, ProviderConfig
from app.providers.base import DiscoveredModel, UpstreamFailure, UpstreamSuccess


def _build_headers(auth: AuthenticationConfig, api_key: str, extra: dict[str, str]) -> dict[str, str]:
    headers = {"Content-Type": "application/json", **extra}
    if auth.type == "bearer":
        headers[auth.header] = f"{auth.prefix}{api_key}"
    elif auth.type == "header":
        headers[auth.header] = api_key
    # type == "none": no auth header attached
    return headers


class OpenAICompatibleProvider:
    """Owns one shared httpx.AsyncClient per provider (connection pooling /
    keep-alive), built once and reused for every request."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        timeout = httpx.Timeout(
            timeout=config.request_timeout_seconds,
            connect=config.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=config.base_url, limits=limits, timeout=timeout
        )
        self._discovery_lock = asyncio.Lock()
        self._discovered_models: list[DiscoveredModel] = []
        self._discovery_at = 0.0
        self._discovery_failure_at = 0.0
        self.discovered_at: float | None = None  # wall clock of the last successful discovery
        self._refresh: asyncio.Task | None = None

    async def discover_models_cached(self, *, api_key: str, wait: float) -> list[DiscoveredModel]:
        """Like discover_models, but never makes a request wait long: a stale list is served while a
        background refresh runs, and a cold start waits at most `wait` seconds."""
        settings = self.config.dynamic_models
        if self._discovered_models and time.monotonic() - self._discovery_at < settings.refresh_interval_seconds:
            return list(self._discovered_models)
        if self._refresh is None or self._refresh.done():
            self._refresh = asyncio.create_task(self.discover_models(api_key=api_key))
        if self._discovered_models:
            return list(self._discovered_models)
        try:
            return await asyncio.wait_for(asyncio.shield(self._refresh), wait)
        except asyncio.TimeoutError:
            return []

    def known_model_ids(self) -> set[str] | None:
        """Ids from the latest successful discovery, or None if there is none to trust."""
        if not self._discovery_at or not self._discovered_models:
            return None
        return {model.id for model in self._discovered_models}

    async def chat_completion(
        self, *, model: str, payload: dict[str, Any], api_key: str, request_id: str
    ) -> UpstreamSuccess | UpstreamFailure:
        body = {**payload, "model": model, "stream": False}
        headers = _build_headers(self.config.authentication, api_key, self.config.headers)
        headers["X-Request-Id"] = request_id
        start = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", json=body, headers=headers)
        except Exception as exc:  # connection-level failure, no response
            latency_ms = (time.perf_counter() - start) * 1000
            return UpstreamFailure(status_code=None, body_text=str(exc), latency_ms=latency_ms, exception=exc)

        latency_ms = (time.perf_counter() - start) * 1000
        if resp.status_code >= 400:
            return UpstreamFailure(
                status_code=resp.status_code,
                body_text=resp.text,
                latency_ms=latency_ms,
                headers=resp.headers,
            )

        data = resp.json()
        usage = data.get("usage") or {}
        return UpstreamSuccess(
            status_code=resp.status_code,
            json_body=data,
            latency_ms=latency_ms,
            headers=resp.headers,
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        )

    async def embeddings(
        self, *, model: str, payload: dict[str, Any], api_key: str, request_id: str
    ) -> UpstreamSuccess | UpstreamFailure:
        body = {**payload, "model": model}
        headers = _build_headers(self.config.authentication, api_key, self.config.headers)
        headers["X-Request-Id"] = request_id
        start = time.perf_counter()
        try:
            resp = await self._client.post("/embeddings", json=body, headers=headers)
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return UpstreamFailure(status_code=None, body_text=str(exc), latency_ms=latency_ms, exception=exc)

        latency_ms = (time.perf_counter() - start) * 1000
        if resp.status_code >= 400:
            return UpstreamFailure(
                status_code=resp.status_code,
                body_text=resp.text,
                latency_ms=latency_ms,
                headers=resp.headers,
            )

        data = resp.json()
        usage = data.get("usage") or {}
        return UpstreamSuccess(
            status_code=resp.status_code,
            json_body=data,
            latency_ms=latency_ms,
            headers=resp.headers,
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        )

    async def chat_completion_stream(
        self, *, model: str, payload: dict[str, Any], api_key: str, request_id: str
    ) -> AsyncIterator[bytes]:
        body = {**payload, "model": model, "stream": True}
        headers = _build_headers(self.config.authentication, api_key, self.config.headers)
        headers["X-Request-Id"] = request_id

        async with self._client.stream(
            "POST", "/chat/completions", json=body, headers=headers
        ) as resp:
            if resp.status_code >= 400:
                error_body = await resp.aread()
                raise UpstreamStreamError(resp.status_code, error_body.decode(errors="replace"), resp.headers)
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def list_models(self) -> list[str]:
        return [m.id for m in self.config.models if m.enabled]

    async def health_check(self, *, api_key: str) -> UpstreamSuccess | UpstreamFailure:
        start = time.perf_counter()
        headers = _build_headers(self.config.authentication, api_key, self.config.headers)
        try:
            resp = await self._client.get("/models", headers=headers)
        except Exception as exc:
            return UpstreamFailure(
                status_code=None,
                body_text=str(exc),
                latency_ms=(time.perf_counter() - start) * 1000,
                exception=exc,
            )

        latency_ms = (time.perf_counter() - start) * 1000
        if resp.status_code >= 400:
            return UpstreamFailure(
                status_code=resp.status_code,
                body_text=resp.text,
                latency_ms=latency_ms,
                headers=resp.headers,
            )
        data = resp.json()
        model_ids = [
            item.get("id") or item.get("name")
            for item in data.get("data", data if isinstance(data, list) else [])
            if isinstance(item, dict) and (item.get("id") or item.get("name"))
        ]
        return UpstreamSuccess(
            status_code=resp.status_code,
            json_body={"model_ids": model_ids},
            latency_ms=latency_ms,
        )

    async def discover_models(self, *, api_key: str) -> list[DiscoveredModel]:
        settings = self.config.dynamic_models
        if not settings.enabled or not settings.endpoint:
            return []

        now = time.monotonic()
        if self._discovered_models and now - self._discovery_at < settings.refresh_interval_seconds:
            return list(self._discovered_models)
        if self._discovery_failure_at and now - self._discovery_failure_at < min(settings.refresh_interval_seconds, 60):
            return list(self._discovered_models)

        async with self._discovery_lock:
            now = time.monotonic()
            if self._discovered_models and now - self._discovery_at < settings.refresh_interval_seconds:
                return list(self._discovered_models)
            if self._discovery_failure_at and now - self._discovery_failure_at < min(settings.refresh_interval_seconds, 60):
                return list(self._discovered_models)

            from app.observability.logging import log_event

            log_event("dynamic_model_discovery_started", provider=self.config.id)
            try:
                headers = _build_headers(self.config.authentication, api_key, self.config.headers)
                response = await self._client.get(settings.endpoint, headers=headers)
                response.raise_for_status()
                raw_models = response.json().get("data", [])
                models = [_parse_discovered_model(item) for item in raw_models if isinstance(item, dict)]
                models = [model for model in models if model is not None]
                self._discovered_models = models
                self._discovery_at = time.monotonic()
                self.discovered_at = time.time()
                self._discovery_failure_at = 0.0
                log_event("dynamic_model_discovery_succeeded", provider=self.config.id, discovered=len(models))
            except Exception as exc:
                self._discovery_failure_at = time.monotonic()
                stale = time.monotonic() - self._discovery_at if self._discovery_at else None
                if stale is not None and stale > settings.stale_after_seconds:
                    self._discovered_models = []
                log_event("dynamic_model_discovery_failed", level="warning", provider=self.config.id, error=str(exc)[:200])
            return list(self._discovered_models)

    async def aclose(self) -> None:
        if self._refresh is not None and not self._refresh.done():
            self._refresh.cancel()
        await self._client.aclose()


def _parse_discovered_model(item: dict[str, Any]) -> DiscoveredModel | None:
    model_id = item.get("id")
    architecture = item.get("architecture") or {}
    pricing = item.get("pricing") or {}
    if not isinstance(model_id, str) or not model_id:
        return None
    try:
        prompt_price = float(pricing["prompt"]) if pricing.get("prompt") is not None else None
        completion_price = float(pricing["completion"]) if pricing.get("completion") is not None else None
    except (TypeError, ValueError):
        prompt_price = completion_price = None
    return DiscoveredModel(
        id=model_id,
        context_length=item.get("context_length") if isinstance(item.get("context_length"), int) else None,
        input_modalities=frozenset(architecture.get("input_modalities") or []),
        output_modalities=frozenset(architecture.get("output_modalities") or []),
        supported_parameters=frozenset(item.get("supported_parameters") or []),
        prompt_price=prompt_price,
        completion_price=completion_price,
        deprecated=bool(item.get("deprecated") or item.get("decommissioned")),
    )


class UpstreamStreamError(Exception):
    """Raised by chat_completion_stream before any bytes have been yielded,
    so the router can still fail over. Once a byte has been yielded, the
    caller (app.api.chat) must NOT catch this to fail over — see
    ARCHITECTURE.md streaming notes."""

    def __init__(self, status_code: int, body_text: str, headers: httpx.Headers) -> None:
        super().__init__(f"upstream stream error {status_code}")
        self.status_code = status_code
        self.body_text = body_text
        self.headers = headers
