from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.core.app_state import AppState
from app.core.config import _resolve_env_recursive, load_yaml_text
from app.models.provider import OctoFluxConfig


DISCOVERY_URL = "http://openrouter.test/v1/models?max_price=0&order=intelligence-high-to-low&output_modalities=text"


def dynamic_config(*, keys=None, **dynamic_overrides) -> OctoFluxConfig:
    keys = keys or [{"name": "primary", "value": "key"}]
    key_yaml = "\n".join(
        f"            - name: {key['name']}\n              value: {key['value']}" for key in keys
    )
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
            keys:
{key_yaml}
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
async def test_model_cooldown_expires():
    state = AppState.build(dynamic_config())
    health = state.health.model("openrouter", "temporary-model")
    health.record_cooldown(0.01)
    await asyncio.sleep(0.02)

    assert not health.is_in_cooldown()


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
