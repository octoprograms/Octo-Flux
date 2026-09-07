from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.core.app_state import AppState
from app.core.config import _resolve_env_recursive, load_yaml_text
from app.models.provider import OctoFluxConfig


DISCOVERY_URL = "http://openrouter.test/v1/models?max_price=0&order=intelligence-high-to-low&output_modalities=text"


def dynamic_config(**dynamic_overrides) -> OctoFluxConfig:
    dynamic = {
        "enabled": True,
        "endpoint": DISCOVERY_URL,
        "max_candidates": 2,
        "refresh_interval_seconds": 60,
        "stale_after_seconds": 300,
        **dynamic_overrides,
    }
    raw = load_yaml_text(
        f"""
        server:
          require_auth: false
        routing:
          max_total_attempts: 5
        providers:
          openrouter:
            base_url: http://openrouter.test/v1
            keys: [{{name: primary, value: key}}]
            models: [{{id: configured-model, priority: 10}}]
            dynamic_models: {dynamic!r}
        """
    )
    resolved = _resolve_env_recursive(raw)
    resolved["providers"]["openrouter"]["id"] = "openrouter"
    return OctoFluxConfig.model_validate(resolved)


def discovery_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "top/model:free",
                    "context_length": 128000,
                    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                    "pricing": {"prompt": "0", "completion": "0"},
                    "supported_parameters": ["tools", "max_tokens"],
                },
                {
                    "id": "second/model:free",
                    "context_length": 4096,
                    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                    "pricing": {"prompt": "0", "completion": "0"},
                },
                {
                    "id": "paid/model",
                    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                    "pricing": {"prompt": "1", "completion": "1"},
                },
                {
                    "id": "image/model:free",
                    "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                    "pricing": {"prompt": "0", "completion": "0"},
                },
            ]
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_discovery_is_cached_and_top_n_is_applied():
    route = respx.get(DISCOVERY_URL).mock(return_value=discovery_response())
    state = AppState.build(dynamic_config())

    first = await state.scheduler.router.resolve_dynamic("auto")
    second = await state.scheduler.router.resolve_dynamic("auto")

    dynamic_ids = [candidate.model_id for candidate in first.candidates if candidate.dynamic]
    assert dynamic_ids == ["top/model:free", "second/model:free"]
    assert [candidate.model_id for candidate in second.candidates if candidate.dynamic] == dynamic_ids
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_discovery_failure_keeps_static_candidates_and_cached_models():
    route = respx.get(DISCOVERY_URL).mock(return_value=discovery_response())
    state = AppState.build(dynamic_config())
    await state.scheduler.router.resolve_dynamic("auto")
    route.return_value = httpx.Response(503, text="temporary failure")
    state.providers.adapter("openrouter")._discovery_at = 0.0
    state.providers.adapter("openrouter")._discovery_failure_at = 0.0

    resolution = await state.scheduler.router.resolve_dynamic("auto")
    assert any(candidate.model_id == "configured-model" for candidate in resolution.candidates)
    assert any(candidate.model_id == "top/model:free" for candidate in resolution.candidates)


@pytest.mark.asyncio
@respx.mock
async def test_dynamic_model_failure_fails_over_and_enters_model_cooldown():
    discovery = respx.get(DISCOVERY_URL).mock(return_value=discovery_response())
    completions = respx.post("http://openrouter.test/v1/chat/completions")
    completions.side_effect = [
        httpx.Response(503, json={"error": {"message": "overloaded"}}),
        httpx.Response(503, json={"error": {"message": "overloaded"}}),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    ]
    state = AppState.build(dynamic_config())

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="dynamic-1"
    )

    assert result.model_id == "second/model:free"
    assert state.health.model("openrouter", "top/model:free").is_in_cooldown()
    assert discovery.call_count == 1
    assert completions.call_count == 3

    next_resolution = state.scheduler.router.resolve("auto")
    assert all(candidate.model_id != "top/model:free" for candidate in next_resolution.candidates)


@pytest.mark.asyncio
@respx.mock
async def test_discovery_refresh_is_serialized():
    route = respx.get(DISCOVERY_URL).mock(return_value=discovery_response())
    state = AppState.build(dynamic_config())
    adapter = state.providers.adapter("openrouter")
    adapter._discovery_at = 0.0

    await asyncio.gather(*(adapter.discover_models(api_key="key") for _ in range(10)))

    assert route.call_count == 1


def test_dynamic_discovery_is_disabled_by_default():
    config = dynamic_config(enabled=False)
    assert config.providers["openrouter"].dynamic_models.enabled is False
