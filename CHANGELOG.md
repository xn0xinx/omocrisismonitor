# Changelog

## 0.3.0 — 2026-09-10 — Phase 2: Conflict layer (GeoConfirmed)

- `conflict.py` — `ConflictService`: periodic (`refresh_h`, default 6 h) server-side
  pull of every tracked GeoConfirmed theatre's GeoJSON feed. **Event store, not
  daily snapshots** — each placemark is one id-keyed row carrying its own event
  date, so "scrub back in time" is a `date_sort BETWEEN` filter. `dateSort` =
  days since 1970-01-01 (verified against the live feed). Ingest is Point-only,
  drops events older than `window_days × 2`, upserts (keeps `first_seen`), stores
  the faction palette per theatre, writes a provenance-only `conflict_snapshot`
  row (counts, no copies). One dead theatre doesn't sink the refresh. Detail
  (`description` / `originalSource` / `geolocation` / `plusCode`) is fetched live
  per-id on click and memory-cached — never stored. `httpx` client injectable
  (`client=`) for tests.
- GeoConfirmed v2 API pinned: `GET /api/Conflict`,
  `GET /api/Placemark/{slug}/geojson` (`{factionMeta, geojson}`),
  `GET /api/Placemark/detail/{id}`. Public reads, no key. Historical theatres
  (`wwi`, `wwii`) skipped unless named in `conflict.theatres`.
- `db.py` — **SCHEMA_VERSION 2**. Reworked `conflict_event` / `conflict_snapshot`
  from the snapshot model to the event store, added `conflict_faction`. Proper
  `_MIGRATIONS` map + a `meta`-first / migrate / `_SCHEMA` init order so a v1 DB
  upgrades cleanly (the v1 conflict tables never held data).
- `server.py` — `ConflictService` in the lifespan; `GET /api/conflict/bootstrap`
  (theatres + counts + faction palettes + date span), `GET /api/conflict/events`
  (`?until=&since=&conflicts=` — day-int or ISO date → trailing-window GeoJSON
  slice), `GET /api/conflict/detail/{id}` (proxied, 404 on unknown),
  `POST /api/conflict/refresh` (manual kick). `conflict` gate on; bootstrap
  carries `window_days` / `refresh_h`.
- `web/conflict.js` — layer module: clustered pins coloured by faction, cluster
  expansion on click, dot → `/api/conflict/detail` → intel panel (description,
  date, faction, origin, plus code, source + geolocation links). **Date
  scrubber** (`#scrubber`, above the status line): range slider from
  `floor_sort` to today, ● LIVE toggle, debounced refetch on drag, amber "history"
  state when wound back. Refetches on the `conflict_refresh` WS event while live.
- `web/ais.js` / `conflict.js` — layers cooperate over the shared `#detail`
  panel: selecting in one dispatches `dismiss` so the other releases it.
- `config.py` / `config.example.toml` — `[conflict]` reworked: `api_base`,
  `refresh_h`, `window_days` (default 90 — what's drawn / how far the scrubber
  winds; history still accrues forever in the DB), `theatres` ([] = all active).
  Dropped `poll_s` / `snapshot_every_h`.
- `index.html` / `app.css` — load `conflict.js`, un-gate the CONFLICT rail button,
  second status badge, scrubber + intel-panel styling.
- Tests: `test_conflict.py` (theatre filter, ingest window/upsert/idempotency,
  dead-theatre resilience, trailing-window slice, bootstrap counts + span,
  detail cache) via a `MockTransport`; `test_server.py` covers the four
  `/api/conflict/*` routes + ISO-date params + the v2 gate. 39 pass.
- Live smoke test against GeoConfirmed: pulled iran + ven, scrubbed by day-int
  and ISO date, fetched real placemark detail.

## 0.2.0 — 2026-09-10 — Phase 1: AIS layer

- `ais.py` — `AisService`: one long-lived aisstream.io WebSocket subscribed to
  the window's current viewport bounding box. Merges `PositionReport` +
  `ShipStaticData` per MMSI, coalesces per-vessel updates on a `throttle_ms`
  timer, sweeps ships unheard for `stale_after_s`, caps the live set
  (`MAX_VESSELS`, LRU evict), and persists static rows + a sparse (≥60 s)
  position trail to `ais_vessel` / `ais_track`. Emits `ais_upsert` / `ais_drop`
  / `ais_status` on the hub. Reconnects with capped backoff; honours the
  aisstream rules (1 connection, ≤1 resubscribe/s). Socket is injectable
  (`connector=`) for tests.
