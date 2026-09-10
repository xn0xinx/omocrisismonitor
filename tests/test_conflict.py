"""Conflict layer: theatre filtering, GeoJSON ingest (Point-only, recency floor,
id-keyed upsert), the trailing-window slice, faction + provenance writes, and
the detail cache. The HTTP client is a MockTransport — no network.
"""
import httpx
import pytest

from omocrisismonitor import config, db
from omocrisismonitor.conflict import ConflictService, _iso_to_sort, _today_sort


def _cfg(tmp_path):
    c = config.load(tmp_path / "none.toml")   # DEFAULTS
    return c


class FakeHub:
    def __init__(self):
        self.events = []

    def emit(self, e):
        self.events.append(e)

    def of(self, t):
        return [e for e in self.events if e["type"] == t]


NOW_SORT = _today_sort()
OLD_SORT = NOW_SORT - 5000        # far outside window*2 → dropped on ingest

CONFLICTS = [
    {"url": "ukraine", "name": "Ukraine"},
    {"url": "wwii", "name": "WWII"},                 # historical → skipped
    {"url": "secret", "name": "Secret", "isPrivate": True},   # private → skipped
    {"url": "ven", "name": "Venezuela"},
    {"name": "No slug"},                             # no url → skipped
]

def _pt(pid, lon, lat, ds, faction=1, color="#E00000"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"id": pid, "factionId": faction, "color": color,
                       "icon": "/api/icons/x/100.png", "date": "2026-09-01T00:00:00",
                       "dateSort": ds},
    }

UKR = {
    "factionMeta": [
        {"id": 1, "name": "Ukraine", "color": "#0057B7"},
        {"id": 2, "name": "Russia", "color": "#E00000"},
    ],
    "geojson": {
        "type": "FeatureCollection",
        "features": [
            _pt("u-recent-1", 30.5, 50.4, NOW_SORT - 3, faction=1),
            _pt("u-recent-2", 37.6, 47.1, NOW_SORT - 40, faction=2),
            _pt("u-old", 35.0, 48.0, OLD_SORT),                       # too old → skip
            {"type": "Feature", "geometry": {"type": "LineString",    # frontline → skip
                "coordinates": [[30, 50], [31, 51]]}, "properties": {"id": "u-line"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]},
             "properties": {"factionId": 1}},                          # no id → skip
        ],
    },
}

VEN = {
    "factionMeta": [{"id": 9, "name": "VEN", "color": "#FFCC00"}],
    "geojson": {"type": "FeatureCollection",
                "features": [_pt("v-1", -66.9, 10.5, NOW_SORT - 10, faction=9)]},
}


def _handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p == "/api/Conflict":
        return httpx.Response(200, json=CONFLICTS)
    if p == "/api/Placemark/ukraine/geojson":
        return httpx.Response(200, json=UKR)
    if p == "/api/Placemark/ven/geojson":
        return httpx.Response(200, json=VEN)
    if p.startswith("/api/Placemark/detail/"):
        pid = p.rsplit("/", 1)[1]
        if pid == "missing":
            return httpx.Response(404)
        return httpx.Response(200, json={"id": pid, "name": "01 SEP 2026",
                                         "description": "test event", "faction": "Ukraine"})
    return httpx.Response(404, text="nope")


