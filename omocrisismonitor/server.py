"""FastAPI app: serves the map window, a WebSocket event stream, and a small
REST surface. Phase 0 wired the shell (map, theme sync, health); Phase 1 adds
the AIS service + `/api/view` / `/api/vessel`. Later layers (conflict / fuel /
AI) attach to the same hub.
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

from . import db
from .ais import AisService
from .config import config_path, db_path

WEB = files("omocrisismonitor").joinpath("web")
__version__ = "0.2.0"


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
        watcher = asyncio.create_task(_watch_theme(app))
        await app.state.ais.start()
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await app.state.ais.stop()
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
                    "conflict": False,
                    "fuel": False,
                    "ai": False,
                    "alerts": False,
                },
                "ais": {
                    "throttle_ms": cfg.ais.throttle_ms,
                    "stale_after_s": cfg.ais.stale_after_s,
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
