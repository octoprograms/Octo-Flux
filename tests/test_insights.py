from __future__ import annotations

import json

import respx
from fastapi.testclient import TestClient

from app.core.app_state import AppState
from app.core.request_log import RequestLog, RequestRecord
from app.core.timeseries import LatencyHistogram, MinuteSeries
from tests.conftest import ADMIN
from tests.mock_provider import error_response, ok_response


def _client(app_state: AppState) -> TestClient:
    from app.main import create_app

    app = create_app()
    app.state.OctoFlux = app_state
    return TestClient(app)


def _record(i: int) -> RequestRecord:
    return RequestRecord(
        id=f"r{i}", started_at=float(i), requested_model="m", upstream_model="m", provider="p", key="k",
        status=200, latency_ms=1.0, input_tokens=1, output_tokens=1, stream=False, attempts=[],
    )


def test_request_log_bounded_200():
    log = RequestLog()
    for i in range(250):
        log.add(_record(i))
    items = log.list()
    assert len(items) == 200
    assert items[0]["id"] == "r249"  # newest first
    assert log.get("r10") is None
    assert log.get("r249")["status"] == 200


@respx.mock
def test_request_log_has_no_message_content(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response("REPLY-TEXT"))
    client = _client(app_state)
    client.post("/v1/chat/completions", json={"model": "model-a", "messages": [{"role": "user", "content": "TOP-SECRET-PROMPT"}]})
    body = client.get("/admin/requests", headers=ADMIN).json()
    assert len(body["requests"]) == 1
    dumped = json.dumps(body)
    assert "TOP-SECRET-PROMPT" not in dumped
    assert "REPLY-TEXT" not in dumped
    record = body["requests"][0]
    assert (record["requested_model"], record["provider"], record["status"]) == ("model-a", "alpha", 200)
    assert (record["input_tokens"], record["output_tokens"]) == (5, 3)


@respx.mock
def test_request_detail_includes_attempt_trace(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(500, "boom"))
    respx.post("http://beta.test/v1/chat/completions").mock(return_value=ok_response("beta"))
    client = _client(app_state)
    client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    request_id = client.get("/admin/requests", headers=ADMIN).json()["requests"][0]["id"]

    detail = client.get(f"/admin/requests/{request_id}", headers=ADMIN).json()
    assert [a["outcome"] for a in detail["attempts"]][-1] == "success"
    assert len(detail["attempts"]) >= 2
    assert detail["attempts"][0]["provider"] == "alpha"
    assert client.get("/admin/requests/nope", headers=ADMIN).status_code == 404


@respx.mock
def test_failed_request_is_logged(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=error_response(400, "bad"))
    client = _client(app_state)
    client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    record = client.get("/admin/requests", headers=ADMIN).json()["requests"][0]
    assert record["status"] == 400
    assert record["provider"] is None


def test_timeseries_counts_per_minute():
    series = MinuteSeries()
    series.record("p", True, 10, now=120.0)
    series.record("p", False, 0, now=125.0)
    series.record("p", True, 5, now=185.0)
    assert series.series("p", minutes=3, now=190.0) == [[0, 0, 0], [2, 1, 10], [1, 0, 5]]
    assert series.series("p", minutes=2, now=120.0 + 1440 * 60) == [[0, 0, 0], [0, 0, 0]]
    restored = MinuteSeries()
    restored.load(series.dump())
    assert restored.series("p", minutes=3, now=190.0) == [[0, 0, 0], [2, 1, 10], [1, 0, 5]]


def test_latency_percentiles():
    hist = LatencyHistogram()
    for _ in range(90):
        hist.observe("p", 80.0)
    for _ in range(10):
        hist.observe("p", 3000.0)
    pct = hist.percentiles("p")
    assert pct["p50"] == 100
    assert pct["p90"] == 100
    assert pct["p95"] == 3200
    assert pct["p99"] == 3200
    assert hist.percentiles("unknown") == {"p50": None, "p75": None, "p90": None, "p95": None, "p99": None}


@respx.mock
def test_timeseries_and_latency_endpoints(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response())
    client = _client(app_state)
    client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    ts = client.get("/admin/timeseries?minutes=5", headers=ADMIN).json()
    assert ts["providers"]["alpha"][-1] == [1, 0, 8]
    assert len(ts["providers"]["alpha"]) == 5
    latency = client.get("/admin/latency", headers=ADMIN).json()
    assert latency["providers"]["alpha"]["p50"] is not None
    assert latency["all"]["p50"] is not None


@respx.mock
def test_playground_returns_record(app_state: AppState):
    respx.post("http://alpha.test/v1/chat/completions").mock(return_value=ok_response("played"))
    client = _client(app_state)
    resp = client.post(
        "/admin/playground", headers=ADMIN,
        json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["response"]["choices"][0]["message"]["content"] == "played"
    assert body["record"]["provider"] == "alpha"
    assert body["record"]["stream"] is False
    assert client.post("/admin/playground", json={"model": "model-a", "messages": []}).status_code == 401
