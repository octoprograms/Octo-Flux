from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.core.config import ConfigError, load_config, load_yaml_text
from app.core.usage import UsageRegistry
from app.models.provider import OctoFluxConfig
from tests.conftest import make_config


def test_valid_config_loads(two_provider_config):
    assert "alpha" in two_provider_config.providers
    assert two_provider_config.providers["alpha"].priority == 10


def test_env_var_resolution(monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "secret-value")
    cfg = make_config(
        """
        providers:
          p1:
            base_url: "http://p1.test/v1"
            keys: [{name: k1, value: "${MY_TEST_KEY}"}]
            models: [{id: m1}]
        """
    )
    assert cfg.providers["p1"].keys[0].value == "secret-value"


def test_env_var_default_used_when_unset(monkeypatch):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    cfg = make_config(
        """
        providers:
          p1:
            base_url: "http://p1.test/v1"
            keys: [{name: k1, value: "${UNSET_VAR:-fallback}"}]
            models: [{id: m1}]
        """
    )
    assert cfg.providers["p1"].keys[0].value == "fallback"


def test_missing_required_env_var_raises():
    from app.core.config import load_config_from_env, ConfigError as CE
    from app.core.config import _resolve_env

    with pytest.raises(Exception):
        _resolve_env("${TOTALLY_UNSET_VAR_XYZ}")


def test_provider_without_enabled_keys_rejected():
    with pytest.raises(Exception):
        make_config(
            """
            providers:
              p1:
                base_url: "http://p1.test/v1"
                keys: []
                models: [{id: m1}]
            """
        )


def test_provider_without_enabled_models_rejected():
    with pytest.raises(Exception):
        make_config(
            """
            providers:
              p1:
                base_url: "http://p1.test/v1"
                keys: [{name: k1, value: v1}]
                models: []
            """
        )


def test_duplicate_key_names_rejected():
    with pytest.raises(Exception):
        make_config(
            """
            providers:
              p1:
                base_url: "http://p1.test/v1"
                keys:
                  - {name: dup, value: v1}
                  - {name: dup, value: v2}
                models: [{id: m1}]
            """
        )


def test_invalid_base_url_rejected():
    with pytest.raises(Exception):
        make_config(
            """
            providers:
              p1:
                base_url: "not-a-url"
                keys: [{name: k1, value: v1}]
                models: [{id: m1}]
            """
        )


def test_alias_referencing_unknown_provider_rejected():
    with pytest.raises(Exception):
        make_config(
            """
            providers:
              p1:
                base_url: "http://p1.test/v1"
                keys: [{name: k1, value: v1}]
                models: [{id: m1}]
            aliases:
              broken:
                - {provider: nonexistent, model: m1}
            """
        )


def test_invalid_limit_rejected():
    with pytest.raises(Exception):
        make_config(
            """
            providers:
              p1:
                base_url: "http://p1.test/v1"
                keys: [{name: k1, value: v1}]
                models: [{id: m1}]
                limits: {requests_per_minute: -5}
            """
        )


def test_missing_config_file_raises():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/path/OctoFlux.yaml")


def test_usage_registry_restores_counters():
    db_path = Path(__file__).with_name(f".usage-{uuid.uuid4().hex}.db")
    try:
        first = UsageRegistry(db_path=str(db_path))
        first.record_attempt("alpha", "model-a", "key-1", True, 12.5)
        first.record_tokens("alpha", "model-a", "key-1", 3, 5)
        first.record_fallback()
        first.close()

        second = UsageRegistry(db_path=str(db_path))
        global_usage = second.snapshot()["global"]

        assert global_usage["requests"] == 1
        assert global_usage["successful_requests"] == 1
        assert global_usage["tokens"] == 8
        assert global_usage["fallback_count"] == 1
        second.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
