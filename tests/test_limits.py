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
