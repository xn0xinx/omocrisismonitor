"""FastAPI app: serves the map window, a WebSocket event stream, and a small
REST surface. Phase 0 wired the shell (map, theme sync, health); Phase 1 the
AIS service + `/api/view` / `/api/vessel`; Phase 2 the conflict service +
`/api/conflict/*`; Phase 3 the fuel service + `/api/fuel/*`; Phase 4 the AI
sidebar + `/api/ai/*`; Phase 5 the alert service + `/api/alerts/*` and the
correlation readout at `/api/correlate`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import correlate, db
from .ai import AiService
from .ais import AisService
from .alerts import AlertService
from .conflict import ConflictService, _iso_to_sort
from .config import config_path, db_path
from .fuel import FuelService

WEB = files("omocrisismonitor").joinpath("web")
__version__ = "0.6.1"


class Hub:
    """Fan-out to every connected window."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def emit(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # slow window — drop it rather than stall the producer
                self._subs.discard(q)


class _NoCacheStatic(StaticFiles):
    async def get_response(self, path: str, scope):  # noqa: ANN001
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


def _bundled_theme() -> Path:
    return Path(str(WEB.joinpath("theme.css")))


async def _watch_theme(app: FastAPI) -> None:
    """Poll the user's theme.css mtime; push a `theme` event on change."""
    target = config_path().with_name("theme.css")
    last = target.stat().st_mtime if target.is_file() else 0.0
    while True:
        await asyncio.sleep(1.0)
        try:
            m = target.stat().st_mtime if target.is_file() else 0.0
        except OSError:
            m = 0.0
        if m != last:
            last = m
            app.state.hub.emit({"type": "theme"})


