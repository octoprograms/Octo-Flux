from __future__ import annotations

import httpx
import pytest
import respx

from app.core.app_state import AppState
from app.core.retry import ClientFacingError
from tests.mock_provider import error_response, ok_response


@pytest.mark.asyncio
@respx.mock
async def test_success_on_first_candidate(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response("hi from alpha"))

    result = await app_state.scheduler.execute_chat_completion(
        requested_model="model-b" if False else "model-a",
        payload={"messages": [{"role": "user", "content": "hi"}]},
        request_id="req1",
    )
    assert result.provider_id == "alpha"
    assert result.json_body["choices"][0]["message"]["content"] == "hi from alpha"


@pytest.mark.asyncio
@respx.mock
async def test_provider_a_429_fails_over_to_provider_b(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(
        return_value=error_response(429, "rate limited", "rate_limit_error")
    )
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response("hi from beta"))

    result = await app_state.scheduler.execute_chat_completion(
        requested_model="model-a", payload={"messages": []}, request_id="req2"
    )
    assert result.provider_id == "beta"
    assert len(result.trace) == 3  # both alpha keys tried (rate_limited retries other keys), then beta succeeds


@pytest.mark.asyncio
@respx.mock
async def test_key_exhausted_then_second_key_fails_then_third_provider_succeeds(app_state: AppState):
    # alpha-key-1 -> 429 (rate limited), alpha-key-2 -> 401 (auth failure), beta -> success
    call_count = {"n": 0}

    def alpha_side_effect(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        auth = request.headers.get("authorization")
        if auth == "Bearer k1":
            return error_response(429, "rate limited")
        return error_response(401, "invalid api key")

    respx.post("http://alpha.test/v1/chat/completions").mock(side_effect=alpha_side_effect)
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response("beta success"))

    result = await app_state.scheduler.execute_chat_completion(
        requested_model="model-a", payload={"messages": []}, request_id="req3"
    )
    assert result.provider_id == "beta"
    assert call_count["n"] == 2  # both alpha keys were tried


@pytest.mark.asyncio
@respx.mock
async def test_model_failure_falls_back_to_alternate_model_via_alias(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(500, "boom"))
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response("beta model-a"))

    result = await app_state.scheduler.execute_chat_completion(
        requested_model="fast", payload={"messages": []}, request_id="req4"
    )
    assert result.provider_id == "beta"


@pytest.mark.asyncio
@respx.mock
async def test_timeout_fails_over_to_next_provider(app_state: AppState):
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    respx.post("http://alpha.test/v1/chat/completions").mock(side_effect=raise_timeout)
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response("beta after timeout"))

    result = await app_state.scheduler.execute_chat_completion(
        requested_model="model-a", payload={"messages": []}, request_id="req5"
    )
    assert result.provider_id == "beta"


@pytest.mark.asyncio
@respx.mock
async def test_invalid_request_returns_immediately_without_failover(app_state: AppState):
    alpha_route = respx.post("http://alpha.test/v1/chat/completions").mock(
        return_value=error_response(400, "invalid request: bad field")
    )
    beta_route = respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response())

    with pytest.raises(ClientFacingError) as exc_info:
        await app_state.scheduler.execute_chat_completion(
            requested_model="model-a", payload={"messages": []}, request_id="req6"
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.error_type == "invalid_request"
    assert not beta_route.called  # must NOT have failed over


@pytest.mark.asyncio
@respx.mock
async def test_context_length_exceeded_does_not_fail_over(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(
        return_value=error_response(400, "This model's maximum context length is 4096 tokens")
    )
    beta_route = respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response())

    with pytest.raises(ClientFacingError) as exc_info:
        await app_state.scheduler.execute_chat_completion(
            requested_model="model-a", payload={"messages": []}, request_id="req7"
        )
    assert exc_info.value.error_type == "context_length_exceeded"
    assert not beta_route.called


@pytest.mark.asyncio
@respx.mock
async def test_all_candidates_exhausted_returns_service_unavailable(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(500, "boom"))
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=error_response(500, "boom too"))

    with pytest.raises(ClientFacingError) as exc_info:
        await app_state.scheduler.execute_chat_completion(
            requested_model="model-a", payload={"messages": []}, request_id="req8"
        )
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
@respx.mock
async def test_repeated_auth_failure_marks_key_unhealthy_and_next_request_skips_it(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(401, "bad key"))
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response())

    for i in range(2):  # failure_threshold = 2 for alpha in fixture
        await app_state.scheduler.execute_chat_completion(
            requested_model="model-a", payload={"messages": []}, request_id=f"req9-{i}"
        )

    resolution = app_state.scheduler.router.resolve("model-a")
    alpha_keys = {c.key.name for c in resolution.candidates if c.provider_id == "alpha"}
    assert alpha_keys == set()  # both alpha keys should now be in cooldown
