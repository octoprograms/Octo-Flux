from __future__ import annotations

import time

import httpx
import pytest
import respx

from app.core.app_state import AppState
from app.core.model_stats import ModelStats, ModelStatsRegistry, score
from tests.conftest import make_config
from tests.test_openrouter_auto import client_for, free_model, mock_discovery, openrouter_config

ADMIN = {"Authorization": "Bearer test-admin"}


def _with_admin(config):
    from app.models.provider import AdminKeyConfig

    config.server.admin_keys = [AdminKeyConfig(name="test", value="test-admin")]
    return config


async def _discovered_order(state: AppState) -> list[str]:
    resolution = await state.scheduler.router.resolve_dynamic("auto", payload={"messages": []})
    return [c.model_id for c in resolution.candidates if c.reason.startswith("dynamically")]


@pytest.mark.asyncio
@respx.mock
async def test_failures_push_model_down():
    mock_discovery(free_model("a/one:free"), free_model("b/two:free"))
    state = AppState.build(openrouter_config(keys=1))
    for _ in range(2):
        state.health.model_stats.get("openrouter", "a/one:free").record(False, 100.0, time.time())
    assert await _discovered_order(state) == ["b/two:free", "a/one:free"]


@pytest.mark.asyncio
@respx.mock
async def test_successes_pull_model_up():
    mock_discovery(free_model("a/one:free"), free_model("b/two:free"))
    state = AppState.build(openrouter_config(keys=1))
    state.health.model_stats.get("openrouter", "b/two:free").record(True, 100.0, time.time())
    assert await _discovered_order(state) == ["b/two:free", "a/one:free"]


def test_score_formula_caps():
    now = time.time()
    stats = ModelStats()
    for _ in range(9):
        stats.record(True, 1000.0, now)
    assert score(4, stats, now) == 4 - 15
    bad = ModelStats()
    for _ in range(9):
        bad.record(False, 1000.0, now)
    assert score(0, bad, now) == 25


def test_slow_model_penalised():
    now = time.time()
    stats = ModelStats()
    stats.record(True, 16000.0, now)
    assert stats.p50_ms(now) == 16000.0
    assert score(0, stats, now) == -3 + 3


@pytest.mark.asyncio
@respx.mock
async def test_pinned_first_in_pin_order():
    mock_discovery(free_model("a/one:free"), free_model("b/two:free"), free_model("c/three:free"))
    config = openrouter_config(keys=1)
    config.providers["openrouter"].dynamic_models.pinned = ["c/three:free", "b/two:free"]
    state = AppState.build(config)
    assert await _discovered_order(state) == ["c/three:free", "b/two:free", "a/one:free"]


STALE = """
providers:
  openrouter:
    base_url: http://openrouter.test/v1
    keys: [{name: k, value: v}]
    models:
      - {id: "old/gone:free", priority: 10}
      - {id: "liquid/embed:free", capabilities: [embeddings]}
    dynamic_models:
      enabled: true
      endpoint: http://openrouter.test/v1/models?dynamic=1
"""


@pytest.mark.asyncio
@respx.mock
async def test_stale_free_model_skipped_after_discovery():
    mock_discovery(free_model("new/model:free"))
    state = AppState.build(make_config(STALE))
    await state.scheduler.router.resolve_dynamic("auto", payload={"messages": []})

    resolution = state.scheduler.router.resolve("old/gone:free")
    assert resolution.candidates == []
    assert any("no longer listed as free upstream" in reason for reason in resolution.excluded)
    # Embedding models aren't in the text-only discovery list; they must not be flagged.
    assert state.scheduler.router.resolve("liquid/embed:free", capability="embeddings").candidates


@pytest.mark.asyncio
@respx.mock
async def test_stale_not_applied_before_first_discovery():
    respx.get("http://openrouter.test/v1/models?dynamic=1").mock(return_value=httpx.Response(500))
    state = AppState.build(make_config(STALE))
    await state.scheduler.router.resolve_dynamic("auto", payload={"messages": []})
    assert state.scheduler.router.resolve("old/gone:free").candidates


def test_idle_stats_evicted_after_24h():
    registry = ModelStatsRegistry()
    registry.get("openrouter", "a").record(True, 10.0, 1000.0)
    registry.get("openrouter", "b").record(True, 10.0, 1000.0 + 80000)
    assert registry.evict_idle(1000.0 + 86401) == 1
    assert registry.get("openrouter", "a").successes_24h(1000.0 + 86401) == 0
    assert registry.get("openrouter", "b").successes_24h(1000.0 + 86401) == 1


def test_stats_window_is_24h():
    stats = ModelStats()
    stats.record(False, 10.0, 0.0)
    stats.record(False, 10.0, 3600.0 * 10)
    assert stats.failures_24h(3600.0 * 10) == 2
    assert stats.failures_24h(3600.0 * 25) == 1
    assert stats.failures_24h(3600.0 * 40) == 0


@respx.mock
def test_free_models_endpoint_shape():
    mock_discovery(free_model("a/one:free"), free_model("acme/guard:free"), free_model("b/blocked:free"))
    config = _with_admin(openrouter_config(keys=1))
    config.providers["openrouter"].dynamic_models.blocked = ["b/blocked:free"]
    client = client_for(AppState.build(config))

    assert client.get("/admin/free-models").status_code == 401
    body = client.get("/admin/free-models", headers=ADMIN).json()

    assert body["provider"] == "openrouter"
    assert isinstance(body["discovered_at"], float)
    assert body["stale"] == []
    models = {m["id"]: m for m in body["models"]}
    assert set(models["a/one:free"]) >= {
        "id", "rank", "score", "position", "context_length", "tools",
        "successes_24h", "failures_24h", "p50_ms", "pinned", "blocked", "rejected_reason",
    }
    assert models["a/one:free"]["rank"] == 1
    assert models["acme/guard:free"]["rank"] is None
    assert models["acme/guard:free"]["rejected_reason"] == "not a chat model"
    assert models["b/blocked:free"]["blocked"] is True


@respx.mock
def test_free_models_endpoint_when_discovery_down():
    respx.get("http://openrouter.test/v1/models?dynamic=1").mock(return_value=httpx.Response(500))
    client = client_for(AppState.build(_with_admin(openrouter_config(keys=1))))
    body = client.get("/admin/free-models", headers=ADMIN).json()
    assert body["discovered_at"] is None
    assert body["models"] == []
    assert body["stale"] == []