def _svc(tmp_path, cfg=None):
    con = db.init(tmp_path / "h.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return ConflictService(cfg or _cfg(tmp_path), FakeHub(), con, client=client), con


# ---- theatre filtering ----------------------------------------------

async def test_fetch_theatres_filters_private_and_historical(tmp_path):
    svc, _ = _svc(tmp_path)
    ts = await svc._fetch_theatres()
    slugs = {t["url"] for t in ts}
    assert slugs == {"ukraine", "ven"}


async def test_fetch_theatres_honours_explicit_pin(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.conflict.theatres = ["ukraine", "wwii"]     # pin overrides the historical skip
    svc, _ = _svc(tmp_path, cfg)
    slugs = {t["url"] for t in await svc._fetch_theatres()}
    assert slugs == {"ukraine", "wwii"}


# ---- ingest / refresh ----------------------------------------------

async def test_refresh_ingests_points_within_window(tmp_path):
    svc, con = _svc(tmp_path)
    totals = await svc.refresh()
    assert totals["conflicts"] == 2
    ids = {r["ext_id"] for r in con.execute("SELECT ext_id FROM conflict_event")}
    assert ids == {"u-recent-1", "u-recent-2", "v-1"}      # old / line / no-id all dropped
    # faction palette stored per theatre
    fac = {r["conflict"] for r in con.execute("SELECT DISTINCT conflict FROM conflict_faction")}
    assert fac == {"ukraine", "ven"}
    # provenance row per theatre, no full copies
    snaps = con.execute("SELECT conflict, event_count, added FROM conflict_snapshot").fetchall()
    assert {s["conflict"] for s in snaps} == {"ukraine", "ven"}
    assert sum(s["added"] for s in snaps) == 3
    assert svc.hub.of("conflict_refresh")


async def test_refresh_is_idempotent_upsert(tmp_path):
    svc, con = _svc(tmp_path)
    await svc.refresh()
    first = con.execute("SELECT first_seen FROM conflict_event WHERE ext_id='u-recent-1'").fetchone()["first_seen"]
    await svc.refresh()                       # second pull, same ids
    rows = con.execute("SELECT COUNT(*) c FROM conflict_event").fetchone()["c"]
    assert rows == 3                          # no duplicates
    again = con.execute("SELECT first_seen, last_seen FROM conflict_event WHERE ext_id='u-recent-1'").fetchone()
    assert again["first_seen"] == first       # first_seen preserved
    assert again["last_seen"] >= first        # last_seen bumped
    added2 = con.execute("SELECT added FROM conflict_snapshot ORDER BY id DESC LIMIT 1").fetchone()["added"]
    assert added2 == 0


async def test_one_dead_theatre_does_not_sink_the_refresh(tmp_path):
    # ven returns fine, a bogus pinned slug 404s
    cfg = _cfg(tmp_path)
    cfg.conflict.theatres = ["ven", "does-not-exist"]
    svc, con = _svc(tmp_path, cfg)
    totals = await svc.refresh()
    assert totals["conflicts"] == 1
    assert [r["ext_id"] for r in con.execute("SELECT ext_id FROM conflict_event")] == ["v-1"]
    assert svc.hub.of("conflict_status")      # the failure was reported, not raised


# ---- read side --------------------------------------------------

async def test_events_geojson_trailing_window(tmp_path):
    svc, con = _svc(tmp_path)
    await svc.refresh()
    fc = svc.events_geojson()                 # defaults: [today-window, today]
    assert fc["type"] == "FeatureCollection"
    got = {f["properties"]["id"] for f in fc["features"]}
    assert got == {"u-recent-1", "u-recent-2", "v-1"}
    f0 = fc["features"][0]
    assert f0["geometry"]["type"] == "Point"
    assert {"id", "c", "f", "color", "date", "ds"} <= set(f0["properties"])

    # narrow the window to the last 5 days → only u-recent-1
    tight = svc.events_geojson(since_sort=NOW_SORT - 5, until_sort=NOW_SORT)
    assert {f["properties"]["id"] for f in tight["features"]} == {"u-recent-1"}

    # filter by theatre
    ven_only = svc.events_geojson(conflicts=["ven"])
    assert {f["properties"]["c"] for f in ven_only["features"]} == {"ven"}


async def test_bootstrap_reports_counts_and_span(tmp_path):
    svc, con = _svc(tmp_path)
    await svc.refresh()
    b = svc.bootstrap()
    counts = {c["slug"]: c["count"] for c in b["conflicts"]}
    assert counts == {"ukraine": 2, "ven": 1}
    assert b["span"]["max_sort"] == NOW_SORT - 3
    assert b["window_days"] == 90
    assert b["factions"]["ukraine"][0]["color"] == "#0057B7"


async def test_detail_fetch_and_cache(tmp_path):
    svc, con = _svc(tmp_path)
    d = await svc.detail("abc-123")
    assert d["description"] == "test event"
    assert "abc-123" in svc._detail_cache
    assert await svc.detail("missing") is None


async def test_stop_before_start_is_safe(tmp_path):
    svc, con = _svc(tmp_path)
    await svc.stop()          # never started — must not raise
