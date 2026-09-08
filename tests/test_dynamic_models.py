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
async def test_model_not_found_skips_remaining_keys_for_logical_model():
    respx.get(DISCOVERY_URL).mock(return_value=discovery_response())
    models_seen: list[tuple[str, str]] = []

    def completion(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "top/model:free" in body:
            model = "top/model:free"
        elif "configured-model" in body:
            model = "configured-model"
        else:
            model = "second/model:free"
        models_seen.append((model, request.headers.get("authorization", "")))
        if model in {"top/model:free", "configured-model"}:
            return httpx.Response(404, json={"error": {"code": "model_not_found"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    completions = respx.post("http://openrouter.test/v1/chat/completions").mock(side_effect=completion)
    state = AppState.build(
        dynamic_config(
            keys=[
                {"name": "primary", "value": "key-1"},
                {"name": "secondary", "value": "key-2"},
                {"name": "tertiary", "value": "key-3"},
            ]
        )
    )

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="model-404"
    )

    assert result.model_id == "second/model:free"
    assert [model for model, _ in models_seen].count("top/model:free") == 1
    assert state.health.model("openrouter", "top/model:free").is_in_cooldown()
    assert completions.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_can_fail_over_to_next_key_for_same_model():
    respx.get(DISCOVERY_URL).mock(return_value=discovery_response())
    seen: list[str] = []

    def completion(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        auth = request.headers.get("authorization", "")
        if "configured-model" in body:
            return httpx.Response(404, json={"error": {"code": "model_not_found"}})
        if "top/model:free" in body:
            seen.append(auth)
            if len(seen) == 1:
                return httpx.Response(429, json={"error": {"message": "rate limit"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}}]})

    respx.post("http://openrouter.test/v1/chat/completions").mock(side_effect=completion)
    state = AppState.build(
        dynamic_config(
            keys=[
                {"name": "primary", "value": "key-1"},
                {"name": "secondary", "value": "key-2"},
            ]
        )
    )

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="key-429"
    )

    assert result.model_id == "top/model:free"
    assert len(seen) == 2
    assert len(set(seen)) == 2
    assert not state.health.model("openrouter", "top/model:free").is_in_cooldown()


@pytest.mark.asyncio
@respx.mock
async def test_dynamic_discovery_deduplicates_logical_model_ids():
    response = discovery_response()
    response.json()["data"].insert(1, response.json()["data"][0])
    respx.get(DISCOVERY_URL).mock(return_value=response)
    state = AppState.build(dynamic_config(max_candidates=3))

    resolution = await state.scheduler.router.resolve_dynamic("auto")
    dynamic_ids = [candidate.model_id for candidate in resolution.candidates if candidate.dynamic]

    assert dynamic_ids == ["top/model:free", "second/model:free"]


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
