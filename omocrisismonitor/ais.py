"""AIS layer: one long-lived WebSocket to aisstream.io, subscribed to the
window's current viewport. Merges PositionReport + ShipStaticData per vessel,
throttles updates, sweeps stale ships, persists static + a sparse track, and
pushes `ais_upsert` / `ais_drop` / `ais_status` to the Hub.

The socket is injectable (`connector=`) so tests can feed canned frames.
aisstream rules honoured: 1 connection, resubscribe no more than ~1/s, and
messages consumed promptly.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from . import aismeta

# don't subscribe to more than this much of the globe at once (potato guard);
# the client shows "zoom in to load ships" instead.
MAX_SPAN_LAT = 70.0
MAX_SPAN_LON = 140.0
MAX_VESSELS = 3000
TRACK_MIN_GAP_S = 60


@dataclass
class Vessel:
    mmsi: int
    lat: float | None = None
    lon: float | None = None
    sog: float | None = None
    cog: float | None = None
    heading: int | None = None
    navstat: int | None = None
    name: str = ""
    imo: int | None = None
    callsign: str = ""
    type: int | None = None
    destination: str = ""
    eta: str = ""
    draught: float | None = None
    dim: list[int | None] = field(default_factory=lambda: [None, None, None, None])
    last: float = 0.0
    _last_track: float = 0.0
    _static_dirty: bool = False

    def public(self) -> dict[str, Any]:
        a, b, c, d = self.dim
        length = (a + b) if (a is not None and b is not None) else None
        beam = (c + d) if (c is not None and d is not None) else None
        label, cat = aismeta.type_info(self.type)
        return {
            "mmsi": self.mmsi,
            "lat": self.lat,
            "lon": self.lon,
            "sog": self.sog,
            "cog": self.cog,
            "heading": self.heading,
            "navstat": self.navstat,
            "navstat_label": aismeta.NAV_STATUS.get(
                self.navstat if self.navstat is not None else 15, ""
            ),
            "name": self.name or f"MMSI {self.mmsi}",
            "imo": self.imo,
            "callsign": self.callsign,
            "type": self.type,
            "type_label": label,
            "cat": cat,
            "flag": aismeta.flag_for_mmsi(self.mmsi),
            "destination": self.destination,
            "eta": self.eta,
            "draught": self.draught,
            "length": length,
            "beam": beam,
            "last": round(self.last),
        }


Connector = Callable[[str], Awaitable[Any]]


def _valid_pos(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and not (abs(lat) < 1e-6 and abs(lon) < 1e-6)
    )


def _fmt_eta(eta: dict | None) -> str:
    if not eta:
        return ""
    mo, d, h, mi = eta.get("Month"), eta.get("Day"), eta.get("Hour"), eta.get("Minute")
    if not mo or not d:
        return ""
    return f"{mo:02d}-{d:02d} {h or 0:02d}:{mi or 0:02d}Z"


class AisService:
    def __init__(
        self,
        cfg: Any,
        hub: Any,
        con: Any = None,
        *,
        connector: Connector | None = None,
    ) -> None:
        self.cfg = cfg
        self.hub = hub
        self.con = con
        self._connector = connector or self._default_connector
        self.vessels: dict[int, Vessel] = {}
        self._dirty: set[int] = set()
        self._bbox: list[list[float]] | None = None       # [[s, w], [n, e]]
        self._sub_dirty = asyncio.Event()
        self._ws: Any = None
        self._tasks: list[asyncio.Task] = []
        self._stopped = asyncio.Event()
        self.state = "idle"                               # idle|zoomed_out|connecting|live|down

    # ---- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._run(), name="ais-run"),
            asyncio.create_task(self._flush_loop(), name="ais-flush"),
            asyncio.create_task(self._sweep_loop(), name="ais-sweep"),
        ]

    async def stop(self) -> None:
        self._stopped.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ---- public API -----------------------------------------------------
    def set_view(self, bbox: list[list[float]] | None) -> None:
        """bbox = [[south, west], [north, east]] in degrees, or None."""
        self._bbox = bbox
        self._sub_dirty.set()
        if bbox is None or self._span_too_big(bbox):
            self._emit_status("zoomed_out")
        elif self.state in ("idle", "zoomed_out", "down"):
            # entering a usable viewport — report the transition now rather than
            # leave a stale state on the API until _run reacts.
            self._emit_status("connecting")

    def vessel(self, mmsi: int) -> dict[str, Any] | None:
        v = self.vessels.get(mmsi)
        return v.public() if v else None

    def vessel_count(self) -> int:
        return len(self.vessels)

    def summary(self) -> dict[str, Any]:
        """Compact snapshot of the current viewport, for the AI sidebar (Phase 4)
        — not the per-vessel detail the map layer already has client-side."""
        cats: dict[str, int] = {}
        notable = []
        for v in self.vessels.values():
            _, cat = aismeta.type_info(v.type)
            cats[cat] = cats.get(cat, 0) + 1
            if cat in ("tanker", "special"):
                notable.append({
                    "name": v.name or f"MMSI {v.mmsi}",
                    "type": aismeta.type_info(v.type)[0],
                    "flag": aismeta.flag_for_mmsi(v.mmsi),
                })
        return {
            "count": len(self.vessels),
            "by_category": cats,
            "viewport": self._bbox,
            "notable": notable[:8],
        }

    # ---- internals ----------------------------------------------------
    @staticmethod
    def _span_too_big(bbox: list[list[float]]) -> bool:
        (s, w), (n, e) = bbox
        return abs(n - s) > MAX_SPAN_LAT or abs(e - w) > MAX_SPAN_LON

    def _emit_status(self, state: str) -> None:
        self.state = state
        self.hub.emit({"type": "ais_status", "state": state, "count": len(self.vessels)})

    async def _default_connector(self, url: str):  # pragma: no cover - needs network
        import websockets

        return await websockets.connect(url, ping_interval=20, max_queue=512)

    def _subscription(self) -> str | None:
        if not self._bbox or self._span_too_big(self._bbox):
            return None
        (s, w), (n, e) = self._bbox
        return json.dumps(
            {
                "APIKey": self.cfg.ais.api_key,
                "BoundingBoxes": [[[s, w], [n, e]]],
                "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
            }
        )

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopped.is_set():
            sub = self._subscription()
            if sub is None:
                # nothing worth subscribing to yet — wait for a usable viewport
                self._sub_dirty.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._sub_dirty.wait(), timeout=5.0)
                continue
            try:
                self._emit_status("connecting")
                self._ws = await self._connector(self.cfg.ais.ws_url)
                await self._ws.send(sub)
                last_sub = time.monotonic()
                self._sub_dirty.clear()
                self._emit_status("live")
                backoff = 1.0
                async for raw in self._ws:
                    self._ingest(raw)
                    if self._sub_dirty.is_set() and time.monotonic() - last_sub > 1.15:
                        s = self._subscription()
                        self._sub_dirty.clear()
                        if s is None:
                            break  # viewport went too big → drop the connection
                        await self._ws.send(s)
                        last_sub = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on anything
                self._emit_status("down")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.close()
                    self._ws = None

    def _ingest(self, raw: Any) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        mt = msg.get("MessageType")
        md = msg.get("MetaData") or {}
        try:
            mmsi = int(md.get("MMSI") or msg.get("Message", {}).get(mt, {}).get("UserID"))
        except (TypeError, ValueError):
            return
        body = (msg.get("Message") or {}).get(mt) or {}
        now = time.time()
        v = self.vessels.get(mmsi)
        if v is None:
            v = self.vessels[mmsi] = Vessel(mmsi=mmsi)
        v.last = now

        if mt == "PositionReport":
            v.lat = body.get("Latitude", v.lat)
            v.lon = body.get("Longitude", v.lon)
            v.sog = body.get("Sog", v.sog)
            v.cog = body.get("Cog", v.cog)
            th = body.get("TrueHeading")
            v.heading = None if th in (511, None) else th
            v.navstat = body.get("NavigationalStatus", v.navstat)
            if not v.name and md.get("ShipName"):
                v.name = md["ShipName"].strip()
        elif mt == "ShipStaticData":
            nm = (body.get("Name") or md.get("ShipName") or "").strip()
            if nm:
                v.name = nm
            v.imo = body.get("ImoNumber") or v.imo
            v.callsign = (body.get("CallSign") or v.callsign or "").strip()
            v.type = body.get("Type", v.type)
            v.destination = (body.get("Destination") or v.destination or "").strip()
            v.draught = body.get("MaximumStaticDraught", v.draught)
            dim = body.get("Dimension") or {}
            if dim:
                v.dim = [dim.get("A"), dim.get("B"), dim.get("C"), dim.get("D")]
            v.eta = _fmt_eta(body.get("Eta")) or v.eta
            v._static_dirty = True
        else:
            return

        if _valid_pos(v.lat, v.lon):
            self._dirty.add(mmsi)

    async def _flush_loop(self) -> None:
        gap = max(0.25, self.cfg.ais.throttle_ms / 1000)
        while not self._stopped.is_set():
            await asyncio.sleep(gap)
            if not self._dirty:
                continue
            ids = list(self._dirty)
            self._dirty.clear()
            out: list[dict] = []
            for mmsi in ids:
                v = self.vessels.get(mmsi)
                if v and _valid_pos(v.lat, v.lon):
                    out.append(v.public())
                    self._persist(v)
            if out:
                self.hub.emit({"type": "ais_upsert", "vessels": out})
            if len(self.vessels) > MAX_VESSELS:
                self._evict()

    async def _sweep_loop(self) -> None:
        while not self._stopped.is_set():
            await asyncio.sleep(30)
            cutoff = time.time() - self.cfg.ais.stale_after_s
            gone = [m for m, v in self.vessels.items() if v.last < cutoff]
            for m in gone:
                self.vessels.pop(m, None)
                self._dirty.discard(m)
            if gone:
                self.hub.emit({"type": "ais_drop", "mmsi": gone})

    def _evict(self) -> None:
        keep = sorted(self.vessels.values(), key=lambda v: v.last, reverse=True)[:MAX_VESSELS]
        keepset = {v.mmsi for v in keep}
        dropped = [m for m in self.vessels if m not in keepset]
        for m in dropped:
            self.vessels.pop(m, None)
        if dropped:
            self.hub.emit({"type": "ais_drop", "mmsi": dropped})

    def _persist(self, v: Vessel) -> None:
        if self.con is None:
            return
        now = time.time()
        try:
            if v._static_dirty:
                self.con.execute(
                    "INSERT INTO ais_vessel(mmsi,name,imo,callsign,ship_type,"
                    "dim_a,dim_b,dim_c,dim_d,draught,destination,eta,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(mmsi) DO UPDATE SET name=excluded.name,imo=excluded.imo,"
                    "callsign=excluded.callsign,ship_type=excluded.ship_type,"
                    "dim_a=excluded.dim_a,dim_b=excluded.dim_b,dim_c=excluded.dim_c,"
                    "dim_d=excluded.dim_d,draught=excluded.draught,"
                    "destination=excluded.destination,eta=excluded.eta,updated=excluded.updated",
                    (
                        v.mmsi, v.name, v.imo, v.callsign, v.type,
                        *v.dim, v.draught, v.destination, v.eta, int(now),
                    ),
                )
                v._static_dirty = False
            if now - v._last_track >= TRACK_MIN_GAP_S:
                self.con.execute(
                    "INSERT OR IGNORE INTO ais_track(mmsi,ts,lat,lon,sog,cog) "
                    "VALUES(?,?,?,?,?,?)",
                    (v.mmsi, int(now), v.lat, v.lon, v.sog, v.cog),
                )
                v._last_track = now
            self.con.commit()
        except Exception:  # noqa: BLE001 - persistence must never break the stream
            pass
