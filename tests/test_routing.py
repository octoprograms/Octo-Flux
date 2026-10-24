from __future__ import annotations

from app.core.app_state import AppState


def test_single_provider_single_key_resolves(app_state: AppState):
    resolution = app_state.scheduler.router.resolve("model-b")  # only beta has model-b
    assert resolution.model_found
    assert len(resolution.candidates) == 1
    assert resolution.candidates[0].provider_id == "beta"


def test_multiple_providers_priority_order(app_state: AppState):
    resolution = app_state.scheduler.router.resolve("model-a")  # both alpha (10) and beta (20) have it
    assert resolution.model_found
    provider_order = [c.provider_id for c in resolution.candidates]
    # alpha (priority 10) candidates should come before beta (priority 20)
    assert provider_order.index("alpha") < provider_order.index("beta")


def test_unknown_model_not_found(app_state: AppState):
    resolution = app_state.scheduler.router.resolve("does-not-exist")
    assert not resolution.model_found
    assert resolution.candidates == []


def test_alias_resolves_in_declared_order(app_state: AppState):
    resolution = app_state.scheduler.router.resolve("fast")
    assert resolution.model_found
    assert resolution.candidates[0].provider_id == "alpha"


def test_key_selection_round_robin_rotates(app_state: AppState):
    first = app_state.scheduler.router.resolve("model-a")
    first_key = next(c.key.name for c in first.candidates if c.provider_id == "alpha")
    second = app_state.scheduler.router.resolve("model-a")
    second_key = next(c.key.name for c in second.candidates if c.provider_id == "alpha")
    assert first_key != second_key  # alpha has 2 keys, round robin should alternate


def test_cooldown_excludes_provider(app_state: AppState):
    app_state.health.provider("alpha").record_cooldown(30)
    resolution = app_state.scheduler.router.resolve("model-a")
    providers = {c.provider_id for c in resolution.candidates}
    assert "alpha" not in providers
    assert "beta" in providers
    assert any("cooldown" in e for e in resolution.excluded)


def test_cooldown_excludes_specific_key_but_not_other_key(app_state: AppState):
    app_state.health.key("alpha", "alpha-key-1").record_cooldown(30)
    resolution = app_state.scheduler.router.resolve("model-a")
    alpha_keys = {c.key.name for c in resolution.candidates if c.provider_id == "alpha"}
    assert "alpha-key-1" not in alpha_keys
    assert "alpha-key-2" in alpha_keys


def test_exhausted_local_rate_limit_excludes_provider(app_state):
    from app.models.provider import LimitsConfig

    app_state.config.providers["alpha"].total_limits = LimitsConfig(requests_per_minute=100)
    limit_state = app_state.limits.provider("alpha", app_state.config.providers["alpha"].total_limits)
    for _ in range(100):
        limit_state.record_request_start()
    resolution = app_state.scheduler.router.resolve("model-a")
    providers = {c.provider_id for c in resolution.candidates}
    assert "alpha" not in providers


SMART = """
routing: {{default_key_selection: {routing}}}
providers:
  solo:
    base_url: "http://solo.test/v1"
    keys:
      - {{name: A, value: "va"}}
      - {{name: B, value: "vb"}}
    models: [{{id: m}}]
    limits: {{requests_per_minute: 10}}
    {extra}
"""


def _smart_state(tmp_path, routing: str = "smart", extra: str = "") -> AppState:
    from tests.conftest import make_config

    return AppState.build(make_config(SMART.format(routing=routing, extra=extra)))


def _first_key(state: AppState) -> str:
    return state.scheduler.router.resolve("m").candidates[0].key.name


def _use(state: AppState, key: str, n: int) -> None:
    from app.core.limits import effective_key_limits

    provider = state.config.providers["solo"]
    cfg = effective_key_limits(provider, next(k for k in provider.keys if k.name == key))
    for _ in range(n):
        state.limits.key("solo", key, cfg).record_request_start()


def test_smart_prefers_key_with_most_headroom(tmp_path):
    state = _smart_state(tmp_path)
    _use(state, "A", 8)
    _use(state, "B", 2)
    assert _first_key(state) == "B"
    assert _first_key(state) == "B"


def test_smart_uses_upstream_remaining(tmp_path):
    from app.core.ratelimit_headers import RateLimitInfo

    state = _smart_state(tmp_path)
    state.health.key("solo", "A").record_rate_limit(RateLimitInfo(10, None, None))
    state.health.key("solo", "A").record_rate_limit(RateLimitInfo(1, None, None))
    state.health.key("solo", "B").record_rate_limit(RateLimitInfo(10, None, None))
    state.health.key("solo", "B").record_rate_limit(RateLimitInfo(9, None, None))
    assert _first_key(state) == "B"


def test_smart_ties_round_robin(tmp_path):
    state = _smart_state(tmp_path)
    assert [_first_key(state) for _ in range(4)] == ["A", "B", "A", "B"]


def test_provider_inherits_routing_default(tmp_path):
    assert _smart_state(tmp_path).config.providers["solo"].key_selection is None
    assert _smart_state(tmp_path).config.routing.default_key_selection == "smart"
    state = _smart_state(tmp_path, routing="priority")
    assert [_first_key(state) for _ in range(3)] == ["A", "A", "A"]
    explicit = _smart_state(tmp_path, routing="priority", extra="key_selection: round_robin")
    assert [_first_key(explicit) for _ in range(2)] == ["A", "B"]


def test_routing_default_is_smart():
    from app.models.provider import RoutingConfig

    assert RoutingConfig().default_key_selection == "smart"
