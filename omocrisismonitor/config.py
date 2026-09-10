"""Configuration: DEFAULTS deep-merged with the user's TOML, exposed as a
nested SimpleNamespace.

Load order:
  1. built-in DEFAULTS below
  2. $OMOCRISISMONITOR_CONFIG if set, else $XDG_CONFIG_HOME/omocrisismonitor/config.toml
     (falls back to ~/.config/omocrisismonitor/config.toml)

Secrets (the AIS + MapTiler keys) live only in that TOML file, which is
mode 0600 and git-ignored. Nothing here is ever committed.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

APP = "omocrisismonitor"

DEFAULTS: dict = {
    "server": {
        "host": "127.0.0.1",
        "port": 8792,
    },
    "map": {
        # MapTiler vector style used as the base; the client restyles it live
        # from the --ocm-* theme vars. Key is injected server-side.
        "maptiler_key": "",
        "style": "dataviz-dark",
        # opening view (Q19: centred where the three feeds overlap most)
        "center": [-30.0, 25.0],
        "zoom": 2.2,
    },
    "ais": {
        # aisstream.io free WebSocket — key stays server-side, never sent to the
        # browser (their ToS forbids direct browser connections).
        "api_key": "",
        "ws_url": "wss://stream.aisstream.io/v0/stream",
        # coalesce per-MMSI updates before pushing to the window (ms)
        "throttle_ms": 1500,
        # drop ships not heard from in this long (s)
        "stale_after_s": 900,
    },
    "conflict": {
        # GeoConfirmed v2 API. Public read access, no key.
        "api_base": "https://geoconfirmed.org",
        # full re-pull of every theatre's GeoJSON feed this often (server-side;
        # the window only ever gets our trimmed slice). Ukraine's feed is ~16 MB.
        "refresh_h": 6,
        # recency cap: the window loads events newer than this, and it's the
        # floor the date scrubber can wind back to. History still accrues in the
        # DB forever (Q11) — this only bounds what's drawn.
        "window_days": 90,
        # [] = every active theatre GeoConfirmed lists; or pin a subset of url
        # slugs, e.g. ["ukraine", "israel", "iran"].
        "theatres": [],
    },
    "fuel": {
        # crude benchmarks (live-ish) + retail-per-country choropleth (weekly)
        "crude_poll_s": 900,
        "retail_poll_h": 24,
    },
    "ai": {
        # AI sidebar runs through the `claude` CLI on the user's subscription
        # (no API key). Disabled until Phase 4.
        "enabled": True,
        "claude_bin": "claude",
        "model": "claude-sonnet-5",
        "auto_refresh": True,          # regenerate on notable change (Q14 = c)
        "min_interval_s": 900,         # never more often than this
    },
    "history": {
        # Q11: keep forever. 0 = never prune.
        "prune_after_days": 0,
    },
    "ui": {
        "units_currency": "USD",
        "fuel_unit": "L",             # USD per litre (Q20)
        "timezone": "local",          # local Omarchy time (Q20)
        "start_fullscreen": False,
    },
    "alerts": {
        "enabled": True,              # Q17: basic watch/alert system in v1
        "notify": True,               # desktop notifications
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _ns(d):
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in d.items()})
    return d


def config_path() -> Path:
    env = os.environ.get("OMOCRISISMONITOR_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP / "config.toml"


def state_path() -> Path:
    """state.json sibling of config.toml — app-managed (window pos, last view…)."""
    return config_path().with_name("state.json")


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    p = Path(base) / APP
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "history.db"


def load(path: Path | None = None) -> SimpleNamespace:
    p = path or config_path()
    user: dict = {}
    if p.is_file():
        user = tomllib.loads(p.read_text("utf-8"))
    merged = _merge(DEFAULTS, user)
    return _ns(merged)
