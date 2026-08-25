from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass
class UpstreamSuccess:
    status_code: int
    json_body: dict[str, Any]
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class UpstreamFailure:
    status_code: int | None  # None for connection-level failures (no response received)
    body_text: str
    latency_ms: float
    exception: Exception | None = None
    headers: httpx.Headers | None = None


class ProviderAdapter(Protocol):
    """A provider adapter knows how to speak to one upstream base_url."""

    async def chat_completion(
        self, *, model: str, payload: dict[str, Any], api_key: str, request_id: str
    ) -> UpstreamSuccess | UpstreamFailure: ...

    def chat_completion_stream(
        self, *, model: str, payload: dict[str, Any], api_key: str, request_id: str
    ) -> AsyncIterator[bytes]:
        """Returns an async iterator of raw SSE bytes chunks. Implementations
        should raise before yielding the first chunk if the connection/auth
        fails, so the caller can still fail over pre-first-byte."""
        ...

    async def list_models(self) -> list[str]: ...

    async def aclose(self) -> None: ...
