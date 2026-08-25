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
