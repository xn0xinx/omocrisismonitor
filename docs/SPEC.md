# OmoCrisisMonitor — spec

A themed desktop situational-awareness console. One interactive map carrying
three independent, toggleable data layers plus an AI synthesis sidebar:

1. **AIS shipping** — live vessel positions + detail, viewport-scoped
2. **Conflict events** — GeoConfirmed geolocated events, worldwide, with history
3. **Fuel prices** — crude benchmarks (live) + retail pump prices per country

Built to run as a *second* Chromium `--app` window on an 8 GB Skylake box, so:
lean, event-driven, viewport-limited, no busy-polling.

## Sources (all sanctioned — no MarineTraffic scraping)

| Layer | Source | Access |
|---|---|---|
| AIS | **aisstream.io** | free WebSocket `wss://stream.aisstream.io/v0/stream`, key server-side only, subscribe with bounding boxes. 3 conns/IP, 1 sub-update/s, no history replay. |
| Conflict | **GeoConfirmed** | official API (`geoconfirmed.org` — `/scalar/v1` OpenAPI; legacy KML `/api/map/ExportAsKml/<theatre>`). Exact endpoints pinned in Phase 2. |
| Fuel — crude | free crude-oil API (Brent/WTI) — chosen in Phase 3 | near-real-time |
| Fuel — retail | GlobalPetrolPrices.com-style per-country pump prices | ~weekly, light scrape (personal use), degrade to STALE on failure |

MarineTraffic: no free API, ToS forbids scraping → not used.
`soph120/global-fuel-price-analysis`: unlicensed static Kaggle analysis → not
used; fuel history builds from zero on first run.

## Decisions (Q&A, 2026-09-10)

- **Name/slug:** OmoCrisisMonitor / `omocrisismonitor` (no hyphen, matches `omoclaude`).
- **Stack:** house pattern — Python + FastAPI on `127.0.0.1:8792` + one themed
  Chromium `--app` window + WebSocket push + SQLite history + TOML config +
  Omarchy theme-set hook.
- **Fuel:** both — crude for the live graph, retail-per-country for a world choropleth.
- **AIS scope:** viewport only; re-subscribe on pan/zoom. No global persistent picture.
- **AIS detail:** name, MMSI, IMO, callsign, type, flag (from MMSI), SOG, COG,
  heading, nav status, destination, ETA, draught, dimensions, last-seen + a
  short in-view track trail. No photos / port-call history (paid).
- **Conflict scope:** everything GeoConfirmed publishes, all theatres, all categories.
- **Conflict history:** store a full-dataset snapshot ~daily → scrub the map back in time.
- **Conflict detail:** location+coords, date/time, category, description, source
  links (tweets/video/news), media thumbnails. Dense "intel console" styling.
- **History retention:** forever (`history.prune_after_days = 0`).
- **Compare view:** a **correlation readout** — computed numbers + sparklines
  (e.g. "Red Sea conflict events vs Brent vs Suez transits, 90d"). AIS side is
  limited to regions the user has had open.
- **AI sidebar (v1):** yes. Runs through the `claude` CLI on the user's
  subscription (no API key). Terminal-style pull-out. Content: 24h conflict
  developments by theatre → intersection with shipping lanes / chokepoints
  (ships currently in view) → crude + retail move → short synthesis.
  Refresh: on-demand button **plus** auto when something notable changes,
  no more often than `ai.min_interval_s`.
- **Map:** MapLibre GL + MapTiler vector tiles (free key). Style recoloured
  live from `--ocm-*` theme vars → follows the OS theme exactly.
- **Theme sync:** `omarchy-omocrisismonitor-theme` generator + `theme-set.d`
  hook → `~/.config/omocrisismonitor/theme.css` (`--ocm-*`); server watches
  mtime → `theme` WS event → window swaps `<link>` + re-skins the map.
- **Alerts (v1):** basic watch/alert system — conflict-in-region, ship-in-box,
  fuel threshold → desktop notifications.
- **Source down:** show last-known + a STALE badge with timestamp; errors go to
  a log/status area, not toasts.
- **Window:** single Chromium `--app`, fullscreen-capable, opens on **DP-2**
  (the Hisense — larger display). Layout: map fills window; collapsible edge
  panels (left = layer rail + AI pull-out, right = selection detail, bottom =
  fuel strip + correlation readout). User will iterate on layout.
- **Units:** USD everywhere; fuel in USD/litre; AIS native (knots/nm); all
  timestamps in local Omarchy time.
- **Repo:** `github.com/xn0xinx/omocrisismonitor`, **public**. Commit + push at
  the end of every phase as a review checkpoint.
- **Autonomy:** while the user is away, unforeseen calls → smallest sane option,
  logged in `CHANGELOG.md`, keep moving; flag bigger ones for their return.

## Phasing

| Phase | Deliverable |
|---|---|
| **0** | scaffold, MapLibre map on MapTiler vectors, theme sync, window, SQLite schema, repo + push |
| **1** | AIS layer — server holds the aisstream WS, viewport bbox subscribe, ship markers + detail panel + trail |
| **2** | Conflict layer — GeoConfirmed API, pins, detail panel, daily snapshots, date scrubber |
| **3** | Fuel panel — crude live graph + retail world choropleth, history accrual |
| **4** | AI sidebar — `claude` CLI synthesis, on-demand + auto-on-change |
| **5** | correlation readout + watch/alert rules + notifications |

## Architecture (target)

```
omocrisismonitor/
  __main__.py     argparse · single-instance lock · Chromium --app launch (DP-2) · uvicorn
  config.py       DEFAULTS + TOML deep-merge → SimpleNamespace; XDG paths
  db.py           SQLite (WAL), schema for every phase, meta.schema_version
  server.py       FastAPI: / · /theme.css · /api/* · /ws · Hub fan-out · theme watcher
  ais.py          (P1) aisstream client, bbox manager, per-MMSI throttle → hub
  conflict.py     (P2) GeoConfirmed poll + snapshot writer → hub
  fuel.py         (P3) crude poll + retail scrape → db + hub
  ai.py           (P4) claude CLI runner, change detector
  alerts.py       (P5) rule engine + desktop notify
  web/            index.html · app.css · app.js · theme.css · vendor/maplibre-gl.*
scripts/          omarchy-omocrisismonitor-theme · omocrisismonitor-theme.hook
share/            hyprland-windowrule.conf · omocrisismonitor.desktop
```

WS event types (server→client): `snapshot`, `theme`, `ping`, then per phase
`ais_upsert` / `ais_drop`, `conflict_set` / `conflict_scrub`, `fuel_tick`,
`ai_summary`, `alert_hit`, `stale`.
