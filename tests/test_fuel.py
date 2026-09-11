"""Fuel layer: the GlobalPetrolPrices parser, the Yahoo / EIA crude sources,
and FuelService's crude tick / backfill / retail scrape / STALE handling.
All HTTP goes through an httpx.MockTransport — no network.
"""
import json

import httpx
import pytest

from omocrisismonitor import config, db
from omocrisismonitor.fuel import EiaCrude, FuelService, YahooCrude, parse_gpp


def _cfg(tmp_path, **fuel):
    c = config.load(tmp_path / "none.toml")
    for k, v in fuel.items():
        setattr(c.fuel, k, v)
    return c


class FakeHub:
    def __init__(self):
        self.events = []

    def emit(self, e):
        self.events.append(e)

    def of(self, t):
        return [e for e in self.events if e["type"] == t]


# ---- a tiny GlobalPetrolPrices page --------------------------------

def _gpp_page(pairs):
    links = "".join(
        f"<div class='outsideTitle'><a href='/{n}/gasoline_prices/' "
        f"class='graph_outside_link'>{n}&nbsp;</a></div>"
        for n, _ in pairs
    )
    bars = "".join(
        f'<div style="position: absolute; left: 21px; top: {18 + i*21}px; width: 40px; '
        f'height: 17px; background: #e2bb04;"><div style="position: absolute; top: 2px; '
        f'left: 7px; height: 15px; color: #000000;">{p:.3f}</div></div>'
        for i, (_, p) in enumerate(pairs)
    )
    return f'<html><body><div id="outsideLinks">{links}</div><div id="graphic">{bars}</div></body></html>'


GPP_GAS = _gpp_page([("Venezuela", 0.035), ("Iran", 0.029), ("Germany", 2.643),
                     ("Norway", 3.108), ("Atlantis", 9.99)])   # Atlantis = unmapped
GPP_DIE = _gpp_page([("Venezuela", 0.004), ("Germany", 2.720), ("Norway", 2.974)])


def _yahoo_chart(px, prev, closes):
    return {"chart": {"result": [{
        "meta": {"regularMarketPrice": px, "chartPreviousClose": prev,
                 "symbol": "CL=F", "currency": "USD"},
        "timestamp": [1_700_000_000 + i * 86400 for i in range(len(closes))],
        "indicators": {"quote": [{"close": closes}]},
    }]}}


def _handler(req: httpx.Request) -> httpx.Response:
    u = str(req.url)
    if "finance/chart/CL=F" in u:
        return httpx.Response(200, json=_yahoo_chart(103.68, 100.0, [95.0, 98.0, 100.0, 103.68]))
    if "finance/chart/BZ=F" in u:
        return httpx.Response(200, json=_yahoo_chart(108.73, 105.0, [101.0, 103.0, 105.0, 108.73]))
    if "gasoline_prices" in u:
        return httpx.Response(200, text=GPP_GAS)
    if "diesel_prices" in u:
        return httpx.Response(200, text=GPP_DIE)
    if "api.eia.gov" in u:
        code = req.url.params.get("facets[series][]")
        val = 103.0 if code == "RWTC" else 108.0
        return httpx.Response(200, json={"response": {"data": [
            {"period": "2026-09-09", "value": val},
            {"period": "2026-09-08", "value": val - 2},
        ]}})
    return httpx.Response(404, text="nope")


def _svc(tmp_path, cfg=None, crude=None):
    con = db.init(tmp_path / "h.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return FuelService(cfg or _cfg(tmp_path), FakeHub(), con, client=client, crude=crude), con


# ---- GPP parser ---------------------------------------------------

def test_parse_gpp_zips_names_and_prices():
    rows = parse_gpp(GPP_GAS)
    assert rows[0] == ("Venezuela", 0.035)
    assert ("Germany", 2.643) in rows
    assert len(rows) == 5


def test_parse_gpp_tolerates_junk():
    assert parse_gpp("<html>nothing here</html>") == []


# ---- crude sources ---------------------------------------------

async def test_yahoo_latest_and_history(tmp_path):
    c = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    y = YahooCrude()
    latest = await y.latest(c)
    assert latest["wti"] == {"usd": 103.68, "prev_close": 100.0}
    assert latest["brent"]["usd"] == 108.73
    hist = await y.history(c, "wti", 365)
    assert hist[-1][1] == 103.68 and len(hist) == 4
    await c.aclose()


async def test_eia_source_used_when_key_set(tmp_path):
    cfg = _cfg(tmp_path, eia_key="ABC123")
    svc, con = _svc(tmp_path, cfg)
    assert isinstance(svc.crude, EiaCrude) and svc.crude.name == "eia"
    latest = await svc.crude.latest(svc._client)
    assert latest["wti"] == {"usd": 103.0, "prev_close": 101.0}
    await svc._client.aclose()


# ---- FuelService ---------------------------------------------

async def test_crude_tick_persists_and_emits(tmp_path):
    svc, con = _svc(tmp_path)
    await svc._tick_crude()
    assert svc.latest_crude["wti"]["usd"] == 103.68
    rows = con.execute("SELECT symbol, usd, source FROM fuel_crude").fetchall()
    assert {r["symbol"] for r in rows} == {"wti", "brent"}
    assert rows[0]["source"] == "yahoo"
    tick = svc.hub.of("fuel_tick")[-1]
    assert tick["crude"]["brent"]["usd"] == 108.73


async def test_backfill_seeds_history_once(tmp_path):
    svc, con = _svc(tmp_path)
    assert not svc._has_crude_history()
    await svc._backfill_crude()
    n = con.execute("SELECT COUNT(*) c FROM fuel_crude").fetchone()["c"]
    assert n == 8                       # 4 daily closes x 2 symbols
    assert svc._has_crude_history()
    ser = svc.crude_series("wti", days=3650)
    assert ser[-1]["usd"] == 103.68 and ser[0]["ts"] < ser[-1]["ts"]


async def test_retail_scrape_maps_iso_and_drops_unknown(tmp_path):
    svc, con = _svc(tmp_path)
    await svc._scrape_retail()
    m = svc.retail_map("gasoline")
    assert m["prices"]["DE"] == 2.643 and m["prices"]["VE"] == 0.035
    assert "Atlantis" not in str(m["prices"])          # unmapped dropped
    assert set(svc.retail_map("diesel")["prices"]) == {"VE", "DE", "NO"}
    assert svc.retail_stale is False
    assert svc.hub.of("fuel_retail_ready")
    note = svc.hub.of("fuel_status")
    assert any("Atlantis" in e.get("detail", "") for e in note)


async def test_retail_scrape_goes_stale_on_http_error(tmp_path):
    def bad(req):
        return httpx.Response(503, text="down")
    con = db.init(tmp_path / "h.db")
    svc = FuelService(_cfg(tmp_path), FakeHub(), con,
                      client=httpx.AsyncClient(transport=httpx.MockTransport(bad)))
    await svc._scrape_retail()
    assert svc.retail_stale is True
    assert svc.hub.of("fuel_status")[-1]["state"] == "stale"
    await svc._client.aclose()


async def test_snapshot_and_stop_clean(tmp_path):
    svc, con = _svc(tmp_path)
    await svc.start()
    snap = svc.snapshot()
    assert snap["source"] == "yahoo" and "retail_stale" in snap
    await svc.stop()
