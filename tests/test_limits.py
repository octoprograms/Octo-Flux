from __future__ import annotations

from app.core.limits import LimitState
from app.models.provider import LimitsConfig


def test_rpm_limit_blocks_after_threshold():
    state = LimitState.from_config(LimitsConfig(requests_per_minute=3))
    for _ in range(3):
        assert state.has_request_budget()
        state.record_request_start()
    assert not state.has_request_budget()


def test_rpd_and_rpm_are_independent_windows():
    state = LimitState.from_config(LimitsConfig(requests_per_minute=1000, requests_per_day=2))
    state.record_request_start()
    state.record_request_start()
    assert not state.has_request_budget()  # RPD hit even though RPM has headroom


def test_token_budget_respects_tpm():
    state = LimitState.from_config(LimitsConfig(tokens_per_minute=100))
    assert state.has_token_budget(100)
    assert not state.has_token_budget(101)
    state.record_request_end(tokens_used=100)
    assert not state.has_token_budget(1)


def test_concurrency_limit():
    state = LimitState.from_config(LimitsConfig(concurrent_requests=2))
    state.record_request_start()
    state.record_request_start()
    assert not state.has_request_budget()
    state.record_request_end()
    assert state.has_request_budget()


def test_no_limits_configured_means_unbounded():
    state = LimitState.from_config(LimitsConfig())
    for _ in range(1000):
        assert state.has_request_budget()
        state.record_request_start()


from app.core.limits import _SlidingWindowCounter


def test_counter_memory_constant():
    counter = _SlidingWindowCounter(60.0, 10**9)
    for i in range(100_000):
        counter.record(1, now=i * 0.01)
    assert len(counter.counts) == 60


def test_counter_expires_after_window():
    counter = _SlidingWindowCounter(60.0, 10)
    counter.record(1, now=0.0)
    assert counter.current_total(now=59.5) == 1
    assert counter.current_total(now=61.0) == 0


def test_counter_limit_at_edge():
    counter = _SlidingWindowCounter(60.0, 5)
    for _ in range(5):
        counter.record(1, now=100.0)
    assert counter.would_exceed(1, now=100.0) is True
    assert counter.would_exceed(0, now=100.0) is False


def test_counter_dump_load_round_trip():
    counter = _SlidingWindowCounter(3600.0, 100)
    counter.record(3, now=1000.0)
    counter.record(4, now=1100.0)
    restored = _SlidingWindowCounter(3600.0, 100)
    restored.load(counter.dump())
    assert restored.current_total(now=1200.0) == 7
    assert restored.current_total(now=1000.0 + 3600 + 120) == 0


import httpx
import pytest
import respx

from app.core.app_state import AppState
from app.core.retry import ClientFacingError
from tests.conftest import make_config
from tests.mock_provider import ok_response

ONE_PROVIDER = """
providers:
  solo:
    base_url: "http://solo.test/v1"
    keys:
      - {{name: k1, value: "v1"}}
      - {{name: k2, value: "v2"}}
    models: [{{id: m}}]
    {extra}
"""


def _state(extra: str, tmp_path) -> AppState:
    import os

    os.environ["OctoFlux_USAGE_DB"] = str(tmp_path / "usage.db")
    return AppState.build(make_config(ONE_PROVIDER.format(extra=extra)))


async def _ask(state: AppState, content: str = "hi") -> str:
    result = await state.scheduler.execute_chat_completion(
        requested_model="m", payload={"messages": [{"role": "user", "content": content}]}, request_id="r"
    )
    return result.key_name


@pytest.mark.asyncio
@respx.mock
async def test_two_keys_double_capacity(tmp_path):
    respx.post("http://solo.test/v1/chat/completions").mock(return_value=ok_response())
    state = _state("limits: {requests_per_minute: 1}", tmp_path)
    assert {await _ask(state), await _ask(state)} == {"k1", "k2"}
    with pytest.raises(ClientFacingError) as exc:
        await _ask(state)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
@respx.mock
async def test_total_limits_cap_all_keys(tmp_path):
    respx.post("http://solo.test/v1/chat/completions").mock(return_value=ok_response())
    state = _state("total_limits: {requests_per_minute: 1}", tmp_path)
    await _ask(state)
    with pytest.raises(ClientFacingError) as exc:
        await _ask(state)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
@respx.mock
async def test_token_budget_skips_key(tmp_path):
    respx.post("http://solo.test/v1/chat/completions").mock(return_value=ok_response())
    state = _state("limits: {tokens_per_minute: 10}", tmp_path)
    with pytest.raises(ClientFacingError) as exc:
        await _ask(state, "x" * 100)
    assert exc.value.status_code == 503
    assert any("token limit" in reason for reason in exc.value.tried)


@pytest.mark.asyncio
@respx.mock
async def test_zero_remaining_cools_key_until_reset(tmp_path):
    exhausted = ok_response()
    exhausted.headers["x-ratelimit-remaining-requests"] = "0"
    exhausted.headers["x-ratelimit-reset-requests"] = "30s"
    respx.post("http://solo.test/v1/chat/completions").mock(side_effect=[exhausted, ok_response(), ok_response()])
    state = _state("key_selection: priority", tmp_path)
    assert await _ask(state) == "k1"
    assert await _ask(state) == "k2"
    assert state.health.key("solo", "k1").is_in_cooldown()


def test_key_limits_override_provider_limits():
    from app.core.limits import effective_key_limits

    config = make_config(
        ONE_PROVIDER.format(extra="limits: {requests_per_minute: 5, tokens_per_minute: 100}")
        .replace('{name: k1, value: "v1"}', '{name: k1, value: "v1", limits: {requests_per_minute: 2}}')
    )
    provider = config.providers["solo"]
    merged = effective_key_limits(provider, provider.keys[0])
    assert (merged.requests_per_minute, merged.tokens_per_minute) == (2, 100)
    assert effective_key_limits(provider, provider.keys[1]).requests_per_minute == 5


def test_estimate_tokens():
    from app.core.limits import estimate_tokens

    payload = {"messages": [{"content": "a" * 10}, {"content": [{"type": "text", "text": "b" * 6}]}], "max_tokens": 50}
    assert estimate_tokens(payload) == 4 + 50
