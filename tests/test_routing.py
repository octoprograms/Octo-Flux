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
    limit_state = app_state.limits.provider("alpha", app_state.config.providers["alpha"].limits)
    # No RPM configured in fixture for alpha? it is 100; force exhaustion manually.
    for _ in range(100):
        limit_state.record_request_start()
    resolution = app_state.scheduler.router.resolve("model-a")
    providers = {c.provider_id for c in resolution.candidates}
    assert "alpha" not in providers