- `aismeta.py` — pure reference data: ITU MID → flag (abridged to seagoing
  flags) and AIS ship-type code → `(label, category)`; nav-status table.
- `server.py` — `AisService` started/stopped in the lifespan; `POST /api/view`
  (viewport bbox → subscription, with a potato span-guard → `zoomed_out`),
  `GET /api/vessel/{mmsi}` (full detail record); `bootstrap` flips the `ais`
  gate on and carries `throttle_ms` / `stale_after_s`.
- `web/ais.js` — MapLibre layer module hanging off `window.OCM` + the `hub`:
  vessel dots colour-coded by category, heading arrows (moving only), name
  labels at high zoom, click-to-select detail panel (`#detail`, right edge) +
  live trail, rail toggle, and a status badge (`#ais-badge`) for
  connecting / zoom-in / link-down / stale. Debounced `moveend` → `/api/view`.
- `web/app.js` — theme baking refactored into `layerPaint` / `bakeStyle` and a
  `reskinMap` that re-tints in place (no `setStyle`, so custom layers survive);
  added a `window.OCM` bridge + `map-ready` / `view-changed` / `theme-applied`
  / `dismiss` hub events for layer modules.
- `web/index.html` / `app.css` — load `ais.js`, un-gate the AIS rail button,
  add the detail panel + badge and their Tokyo-night styling.
- Fix: nav status `0` ("under way, engine") was being coerced to `15`
  ("undefined") by a truthiness check in `Vessel.public()`.
- Fix: `/api/view` now reports `connecting` synchronously when the viewport
  becomes usable instead of returning a stale state until `_run` reacts.
- Tests: `test_ais.py` (flag/type tables, frame parse + vessel merge, null-island
  reject, subscription JSON, span guard, state transition, end-to-end run over a
  fake socket, clean idle stop); `test_server.py` covers `/api/view` accept /
  span-guard / null / malformed and `/api/vessel` 404. 25 pass.

## 0.1.0 — 2026-09-10 — Phase 0: shell

- Scaffolded in the house pattern (FastAPI + Chromium `--app` + WS + theme hook,
  hatchling/pytest, MIT, author xn0xinx).
- `config.py` — DEFAULTS deep-merged with `~/.config/omocrisismonitor/config.toml`
  → nested `SimpleNamespace`; XDG paths; `state.json` / `history.db` locations.
- `db.py` — SQLite (WAL) with the full schema for every phase (fuel_crude,
  fuel_retail, conflict_snapshot/event, ais_vessel/track, alert_rule/hit) and
  `meta.schema_version` migrations.
- `server.py` — `/`, `/theme.css` (user sheet → bundled fallback), `/api/health`,
  `/api/bootstrap` (MapTiler key + view + phase gates), `/ws` with a `Hub`
  fan-out + `snapshot`/`theme`/`ping` events, 1 s theme-file mtime watcher.
- `__main__.py` — single-instance pid lock, Chromium `--app` launch with a
  best-effort `hyprctl movewindow mon:DP-2` (Hisense), uvicorn boot.
- `web/` — MapLibre GL (vendored) full-window map on MapTiler `dataviz-dark`,
  recoloured live from `--ocm-*` theme vars (background/water/land/roads/labels);
  reconnecting WS; top bar (link pill, view readout, clock, fullscreen);
  left layer rail (AIS/CONFLICT/FUEL/AI/ALERTS, disabled until their phase);
  status line.
- `scripts/omarchy-omocrisismonitor-theme` + `theme-set.d` hook, `--ocm-*` vars,
  bundled Tokyo-night fallback.
- Config seeded with the MapTiler + aisstream keys (mode 0600, git-ignored).
- Tests: config merge/paths, db schema/idempotency, server routes + WS snapshot.
- `docs/SPEC.md` captures the full Q&A, sources, and phase plan.
