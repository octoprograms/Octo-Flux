from __future__ import annotations

import os
import textwrap

import pytest

from app.core.app_state import AppState
from app.core.config import _resolve_env_recursive, load_config, load_yaml_text
from app.models.provider import OctoProxyConfig


def make_config(yaml_text: str) -> OctoProxyConfig:
    raw = load_yaml_text(textwrap.dedent(yaml_text))
    resolved = _resolve_env_recursive(raw)
    for pid, pval in (resolved.get("providers") or {}).items():
        pval.setdefault("id", pid)
    return OctoProxyConfig.model_validate(resolved)


@pytest.fixture
def two_provider_config() -> OctoProxyConfig:
    return make_config(
        """
        server:
          require_auth: false
        routing:
          max_total_attempts: 6
        providers:
          alpha:
            base_url: "http://alpha.test/v1"
            priority: 10
            keys:
              - {name: alpha-key-1, value: "k1"}
              - {name: alpha-key-2, value: "k2"}
            models:
              - {id: model-a, priority: 10}
            limits: {requests_per_minute: 100}
            retry: {base_delay_ms: 1, max_delay_ms: 5, jitter_ms: 1}
            health: {failure_threshold: 2, cooldown_seconds: 30}
          beta:
            base_url: "http://beta.test/v1"
            priority: 20
            keys:
              - {name: beta-key-1, value: "k3"}
            models:
              - {id: model-a, priority: 10}
              - {id: model-b, priority: 20}
            limits: {requests_per_minute: 100}
            retry: {base_delay_ms: 1, max_delay_ms: 5, jitter_ms: 1}
            health: {failure_threshold: 2, cooldown_seconds: 30}
        aliases:
          fast:
            - {provider: alpha, model: model-a}
            - {provider: beta, model: model-a}
        """
    )


@pytest.fixture
def app_state(two_provider_config) -> AppState:
    return AppState.build(two_provider_config)
