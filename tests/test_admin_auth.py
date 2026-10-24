from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.app_state import AppState
from tests.conftest import make_config

BASE = """
server:
  require_auth: {require_auth}
  client_keys: ["client-1"]
  {admin}
providers:
  alpha:
    base_url: "http://alpha.test/v1"
    keys: [{{name: a1, value: "k1"}}]
    models: [{{id: model-a}}]
"""
WITH_ADMIN = 'admin_keys: [{name: noure, value: "admin-1"}]'


def _client(require_auth: str = "true", admin: str = WITH_ADMIN) -> TestClient:
    from app.main import create_app

    app = create_app()
    app.state.OctoFlux = AppState.build(make_config(BASE.format(require_auth=require_auth, admin=admin)))
    return TestClient(app)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_key_opens_admin_routes():
    assert _client().get("/admin/status", headers=_bearer("admin-1")).status_code == 200


def test_client_key_rejected_on_admin_routes():
    assert _client().get("/admin/status", headers=_bearer("client-1")).status_code == 401


def test_admin_key_rejected_on_v1_routes():
    assert _client().get("/v1/models", headers=_bearer("admin-1")).status_code == 401


def test_admin_disabled_without_admin_keys():
    resp = _client(admin="").get("/admin/status", headers=_bearer("admin-1"))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "admin_disabled"


def test_public_health_is_minimal():
    assert _client().get("/health").json() == {"status": "ok"}


def test_admin_health_has_details():
    resp = _client().get("/admin/health", headers=_bearer("admin-1"))
    assert resp.status_code == 200
    assert "providers" in resp.json()


def test_require_auth_false_does_not_open_admin():
    assert _client(require_auth="false").get("/admin/status").status_code == 401


def test_admin_status_reports_upstream_remaining():
    from app.core.ratelimit_headers import RateLimitInfo

    client = _client()
    client.app.state.OctoFlux.health.key("alpha", "a1").record_rate_limit(RateLimitInfo(7, 900, 1_900_000_000.0))
    key = client.get("/admin/status", headers=_bearer("admin-1")).json()["providers"]["alpha"]["keys"]["a1"]
    assert (key["remaining_requests"], key["remaining_tokens"], key["resets_at"]) == (7, 900, 1_900_000_000.0)
