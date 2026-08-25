"""Helpers for simulating OpenAI-compatible upstream providers in tests
without any real network calls (respx intercepts httpx at the transport
layer)."""
from __future__ import annotations

import httpx


def ok_response(content: str = "hello", model: str = "model-a") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )


def error_response(status_code: int, message: str = "error", error_type: str = "error", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"error": {"message": message, "type": error_type}},
        headers=headers or {},
    )
