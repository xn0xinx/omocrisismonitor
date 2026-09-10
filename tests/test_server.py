import pytest
from fastapi.testclient import TestClient

from omocrisismonitor import config
from omocrisismonitor.server import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    # never let the AIS service open a real socket during a test, whatever
    # viewport an endpoint test throws at it.
    async def _no_net(self, url):  # noqa: ANN001
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(
        "omocrisismonitor.ais.AisService._default_connector", _no_net
    )

    cfg = config.load(tmp_path / "none.toml")
    cfg.map.maptiler_key = "TESTKEY"
    with TestClient(create_app(cfg)) as c:
        yield c


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "OmoCrisisMonitor" in r.text or "OMO" in r.text


def test_health(client):
    j = client.get("/api/health").json()
    assert j["ok"] is True and "version" in j


def test_bootstrap_shape(client):
    j = client.get("/api/bootstrap").json()
    assert j["map"]["maptiler_key"] == "TESTKEY"
    assert len(j["map"]["center"]) == 2
    assert set(j["layers"]) == {"ais", "conflict", "fuel", "ai", "alerts"}
    # phase 1: AIS is live, everything after it is still gated off
    assert j["layers"]["ais"] is True
    assert all(v is False for k, v in j["layers"].items() if k != "ais")
    assert j["ais"]["throttle_ms"] > 0 and j["ais"]["stale_after_s"] > 0


def test_view_accepts_bbox(client):
    r = client.post("/api/view", json={"bbox": [[10.0, 5.0], [14.0, 9.0]]})
    assert r.status_code == 200 and r.json()["ok"] is True
    # a usable viewport reports the transition synchronously
    assert r.json()["state"] == "connecting"


def test_view_span_guard_reports_zoomed_out(client):
    # a near-global box is bigger than the potato guard allows → no subscription
    r = client.post("/api/view", json={"bbox": [[-80.0, -170.0], [80.0, 170.0]]})
    assert r.status_code == 200
    assert r.json()["state"] == "zoomed_out"


def test_view_null_bbox_ok(client):
    r = client.post("/api/view", json={"bbox": None})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_view_rejects_malformed_bbox(client):
    r = client.post("/api/view", json={"bbox": [1, 2, 3]})
    assert r.status_code == 422


def test_vessel_unknown_is_404(client):
    assert client.get("/api/vessel/999000111").status_code == 404


def test_theme_css_falls_back_to_bundled(client):
    r = client.get("/theme.css")
    assert r.status_code == 200
    assert "--ocm-" in r.text


def test_ws_snapshot_then_ping(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert "version" in first
