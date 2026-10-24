from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.app_state import AppState
from app.core.config import _resolve_env_recursive, load_yaml_text
from app.models.provider import OctoFluxConfig
from tests.conftest import make_config


def openrouter_config(*, keys: int = 2, with_groq: bool = False) -> OctoFluxConfig:
    key_lines = "\n".join(
        f"              - name: openrouter-{index}\n                value: key-{index}"
        for index in range(1, keys + 1)
    )
    groq = """
          groq:
            base_url: http://groq.test/v1
            priority: 20
            keys:
              - {name: groq-primary, value: groq-key}
            models:
              - {id: groq-model, priority: 10}
    """ if with_groq else ""
    raw = load_yaml_text(
        f"""
        server:
          require_auth: false
        routing:
          max_total_attempts: 6
        providers:
          openrouter:
            base_url: http://openrouter.test/v1
            priority: 10
            keys:
{key_lines}
            models:
              - {{id: configured-free-model, priority: 10}}
            dynamic_models:
              enabled: true
              endpoint: http://openrouter.test/v1/models?dynamic=1
{groq}
        """
    )
    resolved = _resolve_env_recursive(raw)
    for provider_id, provider in resolved["providers"].items():
        provider["id"] = provider_id
    return OctoFluxConfig.model_validate(resolved)


def free_model(model_id: str, context: int = 131072, tools: bool = True) -> dict:
    return {
        "id": model_id,
        "context_length": context,
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {"output_modalities": ["text"]},
        "supported_parameters": ["tools", "tool_choice"] if tools else ["temperature"],
    }


def mock_discovery(*models: dict):
    return respx.get("http://openrouter.test/v1/models?dynamic=1").mock(
        return_value=httpx.Response(200, json={"data": list(models)})
    )


def sent_models() -> list[str]:
    return [json.loads(call.request.content)["model"] for call in respx.calls if call.request.method == "POST"]


def client_for(state: AppState) -> TestClient:
    from app.main import create_app

    app = create_app()
    app.state.OctoFlux = state
    return TestClient(app)


def _only_free_router_works(request: httpx.Request) -> httpx.Response:
    if json.loads(request.content)["model"] == "openrouter/free":
        return httpx.Response(200, json={"model": "selected/model:free", "choices": []})
    return httpx.Response(404, text="model not found")


@pytest.mark.asyncio
@respx.mock
async def test_auto_tries_discovered_before_openrouter_free():
    mock_discovery(free_model("big/model:free"))
    respx.post("http://openrouter.test/v1/chat/completions").mock(side_effect=_only_free_router_works)
    state = AppState.build(openrouter_config(keys=1))

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="auto-openrouter"
    )

    assert sent_models() == ["configured-free-model", "big/model:free", "openrouter/free"]
    assert result.model_id == "openrouter/free"


@pytest.mark.asyncio
@respx.mock
async def test_filters_drop_non_chat_and_short_context():
    mock_discovery(
        free_model("acme/content-safety:free"),
        free_model("acme/text-embedding:free"),
        free_model("acme/llama-guard:free"),
        free_model("acme/tiny:free", context=16384),
        free_model("acme/good:free"),
    )
    state = AppState.build(openrouter_config(keys=1))

    resolution = await state.scheduler.router.resolve_dynamic("auto", payload={"messages": []})

    models = [c.model_id for c in resolution.candidates]
    assert models == ["configured-free-model", "acme/good:free", "openrouter/free"]


@pytest.mark.asyncio
@respx.mock
async def test_tools_request_drops_models_without_tools():
    mock_discovery(free_model("acme/no-tools:free", tools=False), free_model("acme/tools:free"))
    state = AppState.build(openrouter_config(keys=1))

    resolution = await state.scheduler.router.resolve_dynamic(
        "auto", payload={"messages": [], "tools": [{"type": "function", "function": {"name": "f"}}]}
    )

    assert "acme/no-tools:free" not in [c.model_id for c in resolution.candidates]
    assert "acme/tools:free" in [c.model_id for c in resolution.candidates]


@pytest.mark.asyncio
@respx.mock
async def test_blocked_model_never_used():
    mock_discovery(free_model("acme/bad:free"), free_model("acme/good:free"))
    config = openrouter_config(keys=1)
    config.providers["openrouter"].dynamic_models.blocked = ["acme/bad:free"]
    state = AppState.build(config)

    resolution = await state.scheduler.router.resolve_dynamic("auto", payload={"messages": []})

    assert "acme/bad:free" not in [c.model_id for c in resolution.candidates]


@pytest.mark.asyncio
@respx.mock
async def test_discovery_down_auto_still_routes():
    respx.get("http://openrouter.test/v1/models?dynamic=1").mock(return_value=httpx.Response(500))
    respx.post("http://openrouter.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    state = AppState.build(openrouter_config(keys=1))

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="discovery-down"
    )

    assert result.model_id == "configured-free-model"


@pytest.mark.asyncio
@respx.mock
async def test_free_router_last_resort_when_discovery_down():
    respx.get("http://openrouter.test/v1/models?dynamic=1").mock(return_value=httpx.Response(500))
    respx.post("http://openrouter.test/v1/chat/completions").mock(side_effect=_only_free_router_works)
    state = AppState.build(openrouter_config(keys=1))

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="discovery-down-2"
    )

    assert sent_models() == ["configured-free-model", "openrouter/free"]
    assert result.model_id == "openrouter/free"


