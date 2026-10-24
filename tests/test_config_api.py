from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.app_state import AppState
from app.core.config import load_config

ADMIN = {"Authorization": "Bearer admin-secret-0001"}

CONFIG = textwrap.dedent(
    """
    server:
      require_auth: true
      client_keys: ["client-secret-9999"]
      admin_keys:
        - {name: owner, value: "admin-secret-0001"}
    providers:
      alpha:
        base_url: "http://alpha.test/v1"
        keys:
          - {name: a, value: "sk-abcdefgh1234"}
          - {name: env, value: "${GROQ_KEY}"}
          - {name: prefixed, value: "Bearer ${X}"}
          - {name: defaulted, value: "${Y:-dev}"}
        models:
          - {id: model-a}
          - {id: model-b}
    """
)


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GROQ_KEY", "gsk-from-env-5678")
    monkeypatch.setenv("X", "xval")
    path = tmp_path / "config" / "OctoFlux.yaml"
    path.parent.mkdir()
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("OctoFlux_CONFIG", str(path))
    return path


@pytest.fixture
def client(cfg_path: Path) -> TestClient:
    from app.main import create_app

    app = create_app()
    app.state.OctoFlux = AppState.build(load_config(cfg_path))
    return TestClient(app)


def _file(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _put(client: TestClient, version: str, config: dict):
    return client.put("/admin/config", json={"version": version, "config": config}, headers=ADMIN)


def test_get_masks_literal_secrets(client):
    body = client.get("/admin/config", headers=ADMIN).json()
    keys = body["config"]["providers"]["alpha"]["keys"]
    assert keys[0]["value"] == "•••• 1234"
    assert keys[1]["value"] == "${GROQ_KEY}"
    assert body["config"]["server"]["client_keys"] == ["•••• 9999"]
    assert body["config"]["server"]["admin_keys"][0]["value"] == "•••• 0001"
    assert len(body["version"]) == 64


def test_get_requires_admin(client):
    assert client.get("/admin/config").status_code == 401


def test_put_round_trip_keeps_real_secret(client, cfg_path):
    body = client.get("/admin/config", headers=ADMIN).json()
    body["config"]["providers"]["alpha"]["models"][0]["enabled"] = False
    resp = _put(client, body["version"], body["config"])
    assert resp.status_code == 200, resp.text
    saved = _file(cfg_path)
    assert saved["providers"]["alpha"]["keys"][0]["value"] == "sk-abcdefgh1234"
    assert saved["providers"]["alpha"]["models"][0]["enabled"] is False
    assert saved["server"]["client_keys"] == ["client-secret-9999"]
    assert saved["server"]["admin_keys"][0]["value"] == "admin-secret-0001"
    assert resp.json()["version"] != body["version"]


def test_put_preserves_env_refs_verbatim(client, cfg_path):
    body = client.get("/admin/config", headers=ADMIN).json()
    assert _put(client, body["version"], body["config"]).status_code == 200
    keys = _file(cfg_path)["providers"]["alpha"]["keys"]
    assert keys[1]["value"] == "${GROQ_KEY}"
    assert keys[2]["value"] == "Bearer ${X}"
    assert keys[3]["value"] == "${Y:-dev}"


def test_put_renamed_key_keeps_value(client, cfg_path):
    body = client.get("/admin/config", headers=ADMIN).json()
    body["config"]["providers"]["alpha"]["keys"][0]["name"] = "b"
    assert _put(client, body["version"], body["config"]).status_code == 200
    key = _file(cfg_path)["providers"]["alpha"]["keys"][0]
    assert key == {"name": "b", "value": "sk-abcdefgh1234"}


def test_put_ambiguous_mask_422(client, cfg_path):
    raw = _file(cfg_path)
    raw["providers"]["alpha"]["keys"].append({"name": "twin", "value": "sk-zzzzzzzz1234"})
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    body = client.get("/admin/config", headers=ADMIN).json()
    body["config"]["providers"]["alpha"]["keys"][0]["name"] = "renamed"
    resp = _put(client, body["version"], body["config"])
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "unknown_mask"


def test_put_stale_version_409(client, cfg_path):
    body = client.get("/admin/config", headers=ADMIN).json()
    cfg_path.write_text(CONFIG + "\n# edited elsewhere\n", encoding="utf-8")
    before = cfg_path.read_bytes()
    resp = _put(client, body["version"], body["config"])
    assert resp.status_code == 409
    assert resp.json()["error"]["type"] == "config_conflict"
    assert cfg_path.read_bytes() == before


def test_put_invalid_config_422(client, cfg_path):
    body = client.get("/admin/config", headers=ADMIN).json()
    body["config"]["providers"]["alpha"]["base_url"] = "ftp://x"
    resp = _put(client, body["version"], body["config"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["type"] == "invalid_config"
    assert "base_url" in error["errors"][0]["loc"]


def test_put_without_admin_keys_422(client):
    body = client.get("/admin/config", headers=ADMIN).json()
    body["config"]["server"]["admin_keys"] = []
    resp = _put(client, body["version"], body["config"])
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "would_lock_out"


def test_put_writes_backup_and_prunes(client, cfg_path):
    for i in range(22):
        body = client.get("/admin/config", headers=ADMIN).json()
        body["config"]["providers"]["alpha"]["models"][0]["priority"] = i
        assert _put(client, body["version"], body["config"]).status_code == 200
    backups = list((cfg_path.parent / "backups").glob("OctoFlux.*.yaml"))
    assert len(backups) == 20


def test_put_hot_reloads(client):
    body = client.get("/admin/config", headers=ADMIN).json()
    body["config"]["providers"]["alpha"]["enabled"] = False
    assert _put(client, body["version"], body["config"]).status_code == 200
    status = client.get("/admin/status", headers=ADMIN).json()
    assert status["providers"]["alpha"]["enabled"] is False


def _save_once(client, priority: int) -> None:
    body = client.get("/admin/config", headers=ADMIN).json()
    body["config"]["providers"]["alpha"]["models"][0]["priority"] = priority
    assert _put(client, body["version"], body["config"]).status_code == 200


def test_list_backups_newest_first(client, cfg_path):
    _save_once(client, 1)
    _save_once(client, 2)
    backups = client.get("/admin/config/backups", headers=ADMIN).json()["backups"]
    assert len(backups) == 2
    assert set(backups[0]) == {"name", "created_at", "size"}
    # Newest backup = the file as it was before the second save (priority 1).
    assert "priority: 1" in (cfg_path.parent / "backups" / backups[0]["name"]).read_text(encoding="utf-8")
    assert "priority: 1" not in (cfg_path.parent / "backups" / backups[1]["name"]).read_text(encoding="utf-8")


def test_restore_backup_applies_and_backs_up_current(client, cfg_path):
    _save_once(client, 7)
    oldest = client.get("/admin/config/backups", headers=ADMIN).json()["backups"][-1]["name"]
    backup_bytes = (cfg_path.parent / "backups" / oldest).read_bytes()
    resp = client.post(f"/admin/config/backups/{oldest}/restore", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert cfg_path.read_bytes() == backup_bytes
    backups = client.get("/admin/config/backups", headers=ADMIN).json()["backups"]
    assert len(backups) == 2
    assert "priority: 7" in (cfg_path.parent / "backups" / backups[0]["name"]).read_text(encoding="utf-8")


def test_restore_unknown_name_404(client):
    resp = client.post("/admin/config/backups/..%2FOctoFlux.yaml/restore", headers=ADMIN)
    assert resp.status_code == 404
    resp = client.post("/admin/config/backups/OctoFlux.20990101T000000Z.yaml/restore", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "backup_not_found"


def test_restore_with_unset_env_var_422(client, cfg_path):
    _save_once(client, 3)
    name = client.get("/admin/config/backups", headers=ADMIN).json()["backups"][0]["name"]
    backup = cfg_path.parent / "backups" / name
    backup.write_text(backup.read_text(encoding="utf-8").replace("${GROQ_KEY}", "${MISSING}"), encoding="utf-8")
    before = cfg_path.read_bytes()
    running = client.app.state.OctoFlux.config
    resp = client.post(f"/admin/config/backups/{name}/restore", headers=ADMIN)
    assert resp.status_code == 422
    assert cfg_path.read_bytes() == before
    assert client.app.state.OctoFlux.config is running
