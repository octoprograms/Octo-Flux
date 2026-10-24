from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.app_state import AppState
from tests.conftest import ADMIN
from tests.mock_provider import error_response, ok_response


def _client_for(app_state: AppState) -> TestClient:
    from app.main import create_app

    app = create_app()
    app.state.OctoFlux = app_state  # bypass lifespan's own config load
    return TestClient(app)


@respx.mock
def test_chat_completions_endpoint_success(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response("via api"))
    client = _client_for(app_state)
    resp = client.post("/v1/chat/completions", json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "via api"


@respx.mock
def test_chat_completions_endpoint_failover_to_beta(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(500, "boom"))
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response("beta wins"))
    client = _client_for(app_state)
    resp = client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "beta wins"


def test_models_endpoint_lists_configured_models(app_state: AppState):
    client = _client_for(app_state)
    resp = client.get("/v1/models")
    ids = {m["id"] for m in resp.json()["data"]}
    assert "model-a" in ids
    assert "model-b" in ids
    assert "auto" in ids
    assert "fast" in ids  # alias


def test_aliases_endpoint_exposes_ordered_targets(app_state: AppState):
    client = _client_for(app_state)
    resp = client.get("/v1/aliases")

    assert resp.status_code == 200
    assert resp.json()["data"]["fast"] == [
        {"provider": "alpha", "model": "model-a", "enabled": True},
        {"provider": "beta", "model": "model-a", "enabled": True},
    ]


def test_health_endpoint(app_state: AppState):
    client = _client_for(app_state)
    resp = client.get("/admin/health", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["providers"]["alpha"]["keys"]["alpha-key-1"]["status"] == "unknown"


@respx.mock
def test_admin_can_test_one_provider_key(app_state: AppState):
    route = respx.get("http://alpha.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "model-a"}]})
    )
    client = _client_for(app_state)

    resp = client.post("/admin/providers/alpha/keys/alpha-key-1/test", headers=ADMIN)

    assert resp.status_code == 200
    assert resp.json()["status"] == "working"
    assert resp.json()["key_hint"] == "**"
    assert resp.json()["models"] == [{"id": "model-a", "available": True}]
    assert route.call_count == 1
    assert app_state.health.key("alpha", "alpha-key-1").last_check_ok is True


@respx.mock
def test_admin_status_reports_latest_model_availability(app_state: AppState):
    respx.get("http://alpha.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "model-a"}]})
    )
    client = _client_for(app_state)

    client.post("/admin/providers/alpha/keys/alpha-key-1/test", headers=ADMIN)
    models = client.get("/admin/status", headers=ADMIN).json()["providers"]["alpha"]["models"]

    assert models == [{"id": "model-a", "enabled": True, "priority": 10, "available": True}]


@respx.mock
def test_admin_provider_key_test_reports_upstream_failure(app_state: AppState):
    respx.get("http://alpha.test/v1/models").mock(return_value=httpx.Response(401, text="invalid key"))
    client = _client_for(app_state)

    resp = client.post("/admin/providers/alpha/keys/alpha-key-1/test", headers=ADMIN)

    assert resp.status_code == 200
    assert resp.json()["status"] == "unhealthy"
    assert resp.json()["status_code"] == 401
    assert app_state.health.key("alpha", "alpha-key-1").last_check_ok is False


@respx.mock
@pytest.mark.asyncio
async def test_health_and_completion_use_same_key_auth_but_different_endpoints(app_state: AppState):
    health = respx.get("http://alpha.test/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
    completion = respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response())

    await app_state.health_monitor.check_key("alpha", "alpha-key-1", "k1")
    await app_state.scheduler.execute_chat_completion(
        requested_model="model-a", payload={"messages": []}, request_id="request-path-1"
    )

    assert health.calls[0].request.url.path == "/v1/models"
    assert completion.calls[0].request.url.path == "/v1/chat/completions"
    assert health.calls[0].request.headers["authorization"] == "Bearer k1"
    assert completion.calls[0].request.headers["authorization"] == "Bearer k1"


def test_admin_provider_key_test_validates_target_and_auth(two_provider_config):
    two_provider_config.server.require_auth = True
    two_provider_config.server.client_keys = ["secret123"]
    state = AppState.build(two_provider_config)
    client = _client_for(state)

    auth = ADMIN
    assert client.post("/admin/providers/missing/keys/key/test", headers=auth).status_code == 404
    assert client.post("/admin/providers/alpha/keys/missing/test", headers=auth).status_code == 404
    assert client.post("/admin/providers/alpha/keys/alpha-key-1/test").status_code == 401


@respx.mock
@pytest.mark.asyncio
async def test_background_provider_check_records_working_key(app_state: AppState):
    route = respx.get("http://alpha.test/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
    await app_state.health_monitor.check_once()

    assert route.call_count == 2
    assert app_state.health.key("alpha", "alpha-key-1").last_check_ok is True
    assert app_state.health.key("alpha", "alpha-key-2").last_check_ok is True


def test_auth_required_when_configured(two_provider_config):
    two_provider_config.server.require_auth = True
    two_provider_config.server.client_keys = ["secret123"]
    state = AppState.build(two_provider_config)
    client = _client_for(state)

    resp = client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    assert resp.status_code == 401

    resp2 = client.get("/admin/status", headers={"Authorization": "Bearer wrong"})
    assert resp2.status_code == 401

    resp3 = client.get("/admin/status", headers={"Authorization": "Bearer secret123"})
    assert resp3.status_code == 401  # client keys no longer open /admin

    assert client.get("/admin/status", headers=ADMIN).status_code == 200


@respx.mock
def test_streaming_pre_first_byte_failover(app_state: AppState):
    sse_chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'

    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(429, "rate limited"))
    respx.post("http://beta.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=sse_chunk, headers={"content-type": "text/event-stream"})
    )

    client = _client_for(app_state)
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "model-a", "messages": [], "stream": True}
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())
        assert b"hi" in body


