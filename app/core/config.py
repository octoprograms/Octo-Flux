"""Configuration loading.

Loads YAML, resolves ${VAR} / ${VAR:-default} against the process
environment, and validates via Pydantic (app.models.provider). Fails fast
with a specific, actionable error message rather than starting with a
silently broken configuration.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.models.provider import OctoProxyConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")


class ConfigError(Exception):
    """Raised for any configuration problem. Message is meant to be shown to the operator."""


def _resolve_env(value: str) -> str:
    def repl(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(3)
        if var_name in os.environ:
            return os.environ[var_name]
        if default is not None:
            return default
        raise ConfigError(
            f"environment variable '{var_name}' referenced in config is not set "
            "and no default was provided (use ${VAR:-default} for optional values)"
        )

    return _ENV_PATTERN.sub(repl, value)


def _resolve_env_recursive(node: object) -> object:
    if isinstance(node, str):
        return _resolve_env(node) if "${" in node else node
    if isinstance(node, dict):
        return {k: _resolve_env_recursive(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_env_recursive(v) for v in node]
    return node


def load_yaml_text(text: str) -> dict:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("top-level configuration must be a mapping")
    return raw


def load_config(path: str | Path) -> OctoProxyConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"configuration file not found: {p}")
    raw = load_yaml_text(p.read_text())
    resolved = _resolve_env_recursive(raw)

    # Fill provider.id from the mapping key if not explicitly given, so
    # authors don't have to repeat it.
    providers = resolved.get("providers") or {}
    for key, value in providers.items():
        if isinstance(value, dict) and "id" not in value:
            value["id"] = key

    try:
        return OctoProxyConfig.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Configuration error:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config_from_env(default_path: str = "config/octoproxy.yaml") -> OctoProxyConfig:
    path = os.environ.get("OCTOPROXY_CONFIG", default_path)
    return load_config(path)
