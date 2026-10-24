"""Read, mask, validate and write the raw YAML config for the dashboard.

Works on the file *before* ${VAR} resolution so values that come from the
environment are never written to disk.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.core.config import ConfigError, _resolve_env_recursive, load_yaml_text
from app.models.provider import OctoFluxConfig

MASK_PREFIX = "••••"
KEEP_BACKUPS = 20
_BACKUP_RE = re.compile(r"^OctoFlux\.(\d{8}T\d{6}Z)(?:\.(\d+))?\.yaml$")


class ConfigStoreError(Exception):
    def __init__(self, status: int, type_: str, message: str, errors: list | None = None):
        super().__init__(message)
        self.status = status
        self.type = type_
        self.message = message
        self.errors = errors


def config_path() -> Path:
    return Path(os.environ.get("OctoFlux_CONFIG", "config/OctoFlux.yaml"))


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_raw(path: Path) -> tuple[dict, str]:
    data = path.read_bytes()
    try:
        return load_yaml_text(data.decode("utf-8")), _version(data)
    except ConfigError as exc:
        raise ConfigStoreError(422, "invalid_config", str(exc)) from exc


def _mask(value: str) -> str:
    if "${" in value:
        return value
    return MASK_PREFIX if len(value) <= 8 else f"{MASK_PREFIX} {value[-4:]}"


def _is_literal(value: object) -> bool:
    return isinstance(value, str) and "${" not in value


def mask_secrets(raw: dict) -> dict:
    out = copy.deepcopy(raw)
    server = out.get("server") or {}
    if isinstance(server.get("client_keys"), list):
        server["client_keys"] = [_mask(v) if isinstance(v, str) else v for v in server["client_keys"]]
    for key in server.get("admin_keys") or []:
        if isinstance(key, dict) and isinstance(key.get("value"), str):
            key["value"] = _mask(key["value"])
    for provider in (out.get("providers") or {}).values():
        for key in (provider or {}).get("keys") or []:
            if isinstance(key, dict) and isinstance(key.get("value"), str):
                key["value"] = _mask(key["value"])
    return out


def _resolve_mask(masked: str, same_identity: str | None, group: list[str], where: str) -> str:
    """Map a masked value back to the stored secret: same identity first, then a unique match in its group."""
    if same_identity is not None and _is_literal(same_identity) and _mask(same_identity) == masked:
        return same_identity
    matches = [v for v in group if _is_literal(v) and _mask(v) == masked]
    if len(matches) == 1:
        return matches[0]
    raise ConfigStoreError(422, "unknown_mask", f"{where}: masked value does not match a stored key — re-enter the key.")


def _is_mask(value: object) -> bool:
    return isinstance(value, str) and value.startswith(MASK_PREFIX)


def unmask_secrets(new: dict, old: dict) -> dict:
    out = copy.deepcopy(new)
    old_server = old.get("server") or {}
    server = out.get("server") or {}

    old_clients = [v for v in old_server.get("client_keys") or [] if isinstance(v, str)]
    clients = server.get("client_keys")
    if isinstance(clients, list):
        # Client keys have no names: position is only trustworthy when nothing was added or removed.
        positional = len(clients) == len(old_clients)
        for i, value in enumerate(clients):
            if _is_mask(value):
                same = old_clients[i] if positional else None
                clients[i] = _resolve_mask(value, same, old_clients, f"server.client_keys[{i}]")

    old_admin = {k.get("name"): k.get("value") for k in old_server.get("admin_keys") or [] if isinstance(k, dict)}
    for i, key in enumerate(server.get("admin_keys") or []):
        if isinstance(key, dict) and _is_mask(key.get("value")):
            key["value"] = _resolve_mask(key["value"], old_admin.get(key.get("name")), list(old_admin.values()), f"server.admin_keys[{i}]")

    old_providers = old.get("providers") or {}
    for pid, provider in (out.get("providers") or {}).items():
        stored = {k.get("name"): k.get("value") for k in ((old_providers.get(pid) or {}).get("keys") or []) if isinstance(k, dict)}
        for i, key in enumerate((provider or {}).get("keys") or []):
            if isinstance(key, dict) and _is_mask(key.get("value")):
                key["value"] = _resolve_mask(key["value"], stored.get(key.get("name")), list(stored.values()), f"providers.{pid}.keys[{i}]")
    return out


def validate_raw(raw: dict) -> OctoFluxConfig:
    try:
        resolved = _resolve_env_recursive(copy.deepcopy(raw))
    except ConfigError as exc:
        raise ConfigStoreError(422, "invalid_config", str(exc)) from exc
    for pid, value in (resolved.get("providers") or {}).items():
        if isinstance(value, dict):
            value.setdefault("id", pid)
    try:
        config = OctoFluxConfig.model_validate(resolved)
    except ValidationError as exc:
        # Only loc + msg: Pydantic's "input" field could echo resolved secrets.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        raise ConfigStoreError(422, "invalid_config", "Configuration is invalid.", errors=errors) from exc
    if not config.server.admin_keys:
        raise ConfigStoreError(422, "would_lock_out", "At least one admin key is required (server.admin_keys).")
    return config


def _backup_sort_key(path: Path) -> tuple[str, int]:
    match = _BACKUP_RE.match(path.name)
    return (match.group(1), int(match.group(2) or 0)) if match else ("", 0)


def _backups(path: Path) -> list[Path]:
    folder = path.parent / "backups"
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if _BACKUP_RE.match(p.name)), key=_backup_sort_key, reverse=True)


def list_backups(path: Path) -> list[dict]:
    out = []
    for p in _backups(path):
        stamp = datetime.strptime(_backup_sort_key(p)[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        out.append({"name": p.name, "created_at": stamp.isoformat(), "size": p.stat().st_size})
    return out


def read_backup(path: Path, name: str) -> tuple[dict, bytes]:
    # The name must come from the listing; it is never used as a raw path.
    match = next((p for p in _backups(path) if p.name == name), None)
    if match is None:
        raise ConfigStoreError(404, "backup_not_found", f"No backup named {name!r}.")
    data = match.read_bytes()
    try:
        return load_yaml_text(data.decode("utf-8")), data
    except ConfigError as exc:
        raise ConfigStoreError(422, "invalid_config", str(exc)) from exc


def write_with_backup(path: Path, raw: dict, data: bytes | None = None) -> str:
    """Back up the current file, then write `data` (or `raw` dumped as YAML) atomically."""
    folder = path.parent / "backups"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target, n = folder / f"OctoFlux.{stamp}.yaml", 0
    while target.exists():
        n += 1
        target = folder / f"OctoFlux.{stamp}.{n}.yaml"
    target.write_bytes(path.read_bytes())
    for old in _backups(path)[KEEP_BACKUPS:]:
        old.unlink()

    if data is None:
        data = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return _version(data)
