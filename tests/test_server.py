import pytest
from fastapi.testclient import TestClient

from omocrisismonitor import config
from omocrisismonitor.server import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
    assert all(v is False for v in j["layers"].values())  # phase 0: nothing on yet


def test_theme_css_falls_back_to_bundled(client):
    r = client.get("/theme.css")
    assert r.status_code == 200
    assert "--ocm-" in r.text


def test_ws_snapshot_then_ping(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert "version" in first
