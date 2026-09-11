"""Fuel layer: crude benchmarks (Brent / WTI) for the live strip + a
retail-pump-price world choropleth.

Crude source is pluggable (`CrudeSource`):
  * YahooCrude  — default, no key. `query1.finance.yahoo.com/v8/finance/chart`
    for CL=F (WTI) / BZ=F (Brent). ~15 min delayed, intraday + history.
  * EiaCrude    — used when cfg.fuel.eia_key is set. Official EIA API, daily.

Retail: a light weekly scrape of globalpetrolprices.com's gasoline + diesel
ranking pages (country list and price bars are parallel ranked sequences — zip
by index). Country label -> ISO2 via _fuel_geo.GPP_ISO; unmapped labels are
dropped. Any fetch failure keeps the last-known rows and sets `retail_stale`.

HTTP client injectable (`client=`) for tests; so is the crude source (`crude=`).
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any, Protocol

import httpx

from ._fuel_geo import GPP_ISO

YF = "https://query1.finance.yahoo.com/v8/finance/chart"
YF_SYMBOL = {"wti": "CL=F", "brent": "BZ=F"}
GPP = "https://www.globalpetrolprices.com"
GPP_PAGE = {"gasoline": "/gasoline_prices/", "diesel": "/diesel_prices/"}
UA = "Mozilla/5.0 (X11; Linux x86_64) omocrisismonitor/0.4"
_HTTP_TIMEOUT = httpx.Timeout(20.0)

_LINK_RE = re.compile(r"class='graph_outside_link'>([^<]+?)(?:&nbsp;|\*)?</a>")
_BAR_RE = re.compile(r"height: 15px; color: #000000;\">([\d.]+)</div>")


def parse_gpp(html: str) -> list[tuple[str, float]]:
    """GlobalPetrolPrices ranking page -> [(country label, USD/litre)]. The name
    column (`#outsideLinks`) and the price bars (`#graphic`) are both in rank
    order, so index-zip them."""
    names = _LINK_RE.findall(html)
    graphic = html[html.find('id="graphic"'):] or html
    prices = _BAR_RE.findall(graphic)
    out: list[tuple[str, float]] = []
    for name, price in zip(names, prices):
        try:
            out.append((name.strip(), float(price)))
        except ValueError:
            continue
    return out


class CrudeSource(Protocol):
    name: str
    async def latest(self, client: httpx.AsyncClient) -> dict[str, dict]: ...
    async def history(
        self, client: httpx.AsyncClient, symbol: str, days: int
    ) -> list[tuple[int, float]]: ...


class YahooCrude:
    name = "yahoo"

    async def _chart(self, client: httpx.AsyncClient, sym: str, params: dict) -> dict:
        r = await client.get(f"{YF}/{sym}", params=params, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.json()["chart"]["result"][0]

    async def latest(self, client: httpx.AsyncClient) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for k, sym in YF_SYMBOL.items():
            m = (await self._chart(client, sym, {"interval": "1d", "range": "5d"}))["meta"]
            px = m.get("regularMarketPrice")
            if px is None:
                continue
            prev = m.get("chartPreviousClose") or m.get("previousClose") or px
            out[k] = {"usd": float(px), "prev_close": float(prev)}
        return out

    async def history(self, client, symbol, days):
        rng = "1y" if days > 180 else "6mo" if days > 90 else "3mo"
        res = await self._chart(client, YF_SYMBOL[symbol], {"interval": "1d", "range": rng})
        ts = res.get("timestamp") or []
        closes = res["indicators"]["quote"][0]["close"]
        return [(int(t), float(c)) for t, c in zip(ts, closes) if c is not None]


class EiaCrude:
    name = "eia"
    _SERIES = {"wti": "RWTC", "brent": "RBRTE"}

    def __init__(self, key: str) -> None:
        self.key = key

    async def _rows(self, client: httpx.AsyncClient, code: str, length: int) -> list[dict]:
        r = await client.get(
            "https://api.eia.gov/v2/petroleum/pri/spt/data/",
            params={
                "api_key": self.key, "frequency": "daily", "data[0]": "value",
                "facets[series][]": code, "sort[0][column]": "period",
                "sort[0][direction]": "desc", "length": length,
            },
        )
        r.raise_for_status()
        return r.json()["response"]["data"]

    async def latest(self, client):
        out: dict[str, dict] = {}
        for k, code in self._SERIES.items():
            rows = await self._rows(client, code, 2)
            if rows:
                prev = rows[1]["value"] if len(rows) > 1 else rows[0]["value"]
                out[k] = {"usd": float(rows[0]["value"]), "prev_close": float(prev)}
        return out

    async def history(self, client, symbol, days):
        rows = await self._rows(client, self._SERIES[symbol], min(max(days, 30), 5000))
        out: list[tuple[int, float]] = []
        for row in rows:
            try:
                t = int(time.mktime(time.strptime(row["period"], "%Y-%m-%d")))
                out.append((t, float(row["value"])))
            except (ValueError, TypeError, KeyError):
                continue
        return sorted(out)


class FuelService:
    def __init__(
        self,
        cfg: Any,
        hub: Any,
        con: Any = None,
        *,
        client: httpx.AsyncClient | None = None,
        crude: CrudeSource | None = None,
    ) -> None:
        self.cfg = cfg
        self.hub = hub
        self.con = con
        self._client = client
        self._owns_client = client is None
        key = getattr(cfg.fuel, "eia_key", "") or ""
        self.crude: CrudeSource = crude or (EiaCrude(key) if key else YahooCrude())
        self.latest_crude: dict[str, dict] = {}       # {"wti": {"usd":..,"prev_close":..}}
        self.retail_updated: int | None = None
        self.retail_stale = False
        self._tasks: list[asyncio.Task] = []
        self._stopped = asyncio.Event()

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
        self._load_from_db()
        self._tasks = [
            asyncio.create_task(self._crude_loop(), name="fuel-crude"),
            asyncio.create_task(self._retail_loop(), name="fuel-retail"),
        ]

    async def stop(self) -> None:
        self._stopped.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._owns_client and self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()

    async def _safe(self, coro_fn) -> None:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a bad poll must not kill the loop
            self.hub.emit({"type": "fuel_status", "state": "error", "detail": str(e)})

    # ---- crude ------------------------------------------------------
    async def _crude_loop(self) -> None:
        gap = max(60, self.cfg.fuel.crude_poll_s)
        if self.con is not None and not self._has_crude_history():
            await self._safe(self._backfill_crude)
        while not self._stopped.is_set():
            await self._safe(self._tick_crude)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=gap)

    async def _tick_crude(self) -> None:
        px = await self.crude.latest(self._client)
        if not px:
            return
        now = int(time.time())
        # prefer a DB-derived prior-day close for the tile's day-over-day % —
        # Yahoo's meta.chartPreviousClose is relative to the request window.
        for k, v in px.items():
            pc = self._prev_close(k, now)
            if pc is not None:
                v["prev_close"] = pc
        self.latest_crude = px
        if self.con is not None:
            self.con.executemany(
                "INSERT INTO fuel_crude(ts,symbol,usd,source) VALUES(?,?,?,?) "
                "ON CONFLICT(ts,symbol) DO UPDATE SET usd=excluded.usd",
                [(now, k, v["usd"], self.crude.name) for k, v in px.items()],
            )
            self.con.commit()
        self.hub.emit({"type": "fuel_tick", "ts": now, "crude": px,
                       "source": self.crude.name})

    async def _backfill_crude(self) -> None:
        for sym in ("wti", "brent"):
            hist = await self.crude.history(self._client, sym, 365)
            if hist:
                self.con.executemany(
                    "INSERT OR IGNORE INTO fuel_crude(ts,symbol,usd,source) VALUES(?,?,?,?)",
                    [(t, sym, v, self.crude.name) for t, v in hist],
                )
        self.con.commit()

    # ---- retail ---------------------------------------------------
    async def _retail_loop(self) -> None:
        gap = max(3600, self.cfg.fuel.retail_poll_h * 3600)
        if self._retail_age() > gap:
            await self._safe(self._scrape_retail)
        while not self._stopped.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=gap)
            if self._stopped.is_set():
                break
            await self._safe(self._scrape_retail)

    async def _scrape_retail(self) -> None:
        now = int(time.time())
        rows: list[tuple] = []
        unknown: set[str] = set()
        for kind, page in GPP_PAGE.items():
            try:
                r = await self._client.get(GPP + page, headers={"User-Agent": UA})
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001 - degrade to STALE, keep last data
                self.retail_stale = True
                self.hub.emit({"type": "fuel_status", "state": "stale",
                               "detail": f"{kind}: {e}"})
                return
            for name, price in parse_gpp(r.text):
                iso = GPP_ISO.get(name)
                if iso:
                    rows.append((now, iso, kind, price, "globalpetrolprices"))
                else:
                    unknown.add(name)
        if not rows:
            self.retail_stale = True
            self.hub.emit({"type": "fuel_status", "state": "stale",
                           "detail": "retail scrape parsed nothing"})
            return
        if self.con is not None:
            self.con.executemany(
                "INSERT INTO fuel_retail(ts,country,kind,usd_per_l,source) "
                "VALUES(?,?,?,?,?) ON CONFLICT(ts,country,kind) DO UPDATE SET "
                "usd_per_l=excluded.usd_per_l",
                rows,
            )
            self.con.commit()
        self.retail_updated = now
        self.retail_stale = False
        self.hub.emit({"type": "fuel_retail_ready", "ts": now,
                       "countries": len({r[1] for r in rows})})
        if unknown:
            self.hub.emit({"type": "fuel_status", "state": "note",
                           "detail": "unmapped: " + ", ".join(sorted(unknown))})

    # ---- db / read side --------------------------------------
    def _load_from_db(self) -> None:
        if self.con is None:
            return
        for row in self.con.execute(
            "SELECT symbol, usd, ts FROM fuel_crude "
            "WHERE ts=(SELECT MAX(ts) FROM fuel_crude)"
        ):
            pc = self._prev_close(row["symbol"], row["ts"])
            self.latest_crude[row["symbol"]] = {
                "usd": row["usd"], "prev_close": pc if pc is not None else row["usd"]
            }
        r = self.con.execute("SELECT MAX(ts) m FROM fuel_retail").fetchone()
        self.retail_updated = r["m"] if r and r["m"] else None

    def _prev_close(self, symbol: str, latest_ts: int) -> float | None:
        """Most recent stored close from a day strictly before latest_ts's day."""
        if self.con is None:
            return None
        day = latest_ts - (latest_ts % 86400)
        r = self.con.execute(
            "SELECT usd FROM fuel_crude WHERE symbol=? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (symbol, day),
        ).fetchone()
        return r["usd"] if r else None

    def _has_crude_history(self) -> bool:
        r = self.con.execute("SELECT COUNT(*) c FROM fuel_crude").fetchone()
        return bool(r and r["c"] > 5)

    def _retail_age(self) -> float:
        return 1e12 if not self.retail_updated else time.time() - self.retail_updated

    def snapshot(self) -> dict:
        return {
            "crude": self.latest_crude,
            "source": self.crude.name,
            "retail_updated": self.retail_updated,
            "retail_stale": self.retail_stale,
        }

    def crude_series(self, symbol: str, days: int = 180) -> list[dict]:
        if self.con is None:
            return []
        since = int(time.time()) - days * 86400
        return [
            {"ts": row["ts"], "usd": row["usd"]}
            for row in self.con.execute(
                "SELECT ts, usd FROM fuel_crude WHERE symbol=? AND ts>=? ORDER BY ts",
                (symbol, since),
            )
        ]

    def retail_map(self, kind: str = "gasoline") -> dict:
        if self.con is None:
            return {"kind": kind, "updated": None, "prices": {}, "stale": self.retail_stale}
        r = self.con.execute(
            "SELECT MAX(ts) m FROM fuel_retail WHERE kind=?", (kind,)
        ).fetchone()
        ts = r["m"] if r and r["m"] else None
        prices: dict[str, float] = {}
        if ts:
            for row in self.con.execute(
                "SELECT country, usd_per_l FROM fuel_retail WHERE kind=? AND ts=?",
                (kind, ts),
            ):
                prices[row["country"]] = row["usd_per_l"]
        return {"kind": kind, "updated": ts, "prices": prices, "stale": self.retail_stale}