def create_app(cfg: SimpleNamespace) -> FastAPI:
    app = FastAPI(title="OmoCrisisMonitor", version=__version__)
    app.state.cfg = cfg
    app.state.hub = Hub()
    app.state.started = time.time()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN001
        app.state.db = db.init(db_path())
        app.state.ais = AisService(cfg, app.state.hub, app.state.db)
        app.state.conflict = ConflictService(cfg, app.state.hub, app.state.db)
        app.state.fuel = FuelService(cfg, app.state.hub, app.state.db)
        app.state.ai = AiService(
            cfg, app.state.hub,
            ais=app.state.ais, conflict=app.state.conflict, fuel=app.state.fuel,
        )
        app.state.alerts = AlertService(
            cfg, app.state.hub, app.state.db,
            ais=app.state.ais, conflict=app.state.conflict, fuel=app.state.fuel,
        )
        watcher = asyncio.create_task(_watch_theme(app))
        await app.state.ais.start()
        await app.state.conflict.start()
        await app.state.fuel.start()
        await app.state.ai.start()
        await app.state.alerts.start()
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await app.state.ais.stop()
            await app.state.conflict.stop()
            await app.state.fuel.stop()
            await app.state.ai.stop()
            await app.state.alerts.stop()
            app.state.db.close()

    app.router.lifespan_context = lifespan

    # ---- REST -----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(Path(str(WEB.joinpath("index.html"))).read_text("utf-8"))

    @app.get("/theme.css")
    async def theme_css() -> Response:
        user = config_path().with_name("theme.css")
        src = user if user.is_file() else _bundled_theme()
        return Response(
            src.read_text("utf-8"),
            media_type="text/css",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "uptime_s": round(time.time() - app.state.started, 1),
                "db": str(db_path()),
            }
        )

    @app.get("/api/bootstrap")
    async def bootstrap() -> JSONResponse:
        """Everything the window needs at load time (no secrets beyond the
        MapTiler key, which is unavoidably client-side for any web map)."""
        m = cfg.map
        return JSONResponse(
            {
                "version": __version__,
                "map": {
                    "maptiler_key": m.maptiler_key,
                    "style": m.style,
                    "center": list(m.center),
                    "zoom": m.zoom,
                },
                "ui": {
                    "currency": cfg.ui.units_currency,
                    "fuel_unit": cfg.ui.fuel_unit,
                    "timezone": cfg.ui.timezone,
                    "start_fullscreen": cfg.ui.start_fullscreen,
                },
                "layers": {  # Phase gates — flipped on as each phase lands
                    "ais": True,
                    "conflict": True,
                    "fuel": True,
                    "ai": bool(cfg.ai.enabled),
                    "alerts": bool(cfg.alerts.enabled),
                },
                "ais": {
                    "throttle_ms": cfg.ais.throttle_ms,
                    "stale_after_s": cfg.ais.stale_after_s,
                },
                "conflict": {
                    "window_days": cfg.conflict.window_days,
                    "refresh_h": cfg.conflict.refresh_h,
                },
                "fuel": {
                    "unit": cfg.ui.fuel_unit,
                    "currency": cfg.ui.units_currency,
                },
                "ai": {
                    "auto_refresh": cfg.ai.auto_refresh,
                    "min_interval_s": cfg.ai.min_interval_s,
                    "model": cfg.ai.model,
                    "backend": app.state.ai.backend_key,
                    "backends": app.state.ai.list_backends(),
                },
                "alerts": {
                    "notify": cfg.alerts.notify,
                    "poll_s": cfg.alerts.poll_s,
                },
            }
        )

    @app.post("/api/view")
    async def set_view(payload: dict = Body(...)) -> JSONResponse:
        """Window viewport → AIS subscription box. bbox = [[s,w],[n,e]] or null."""
        bbox = payload.get("bbox")
        if bbox is not None:
            try:
                (s, w), (n, e) = bbox
                bbox = [[float(s), float(w)], [float(n), float(e)]]
            except (TypeError, ValueError):
                raise HTTPException(422, "bbox must be [[s,w],[n,e]]") from None
        app.state.ais.set_view(bbox)
        return JSONResponse({"ok": True, "state": app.state.ais.state})

    @app.get("/api/vessel/{mmsi}")
    async def vessel(mmsi: int) -> JSONResponse:
        v = app.state.ais.vessel(mmsi)
        if v is None:
            raise HTTPException(404, "unknown mmsi")
        return JSONResponse(v)

    # ---- conflict (GeoConfirmed) ------------------------------------
    def _as_sort(v: str | None) -> int | None:
        """Query param → dateSort int. Accepts a bare day-int or an ISO date."""
        if v is None or v == "":
            return None
        try:
            return int(v)
        except ValueError:
            return _iso_to_sort(v)

    @app.get("/api/conflict/bootstrap")
    async def conflict_bootstrap() -> JSONResponse:
        return JSONResponse(app.state.conflict.bootstrap())

    @app.get("/api/conflict/events")
    async def conflict_events(
        until: str | None = None,
        since: str | None = None,
        conflicts: str | None = None,
    ) -> JSONResponse:
        slugs = [s for s in (conflicts or "").split(",") if s] or None
        return JSONResponse(
            app.state.conflict.events_geojson(_as_sort(until), _as_sort(since), slugs)
        )

    @app.get("/api/conflict/detail/{ext_id}")
    async def conflict_detail(ext_id: str) -> JSONResponse:
        d = await app.state.conflict.detail(ext_id)
        if d is None:
            raise HTTPException(404, "unknown placemark")
        return JSONResponse(d)

    @app.post("/api/conflict/refresh")
    async def conflict_refresh() -> JSONResponse:
        """Manual kick — the layer normally refreshes on its own timer."""
        totals = await app.state.conflict.refresh()
        return JSONResponse({"ok": True, **totals})

    # ---- fuel (crude + retail) ------------------------------------
    @app.get("/api/fuel/bootstrap")
    async def fuel_bootstrap() -> JSONResponse:
        return JSONResponse(app.state.fuel.snapshot())

    @app.get("/api/fuel/crude")
    async def fuel_crude(symbol: str = "wti", days: int = 180) -> JSONResponse:
        if symbol not in ("wti", "brent"):
            raise HTTPException(422, "symbol must be wti or brent")
        return JSONResponse(
            {"symbol": symbol, "days": days,
             "series": app.state.fuel.crude_series(symbol, max(1, min(days, 3650)))}
        )

    @app.get("/api/fuel/retail")
    async def fuel_retail(kind: str = "gasoline") -> JSONResponse:
        if kind not in ("gasoline", "diesel"):
            raise HTTPException(422, "kind must be gasoline or diesel")
        return JSONResponse(app.state.fuel.retail_map(kind))

    # ---- AI sidebar -------------------------------------------------
    @app.get("/api/ai/summary")
    async def ai_summary() -> JSONResponse:
        return JSONResponse({"summary": app.state.ai.summary, "busy": app.state.ai.busy})

    @app.post("/api/ai/refresh")
    async def ai_refresh() -> JSONResponse:
        """Fire-and-forget — an AI CLI round trip can take tens of seconds; the
        result rides the `ai_summary` WS event, not this response."""
        if not cfg.ai.enabled:
            raise HTTPException(409, "AI sidebar is disabled in config.toml")
        if not app.state.ai.busy:
            asyncio.create_task(app.state.ai.refresh(reason="manual"))
        return JSONResponse({"ok": True, "state": "thinking"})

    @app.post("/api/ai/backend")
    async def ai_set_backend(payload: dict = Body(...)) -> JSONResponse:
        key = payload.get("key")
        try:
            app.state.ai.set_backend(key)
        except KeyError:
            raise HTTPException(422, f"unknown backend {key!r}") from None
        return JSONResponse({"ok": True, "backend": app.state.ai.backend_key})

    # ---- alerts (watch rules) --------------------------------------
    @app.get("/api/alerts/rules")
    async def alerts_list_rules() -> JSONResponse:
        return JSONResponse(app.state.alerts.list_rules())

    @app.post("/api/alerts/rules")
    async def alerts_create_rule(payload: dict = Body(...)) -> JSONResponse:
        try:
            rule = app.state.alerts.create_rule(
                payload.get("kind"), payload.get("label", ""),
                payload.get("params") or {}, payload.get("enabled", True),
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return JSONResponse(rule)

    @app.patch("/api/alerts/rules/{rule_id}")
    async def alerts_patch_rule(rule_id: int, payload: dict = Body(...)) -> JSONResponse:
        if "enabled" not in payload:
            raise HTTPException(422, "PATCH body needs 'enabled'")
        if not app.state.alerts.set_enabled(rule_id, bool(payload["enabled"])):
            raise HTTPException(404, "unknown rule")
        return JSONResponse(app.state.alerts.get_rule(rule_id))

    @app.delete("/api/alerts/rules/{rule_id}")
    async def alerts_delete_rule(rule_id: int) -> JSONResponse:
        if not app.state.alerts.delete_rule(rule_id):
            raise HTTPException(404, "unknown rule")
        return JSONResponse({"ok": True})

    @app.get("/api/alerts/hits")
    async def alerts_hits(limit: int = 50) -> JSONResponse:
        return JSONResponse(app.state.alerts.recent_hits(max(1, min(limit, 500))))

    # ---- correlation readout ----------------------------------------
    @app.get("/api/correlate")
    async def correlate_readout(bbox: str, days: int = 90) -> JSONResponse:
        try:
            s, w, n, e = (float(x) for x in bbox.split(","))
        except ValueError:
            raise HTTPException(422, "bbox must be 's,w,n,e'") from None
        return JSONResponse(
            correlate.readout(app.state.db, [[s, w], [n, e]], max(7, min(days, 3650)))
        )

    # ---- WebSocket ----------------------------------------------------
    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        q = app.state.hub.subscribe()
        await sock.send_json(
            {
                "type": "snapshot",
                "version": __version__,
                "since": time.time(),
            }
        )
        pinger = asyncio.create_task(_ping(sock))
        try:
            while True:
                event = await q.get()
                await sock.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            pinger.cancel()
            app.state.hub.unsubscribe(q)

    async def _ping(sock: WebSocket) -> None:
        try:
            while True:
                await asyncio.sleep(20)
                await sock.send_json({"type": "ping", "t": time.time()})
        except Exception:  # noqa: BLE001
            pass

    # ---- static (last, so /api/* and /ws win) --------------------------
    app.mount("/", _NoCacheStatic(directory=str(WEB)), name="web")
    return app
