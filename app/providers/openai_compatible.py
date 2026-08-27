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
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.models.provider import AuthenticationConfig, ProviderConfig
from app.providers.base import UpstreamFailure, UpstreamSuccess


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
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
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

    async def aclose(self) -> None:
        await self._client.aclose()


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
