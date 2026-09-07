from __future__ import annotations

import json

import pytest
import respx
from fastapi.testclient import TestClient

from app.core.app_state import AppState
from app.core.retry import ClientFacingError
from app.models.provider import AliasTarget, ModelConfig
from tests.mock_provider import embedding_response, error_response


def _client_for(app_state: AppState) -> TestClient:
    from app.main import create_app

    app = create_app()
    app.state.OctoFlux = app_state
    return TestClient(app)


@pytest.fixture
def embedding_app_state(two_provider_config) -> AppState:
    for provider in two_provider_config.providers.values():
        provider.models.append(ModelConfig(id="model-embed", priority=30, capabilities=["embeddings"]))
    two_provider_config.aliases["embedded"] = [
        AliasTarget(provider="alpha", model="model-embed"),
        AliasTarget(provider="beta", model="model-embed"),
    ]
    return AppState.build(two_provider_config)


def test_embedding_model_is_capability_specific(embedding_app_state: AppState):
    embedding = embedding_app_state.scheduler.router.resolve("model-embed", capability="embeddings")
    chat = embedding_app_state.scheduler.router.resolve("model-embed", capability="chat")

    assert embedding.candidates
    assert not chat.capability_found
    assert chat.candidates == []


def test_auto_and_alias_only_select_embedding_models(embedding_app_state: AppState):
    auto = embedding_app_state.scheduler.router.resolve("auto", capability="embeddings")
    alias = embedding_app_state.scheduler.router.resolve("embedded", capability="embeddings")

    assert {candidate.model_id for candidate in auto.candidates} == {"model-embed"}
    assert {candidate.model_id for candidate in alias.candidates} == {"model-embed"}


async def test_unsupported_embedding_model_rejected(embedding_app_state: AppState):
    try:
        await embedding_app_state.scheduler.execute_embeddings(
            requested_model="model-a", payload={"input": "hello"}, request_id="unsupported"
        )
    except ClientFacingError as exc:
        assert exc.status_code == 400
        assert exc.error_type == "unsupported_operation"
        assert "does not support embeddings" in exc.message
    else:
        raise AssertionError("chat-only model was accepted for embeddings")


@respx.mock
def test_embeddings_endpoint_supports_string_input_and_alias(embedding_app_state: AppState):
    route = respx.post("http://alpha.test/v1/embeddings").mock(return_value=embedding_response())
    client = _client_for(embedding_app_state)

    response = client.post("/v1/embeddings", json={"model": "embedded", "input": "hello"})

    assert response.status_code == 200
    assert response.json()["data"][0]["embedding"] == [0.1, 0.2]
    assert route.calls[0].request.content
    assert route.calls[0].request.headers["authorization"] == "Bearer k1"


@respx.mock
def test_embeddings_endpoint_supports_array_input_and_auto(embedding_app_state: AppState):
    route = respx.post("http://alpha.test/v1/embeddings").mock(return_value=embedding_response())
    client = _client_for(embedding_app_state)

    response = client.post("/v1/embeddings", json={"model": "auto", "input": ["one", "two"]})

    assert response.status_code == 200
    assert route.called
    assert json.loads(route.calls[0].request.content)["input"] == ["one", "two"]


@respx.mock
def test_embeddings_fail_over_between_providers(embedding_app_state: AppState):
    respx.post("http://alpha.test/v1/embeddings").mock(return_value=error_response(500, "boom"))
    beta = respx.post("http://beta.test/v1/embeddings").mock(return_value=embedding_response())
    client = _client_for(embedding_app_state)

    response = client.post("/v1/embeddings", json={"model": "model-embed", "input": "hello"})

    assert response.status_code == 200
    assert beta.called


def test_embeddings_requires_auth_when_configured(two_provider_config):
    two_provider_config.server.require_auth = True
    two_provider_config.server.client_keys = ["secret123"]
    client = _client_for(AppState.build(two_provider_config))

    response = client.post("/v1/embeddings", json={"model": "auto", "input": "hello"})

    assert response.status_code == 401