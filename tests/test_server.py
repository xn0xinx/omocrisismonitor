import pytest
from fastapi.testclient import TestClient

from omocrisismonitor import config
from omocrisismonitor.conflict import _today_sort
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

    # same for the conflict service: no GeoConfirmed fetches in a route test.
    async def _cf_no_refresh(self):
        return {"conflicts": 0, "events": 0, "added": 0}

    async def _cf_no_detail(self, ext_id):
        return None

    monkeypatch.setattr(
        "omocrisismonitor.conflict.ConflictService.refresh", _cf_no_refresh
    )
    monkeypatch.setattr(
        "omocrisismonitor.conflict.ConflictService.detail", _cf_no_detail
    )

    # ...and the fuel service: no Yahoo / GlobalPetrolPrices fetches in a test.
    async def _noop(self, *a, **k):
        return None

    for m in ("_tick_crude", "_backfill_crude", "_scrape_retail"):
        monkeypatch.setattr(f"omocrisismonitor.fuel.FuelService.{m}", _noop)

    # ...and the AI sidebar: never actually shell out to `claude` in a test.
    async def _no_run(self, prompt, system):
        return "stub", {"cost_usd": 0.0}

    monkeypatch.setattr("omocrisismonitor.ai.ClaudeBackend.run", _no_run)

    # ...and alerts: never actually shell out to notify-send in a test.
    async def _no_notify(self, title, body):
        return None

    monkeypatch.setattr("omocrisismonitor.alerts.AlertService._default_notify", _no_notify)

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
    # all five phases live
    live = ("ais", "conflict", "fuel", "ai", "alerts")
    assert all(j["layers"][k] is True for k in live)
    assert set(j["layers"]) == set(live)
    assert j["ais"]["throttle_ms"] > 0 and j["ais"]["stale_after_s"] > 0
    assert j["conflict"]["window_days"] > 0 and j["conflict"]["refresh_h"] > 0
    assert j["fuel"]["unit"] and j["fuel"]["currency"]
    assert j["ai"]["model"] and j["ai"]["min_interval_s"] > 0
    assert j["ai"]["backend"] == "claude"
    keys = {b["key"] for b in j["ai"]["backends"]}
    assert keys == {"claude", "gemini"}
    assert next(b for b in j["ai"]["backends"] if b["key"] == "claude")["available"] is True
    assert next(b for b in j["ai"]["backends"] if b["key"] == "gemini")["available"] is False
    assert j["alerts"]["poll_s"] > 0


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


def test_current_view_is_layer_independent_and_feeds_ai(client):
    """/api/view/current is decoupled from the AIS toggle/subscription — it
    only feeds AiService.current_view for region-scoped synthesis."""
    box = [[10.0, 20.0], [30.0, 40.0]]
    r = client.post("/api/view/current", json={"bbox": box})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.app.state.ai.current_view == box
    assert client.app.state.ais._bbox is None      # never touches the AIS subscription

    r2 = client.post("/api/view/current", json={"bbox": None})
    assert r2.status_code == 200 and client.app.state.ai.current_view is None


def test_current_view_rejects_malformed_bbox(client):
    assert client.post("/api/view/current", json={"bbox": [1, 2, 3]}).status_code == 422


def test_vessel_unknown_is_404(client):
    assert client.get("/api/vessel/999000111").status_code == 404


def test_conflict_bootstrap_shape(client):
    j = client.get("/api/conflict/bootstrap").json()
    assert set(j) >= {"conflicts", "factions", "span", "window_days"}
    assert isinstance(j["conflicts"], list)      # empty DB in tests — just shape


def test_conflict_events_empty_featurecollection(client):
    hi, lo = _today_sort(), _today_sort() - 2   # within the 7-day hard cap
    j = client.get(f"/api/conflict/events?until={hi}&since={lo}").json()
    assert j["type"] == "FeatureCollection" and j["features"] == []
    assert j["window"] == {"since_sort": lo, "until_sort": hi}


