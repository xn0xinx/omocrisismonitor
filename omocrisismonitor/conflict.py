"""Conflict layer: periodically pulls every tracked GeoConfirmed theatre's
GeoJSON feed and upserts each placemark into `conflict_event`, id-keyed. Each
event carries its own date, so "scrub the map back in time" is a date filter,
not a pile of daily snapshots. Detail (description / sources / geolocation) is
fetched live per-id when the user clicks a pin — never stored.

GeoConfirmed v2 API, public reads (no key):
  GET /api/Conflict                    -> theatre list
  GET /api/Placemark/{slug}/geojson    -> {factionMeta:[...], geojson:{FeatureCollection}}
                                          feature props: id, factionId, color, icon,
                                          date (ISO), dateSort (days since 1970-01-01)
  GET /api/Placemark/detail/{id}       -> full placemark record

The HTTP client is injectable (`client=`) so tests feed canned payloads without
a network. Only Point geometries are ingested (frontline lines/polygons are a
later phase).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx

# theatres to skip unless the user names them explicitly in cfg.conflict.theatres
# — historical, would drag ~150k old rows in on the first pull for no live value.
_HISTORICAL = {"wwi", "wwii"}

DAY = 86400
DETAIL_TTL = 3600.0          # seconds to trust a cached /detail response
DETAIL_CACHE_MAX = 500
_HTTP_TIMEOUT = httpx.Timeout(15.0, read=120.0)   # Ukraine's feed is ~16 MB


def _today_sort() -> int:
    return int(time.time() // DAY)


def _iso_to_sort(iso: str | None) -> int | None:
    """'2026-09-09T00:00:00' -> days since 1970-01-01 (GeoConfirmed's dateSort).
    Only used as a fallback when a feature omits `dateSort`."""
    if not iso:
        return None
    try:
        y, m, d = (int(x) for x in iso[:10].split("-"))
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() // DAY)
    except (ValueError, OverflowError, OSError):
        return None


class ConflictService:
    def __init__(
        self,
        cfg: Any,
        hub: Any,
        con: Any = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.hub = hub
        self.con = con
        self._client = client
        self._owns_client = client is None
        self._base = cfg.conflict.api_base.rstrip("/")
        self.theatres: list[dict] = []            # last /api/Conflict result, filtered
        self.last_refresh: float = 0.0
        self.refreshing = False
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._detail_cache: dict[str, tuple[float, dict]] = {}

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
        self._task = asyncio.create_task(self._loop(), name="conflict-loop")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._owns_client and self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()

    async def _loop(self) -> None:
        # kick off an immediate refresh unless the DB was topped up recently
        gap = self.cfg.conflict.refresh_h * 3600
        if self._seconds_since_last_snapshot() > gap:
            await self._safe_refresh()
        while not self._stopped.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=gap)
            if self._stopped.is_set():
                break
            await self._safe_refresh()

    async def _safe_refresh(self) -> None:
        try:
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a bad refresh must not kill the loop
            self.hub.emit({"type": "conflict_status", "state": "error", "detail": str(e)})

    # ---- refresh ------------------------------------------------------
    async def refresh(self) -> dict:
        self.refreshing = True
        self.hub.emit({"type": "conflict_status", "state": "refreshing"})
        try:
            theatres = await self._fetch_theatres()
            self.theatres = theatres
            floor = _today_sort() - self.cfg.conflict.window_days * 2   # ingest slack
            now = int(time.time())
            totals = {"conflicts": 0, "events": 0, "added": 0}
            for th in theatres:
                slug = th["url"]
                try:
                    payload = await self._get_json(f"/api/Placemark/{slug}/geojson")
                except Exception as e:  # noqa: BLE001 - one dead theatre != dead refresh
                    self.hub.emit(
                        {"type": "conflict_status", "state": "error",
                         "detail": f"{slug}: {e}"}
                    )
                    continue
                seen, added = self._ingest(slug, payload, floor, now)
                totals["conflicts"] += 1
                totals["events"] += seen
                totals["added"] += added
            self.last_refresh = time.time()
            self.hub.emit(
                {"type": "conflict_refresh", "ts": now,
                 "added": totals["added"], "events": totals["events"],
                 "span": self.span()}
            )
            return totals
        finally:
            self.refreshing = False

    async def _fetch_theatres(self) -> list[dict]:
        data = await self._get_json("/api/Conflict")
        pin = set(self.cfg.conflict.theatres or [])
        out = []
        for c in data if isinstance(data, list) else []:
            slug = c.get("url")
            if not slug or c.get("isPrivate"):
                continue
            if pin:
                if slug in pin:
                    out.append(c)
            elif slug not in _HISTORICAL:
                out.append(c)
        return out

    def _ingest(self, slug: str, payload: dict, floor_sort: int, now: int) -> tuple[int, int]:
        fc = (payload or {}).get("geojson") or {}
        feats = fc.get("features") or []
        factions = (payload or {}).get("factionMeta") or []

        rows = []
        for f in feats:
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            p = f.get("properties") or {}
            ext_id = p.get("id")
            if not ext_id:
                continue
            ds = p.get("dateSort")
            if ds is None:
                ds = _iso_to_sort(p.get("date"))
            if ds is None or ds < floor_sort:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            rows.append((ext_id, slug, lat, lon, p.get("date"), ds,
                         p.get("factionId"), p.get("color"), p.get("icon"), now, now))

        if self.con is None:
            return len(rows), 0

        before = self._count(slug)
        self.con.executemany(
            "INSERT INTO conflict_event"
            " (ext_id,conflict,lat,lon,date,date_sort,faction_id,color,icon,first_seen,last_seen)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(ext_id) DO UPDATE SET"
            "   lat=excluded.lat, lon=excluded.lon, date=excluded.date,"
            "   date_sort=excluded.date_sort, faction_id=excluded.faction_id,"
            "   color=excluded.color, icon=excluded.icon, last_seen=excluded.last_seen",
            rows,
        )
        self.con.execute("DELETE FROM conflict_faction WHERE conflict=?", (slug,))
        self.con.executemany(
            "INSERT OR REPLACE INTO conflict_faction(conflict,id,name,color) VALUES(?,?,?,?)",
            [(slug, fac.get("id"), fac.get("name"), fac.get("color")) for fac in factions
             if fac.get("id") is not None],
        )
        after = self._count(slug)
        added = max(0, after - before)
        self.con.execute(
            "INSERT INTO conflict_snapshot(ts,conflict,event_count,added) VALUES(?,?,?,?)",
            (now, slug, len(rows), added),
        )
        self.con.commit()
        return len(rows), added

    def _count(self, slug: str) -> int:
        r = self.con.execute(
            "SELECT COUNT(*) c FROM conflict_event WHERE conflict=?", (slug,)
        ).fetchone()
        return r["c"] if r else 0

    def _seconds_since_last_snapshot(self) -> float:
        if self.con is None:
            return 1e12
        r = self.con.execute("SELECT MAX(ts) m FROM conflict_snapshot").fetchone()
        return 1e12 if not r or r["m"] is None else time.time() - r["m"]

    # ---- read side (server routes call these) -------------------------
    def span(self) -> dict:
        if self.con is None:
            return {}
        r = self.con.execute(
            "SELECT MIN(date_sort) lo, MAX(date_sort) hi FROM conflict_event"
        ).fetchone()
        floor = _today_sort() - self.cfg.conflict.window_days
        return {
            "min_sort": r["lo"], "max_sort": r["hi"],
            "today_sort": _today_sort(), "window_days": self.cfg.conflict.window_days,
            "floor_sort": floor,
        }

    def bootstrap(self) -> dict:
        factions: dict[str, list] = {}
        counts: dict[str, int] = {}
        if self.con is not None:
            for row in self.con.execute(
                "SELECT conflict, id, name, color FROM conflict_faction ORDER BY conflict, id"
            ):
                factions.setdefault(row["conflict"], []).append(
                    {"id": row["id"], "name": row["name"], "color": row["color"]}
                )
            for row in self.con.execute(
                "SELECT conflict, COUNT(*) c FROM conflict_event GROUP BY conflict"
            ):
                counts[row["conflict"]] = row["c"]
        return {
            "conflicts": [
                {"slug": t["url"], "name": t.get("name") or t["url"],
                 "count": counts.get(t["url"], 0)}
                for t in self.theatres
            ] or [{"slug": s, "name": s, "count": counts[s]} for s in sorted(counts)],
            "factions": factions,
            "span": self.span(),
            "window_days": self.cfg.conflict.window_days,
        }

    def events_geojson(
        self,
        until_sort: int | None = None,
        since_sort: int | None = None,
        conflicts: list[str] | None = None,
    ) -> dict:
        """Trailing-window slice for the map: events in [since, until]."""
        if self.con is None:
            return {"type": "FeatureCollection", "features": []}
        until = until_sort if until_sort is not None else _today_sort()
        since = since_sort if since_sort is not None else until - self.cfg.conflict.window_days
        q = ("SELECT ext_id,conflict,lat,lon,date,date_sort,faction_id,color,icon"
             " FROM conflict_event WHERE date_sort BETWEEN ? AND ?")
        args: list = [since, until]
        if conflicts:
            q += " AND conflict IN (%s)" % ",".join("?" * len(conflicts))
            args += conflicts
        feats = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {
                    "id": r["ext_id"], "c": r["conflict"], "f": r["faction_id"],
                    "color": r["color"], "date": r["date"], "ds": r["date_sort"],
                },
            }
            for r in self.con.execute(q, args)
        ]
        return {"type": "FeatureCollection", "features": feats,
                "window": {"since_sort": since, "until_sort": until}}

    async def recent_highlights(
        self, days: int = 1, max_theatres: int = 4, per_theatre: int = 2
    ) -> dict:
        """Event counts per theatre since `days` ago, plus a couple of live-fetched
        descriptions for the busiest theatres — for the AI sidebar (Phase 4).
        Not hot-path: only called on an AI refresh, gated by ai.min_interval_s."""
        if self.con is None:
            return {"since_sort": _today_sort() - days, "by_theatre": {}, "highlights": {}}
        since = _today_sort() - days
        rows = self.con.execute(
            "SELECT conflict, COUNT(*) c FROM conflict_event WHERE date_sort >= ? "
            "GROUP BY conflict ORDER BY c DESC",
            (since,),
        ).fetchall()
        by_theatre = {r["conflict"]: r["c"] for r in rows}
        highlights: dict[str, list[str]] = {}
        for conflict in list(by_theatre)[:max_theatres]:
            ids = [
                r["ext_id"]
                for r in self.con.execute(
                    "SELECT ext_id FROM conflict_event WHERE conflict=? AND date_sort>=? "
                    "ORDER BY date_sort DESC LIMIT ?",
                    (conflict, since, per_theatre),
                )
            ]
            descs = []
            for ext_id in ids:
                d = await self.detail(ext_id)
                if d and d.get("description"):
                    descs.append(d["description"][:220].strip())
            if descs:
                highlights[conflict] = descs
        return {"since_sort": since, "by_theatre": by_theatre, "highlights": highlights}

    async def detail(self, ext_id: str) -> dict | None:
        hit = self._detail_cache.get(ext_id)
        if hit and time.time() - hit[0] < DETAIL_TTL:
            return hit[1]
        if self._client is None:
            return None
        try:
            r = await self._client.get(f"{self._base}/api/Placemark/detail/{ext_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
        except Exception:  # noqa: BLE001
            return hit[1] if hit else None
        if len(self._detail_cache) >= DETAIL_CACHE_MAX:
            self._detail_cache.pop(next(iter(self._detail_cache)), None)
        self._detail_cache[ext_id] = (time.time(), data)
        return data

    # ---- http ------------------------------------------------------
    async def _get_json(self, path: str) -> Any:
        assert self._client is not None
        r = await self._client.get(self._base + path)
        r.raise_for_status()
        return r.json()