@respx.mock
def test_admin_usage_reflects_requests(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response())
    client = _client_for(app_state)
    client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    resp = client.get("/admin/status", headers=ADMIN)
    # require_auth is False in fixture config
    assert resp.status_code == 200

    usage_resp = client.get("/admin/usage", headers=ADMIN)
    assert usage_resp.json()["global"]["successful_requests"] >= 1


@respx.mock
def test_successful_request_counts_toward_token_limits():
    from tests.conftest import make_config

    state = AppState.build(make_config(
        """
        server: {require_auth: false}
        providers:
          alpha:
            base_url: "http://alpha.test/v1"
            keys: [{name: k, value: "k1"}]
            models: [{id: model-a}]
            limits: {tokens_per_minute: 1000}
        """
    ))
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response())
    _client_for(state).post("/v1/chat/completions", json={"model": "model-a", "messages": []})

    assert state.limits.snapshot()["keys"]["alpha:k"]["tokens_per_minute"]["used"] == 8  # 5 prompt + 3 completion


@respx.mock
def test_streaming_counts_toward_limits_and_usage(app_state: AppState):
    sse_chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    respx.post("http://alpha.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=sse_chunk, headers={"content-type": "text/event-stream"})
    )
    client = _client_for(app_state)
    before = client.get("/admin/usage", headers=ADMIN).json()["global"]["successful_requests"]
    with client.stream("POST", "/v1/chat/completions", json={"model": "model-a", "messages": [], "stream": True}) as resp:
        b"".join(resp.iter_bytes())

    keys = app_state.limits.snapshot()["keys"]
    alpha = [keys[name] for name in ("alpha:alpha-key-1", "alpha:alpha-key-2") if name in keys]
    assert sum(k["requests_per_minute"]["used"] for k in alpha) == 1
    assert all(k["in_flight"] == 0 for k in alpha)  # released once the stream finished
    assert client.get("/admin/usage", headers=ADMIN).json()["global"]["successful_requests"] == before + 1


@respx.mock
def test_streaming_error_does_not_echo_upstream_body(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(400, "secret upstream detail"))
    client = _client_for(app_state)
    resp = client.post("/v1/chat/completions", json={"model": "model-a", "messages": [], "stream": True})

    assert resp.status_code == 400
    assert "secret upstream detail" not in resp.text