def test_conflict_events_accepts_iso_dates(client):
    from datetime import datetime, timezone
    today = _today_sort()
    since_iso = datetime.fromtimestamp((today - 2) * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    until_iso = datetime.fromtimestamp(today * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    j = client.get(f"/api/conflict/events?since={since_iso}&until={until_iso}").json()
    assert j["window"]["since_sort"] == today - 2 and j["window"]["until_sort"] == today


def test_conflict_events_since_is_clamped_to_the_hard_cap(client):
    # requesting a since far in the past never surfaces data older than window_days
    j = client.get(f"/api/conflict/events?until={_today_sort()}&since=1").json()
    assert j["window"]["since_sort"] == _today_sort() - 7   # default conflict.window_days


def test_conflict_detail_unknown_is_404(client):
    assert client.get("/api/conflict/detail/nope-nope").status_code == 404


def test_conflict_manual_refresh(client):
    j = client.post("/api/conflict/refresh").json()
    assert j["ok"] is True and "added" in j


def test_fuel_bootstrap_shape(client):
    j = client.get("/api/fuel/bootstrap").json()
    assert set(j) >= {"crude", "source", "retail_updated", "retail_stale"}
    assert isinstance(j["crude"], dict)          # empty DB in tests


def test_fuel_crude_series_empty(client):
    j = client.get("/api/fuel/crude?symbol=brent&days=30").json()
    assert j["symbol"] == "brent" and j["series"] == []


def test_fuel_crude_rejects_bad_symbol(client):
    assert client.get("/api/fuel/crude?symbol=gold").status_code == 422


def test_fuel_retail_empty_map(client):
    j = client.get("/api/fuel/retail?kind=diesel").json()
    assert j["kind"] == "diesel" and j["prices"] == {}


def test_fuel_retail_rejects_bad_kind(client):
    assert client.get("/api/fuel/retail?kind=kerosene").status_code == 422


def test_ai_summary_empty_before_any_refresh(client):
    j = client.get("/api/ai/summary").json()
    assert j["summary"] is None and j["busy"] is False


def test_ai_refresh_is_fire_and_forget(client):
    r = client.post("/api/ai/refresh")
    assert r.status_code == 200 and r.json() == {"ok": True, "state": "thinking"}


def test_ai_set_backend(client):
    r = client.post("/api/ai/backend", json={"key": "gemini"})
    assert r.status_code == 200 and r.json() == {"ok": True, "backend": "gemini"}
    assert client.get("/api/bootstrap").json()["ai"]["backend"] == "gemini"


def test_ai_set_backend_rejects_unknown(client):
    assert client.post("/api/ai/backend", json={"key": "grok"}).status_code == 422


def test_alerts_rule_crud_roundtrip(client):
    assert client.get("/api/alerts/rules").json() == []

    r = client.post("/api/alerts/rules", json={
        "kind": "fuel_threshold", "label": "WTI spike",
        "params": {"symbol": "wti", "op": ">", "value": 100},
    })
    assert r.status_code == 200
    rule = r.json()
    assert rule["kind"] == "fuel_threshold" and rule["enabled"] is True
    assert rule["params"] == {"symbol": "wti", "op": ">", "value": 100.0}

    listed = client.get("/api/alerts/rules").json()
    assert [r["id"] for r in listed] == [rule["id"]]

    r2 = client.patch(f"/api/alerts/rules/{rule['id']}", json={"enabled": False})
    assert r2.status_code == 200 and r2.json()["enabled"] is False

    assert client.delete(f"/api/alerts/rules/{rule['id']}").json() == {"ok": True}
    assert client.get("/api/alerts/rules").json() == []


def test_alerts_create_rejects_bad_params(client):
    r = client.post("/api/alerts/rules", json={"kind": "ship_box", "params": {}})
    assert r.status_code == 422


def test_alerts_patch_and_delete_unknown_rule_404(client):
    assert client.patch("/api/alerts/rules/999", json={"enabled": True}).status_code == 404
    assert client.delete("/api/alerts/rules/999").status_code == 404


def test_alerts_hits_empty(client):
    assert client.get("/api/alerts/hits").json() == []


def test_correlate_readout_shape(client):
    j = client.get("/api/correlate?bbox=10,40,20,50&days=30").json()
    assert j["bbox"] == [[10.0, 40.0], [20.0, 50.0]] and j["days"] == 30
    assert len(j["axis"]) == len(j["series"]["conflict"]) == 31
    assert set(j["correlations"]) == {"conflict_vs_wti", "conflict_vs_brent", "conflict_vs_ships"}
    assert any("AIS data" in n for n in j["notes"])


def test_correlate_rejects_bad_bbox(client):
    assert client.get("/api/correlate?bbox=not,a,bbox").status_code == 422


def test_theme_css_falls_back_to_bundled(client):
    r = client.get("/theme.css")
    assert r.status_code == 200
    assert "--ocm-" in r.text


def test_ws_snapshot_then_ping(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert "version" in first