@pytest.mark.asyncio
@respx.mock
async def test_explicit_openrouter_model_is_unchanged():
    completion = respx.post("http://openrouter.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"model": "configured-free-model", "choices": []})
    )
    state = AppState.build(openrouter_config(keys=1))

    await state.scheduler.execute_chat_completion(
        requested_model="configured-free-model", payload={"messages": []}, request_id="explicit-openrouter"
    )

    assert json.loads(completion.calls[0].request.content)["model"] == "configured-free-model"


@pytest.mark.asyncio
@respx.mock
async def test_auto_groq_keeps_configured_model_behavior():
    state = AppState.build(make_config(
        """
        server: {require_auth: false}
        providers:
          groq:
            base_url: http://groq.test/v1
            keys: [{name: key, value: secret}]
            models: [{id: groq-model}]
        """
    ))
    completion = respx.post("http://groq.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="auto-groq"
    )

    assert json.loads(completion.calls[0].request.content)["model"] == "groq-model"


@pytest.mark.asyncio
@respx.mock
async def test_auto_nvidia_keeps_configured_model_behavior():
    state = AppState.build(make_config(
        """
        server: {require_auth: false}
        providers:
          nvidia_nim:
            base_url: http://nvidia.test/v1
            keys: [{name: key, value: secret}]
            models: [{id: nvidia-model}]
        """
    ))
    completion = respx.post("http://nvidia.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="auto-nvidia"
    )

    assert json.loads(completion.calls[0].request.content)["model"] == "nvidia-model"


@respx.mock
def test_streaming_auto_openrouter_uses_free_router():
    stream_body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    mock_discovery(free_model("big/model:free"))

    def completion(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "openrouter/free":
            return httpx.Response(200, content=stream_body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, text="model not found")

    respx.post("http://openrouter.test/v1/chat/completions").mock(side_effect=completion)
    client = client_for(AppState.build(openrouter_config(keys=1)))

    with client.stream(
        "POST", "/v1/chat/completions",
        json={"model": "auto", "messages": [], "stream": True},
    ) as response:
        assert response.status_code == 200
        assert b"hi" in b"".join(response.iter_bytes())

    assert sent_models() == ["configured-free-model", "big/model:free", "openrouter/free"]


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_rate_limit_rotates_keys():
    seen_auth: list[str] = []

    def completion(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers["authorization"])
        if len(seen_auth) == 1:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json={"choices": []})

    respx.post("http://openrouter.test/v1/chat/completions").mock(side_effect=completion)
    state = AppState.build(openrouter_config(keys=2))

    await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="auto-429"
    )

    assert len(seen_auth) == 2
    assert len(set(seen_auth)) == 2


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_auth_failure_falls_back_to_next_provider():
    respx.post("http://openrouter.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, text="invalid key")
    )
    groq = respx.post("http://groq.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    state = AppState.build(openrouter_config(keys=2, with_groq=True))

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="auto-auth"
    )

    assert result.provider_id == "groq"
    assert groq.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_free_unavailable_falls_back_to_next_provider():
    respx.post("http://openrouter.test/v1/chat/completions").mock(
        return_value=httpx.Response(404, text="model not found")
    )
    groq = respx.post("http://groq.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    state = AppState.build(openrouter_config(keys=2, with_groq=True))

    result = await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="auto-unavailable"
    )

    assert result.provider_id == "groq"
    assert groq.call_count == 1
    assert sent_models() == ["configured-free-model", "openrouter/free", "groq-model"]


def test_models_endpoint_remains_backward_compatible():
    client = client_for(AppState.build(openrouter_config(keys=1)))

    response = client.get("/v1/models")
    ids = {model["id"] for model in response.json()["data"]}

    assert "auto" in ids
    assert "configured-free-model" in ids
    assert "openrouter/free" not in ids


@pytest.mark.asyncio
@respx.mock
async def test_logs_distinguish_requested_and_upstream_models(monkeypatch):
    import app.core.retry as retry_module

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(retry_module, "log_event", lambda event, **fields: events.append((event, fields)))
    respx.post("http://openrouter.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    state = AppState.build(openrouter_config(keys=1))

    await state.scheduler.execute_chat_completion(
        requested_model="auto", payload={"messages": []}, request_id="auto-log"
    )

    routing = next(fields for event, fields in events if event == "routing_decision")
    assert routing["requested_model"] == "auto"
    assert routing["upstream_model"] == "configured-free-model"


@pytest.mark.asyncio
@respx.mock
async def test_slow_discovery_does_not_stall_auto(monkeypatch):
    import asyncio
    import time

    import app.core.router as router_module

    monkeypatch.setattr(router_module, "DISCOVERY_WAIT_SECONDS", 0.2)

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={"data": [free_model("late/model:free")]})

    respx.get("http://openrouter.test/v1/models?dynamic=1").mock(side_effect=slow)
    state = AppState.build(openrouter_config(keys=1))

    start = time.perf_counter()
    resolution = await state.scheduler.router.resolve_dynamic("auto", payload={"messages": []})
    assert time.perf_counter() - start < 1.0
    assert [c.model_id for c in resolution.candidates] == ["configured-free-model", "openrouter/free"]
    await state.providers.aclose()
