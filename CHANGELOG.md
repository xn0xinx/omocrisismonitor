# Changelog

## 0.5.1 — 2026-09-10 — pluggable AI backend (Claude / Gemini)

- `ai.py` — reworked to a pluggable `AiBackend` (same pattern as fuel.py's
  `CrudeSource`): `ClaudeBackend` (verified, `claude -p`) and `GeminiBackend`
  (`gemini -p`, folds the system prompt into the user prompt since the CLI has
  no `--system-prompt` flag). **`GeminiBackend.available = False`** — `gemini
  -p` hung indefinitely in testing (45s timeout, twice, no competing process),
  most likely a stale OAuth token trying to refresh rather than failing fast.
  `run()` refuses with a clear message pointing at the fix until someone
  confirms `gemini -p "hi" -o json --approval-mode plan` returns promptly
  after a fresh `gemini` interactive login, then flips the flag. Picking it
  before then fails cleanly via `ai_status` `error` and leaves the last good
  summary untouched — verified live.
- `AiService` gains `backend_key` / `backend` / `list_backends()` /
  `set_backend()`. `cfg.ai.backend` picks the default (falls back to `claude`
  if misconfigured). The existing `runner=` test seam is unchanged and now
  bypasses backend selection entirely (production code never sets it).
- `server.py` — `GET /api/bootstrap`'s `ai` block carries `backend` +
  `backends` (key/label/available); `POST /api/ai/backend {key}` switches
  (422 on an unknown key).
- `web/ai.js` — a backend `<select>` in the drawer header, populated from
  bootstrap, disabled options for unavailable backends; switching posts the
  change and immediately requests a refresh so the pick visibly takes effect.
- `config` / `config.example.toml` — `[ai]` adds `backend`, `gemini_bin`,
  `gemini_model` (empty = CLI default).
- Tests: `test_ai.py` covers backend listing/switching/fallback and the
  gemini refusal; `test_server.py` covers the new bootstrap fields + the
  `/api/ai/backend` route. 75 pass.

## 0.5.0 — 2026-09-10 — Phase 4: AI sidebar

- `ai.py` — `AiService`: a one-shot `claude -p` call through the user's own
  subscription (no API key), tools disabled (`--disallowedTools`), no session
  persistence, `--output-format json` parsed for `result` / `is_error` /
  `total_cost_usd`. Prompt gathers the last day of conflict activity by
  theatre (+ a couple of live-fetched descriptions for the busiest theatres via
  `ConflictService.recent_highlights`), the vessels currently in the AIS
  viewport (`AisService.summary`), and the latest crude + retail freshness
  (`FuelService.snapshot`) — all optional, so the service degrades gracefully
  if a layer is off. Auto-refreshes when a cheap signature (event count / WTI
  price / ship count) changes **and** `ai.min_interval_s` has elapsed;
  always available on demand via `refresh()`. Never crashes the app on a bad
  run — emits `ai_status` `error` and keeps the last good summary. Subprocess
  runner injectable (`runner=`) for tests.
- `ais.py` / `conflict.py` — added `AisService.summary()`/`vessel_count()` and
  `ConflictService.recent_highlights()` as the AI's context sources.
- `server.py` — `AiService` in the lifespan, wired to the other three services.
  `GET /api/ai/summary` (last result + busy flag), `POST /api/ai/refresh`
  (fire-and-forget — a `claude` round trip can take tens of seconds; the result
  rides the `ai_summary` WS event, not the HTTP response). `ai` gate now tracks
  `cfg.ai.enabled` instead of a hardcoded `False`.
- `web/ai.js` — a terminal-style drawer that pulls up from the bottom, full
  width: a status dot (green idle, pulsing amber while thinking), model/reason/
  age/cost meta line, refresh + close buttons, scrollable body. Hydrates any
  existing summary on load without forcing a run; listens for `ai_status` /
  `ai_summary` so it stays live even if another window (or the auto-loop)
  triggered the refresh. Deliberately **not** wired to the shared `dismiss`
  event — that fires on every AIS/conflict marker click and would otherwise
  slam the drawer shut mid-read.
- `index.html` / `app.css` — load `ai.js`, un-gate AI, drawer + terminal
  styling.
- Verified live: `POST /api/ai/refresh` → 6 s → a grounded, non-alarmist,
  ~85-word synthesis correctly citing the real WTI/Brent move, $0.05
  (informational; subscription auth, not billed).
- Tests: `test_ai.py` (change-detection gate, refresh happy path / runner
  error / reentrancy, prompt content, clean start/stop) with fake
  ais/conflict/fuel stand-ins — the real `claude` subprocess path
  (`_default_runner`) is `pragma: no cover`, exercised only by the live smoke
  test above. `test_ais.py` / `test_conflict.py` cover the new context
  methods; `test_server.py` covers the two `/api/ai/*` routes. 68 pass.

## 0.4.0 — 2026-09-10 — Phase 3: Fuel layer (crude + retail choropleth)

- `fuel.py` — `FuelService` with two independent loops:
  - **Crude** (`crude_poll_s`, default 15 min). Pluggable `CrudeSource`:
    `YahooCrude` (default, no key — `query1.finance.yahoo.com/v8/finance/chart`
    for `CL=F`/`BZ=F`, ~15 min delayed, intraday + 1 y history) or `EiaCrude`
    (used when `fuel.eia_key` is set — official EIA API, daily). First run
    backfills ~1 y of daily closes; each tick upserts `fuel_crude` and emits
    `fuel_tick`. Day-over-day % on the tile is computed from the stored series
    (Yahoo's `chartPreviousClose` is window-relative, not "yesterday").
  - **Retail** (`retail_poll_h`, default 24 h). Light scrape of
    globalpetrolprices.com's gasoline + diesel ranking pages — the country list
    and the price bars are parallel ranked sequences, zipped by index
    (`parse_gpp`). Label → ISO2 via `_fuel_geo.GPP_ISO` (~170 hand-mapped
    names); unmapped are dropped and reported. Any fetch failure keeps the
    last-known rows and sets `retail_stale` (→ `fuel_status` `stale`).
  - `httpx` client and crude source both injectable for tests.
- `_fuel_geo.py` — GlobalPetrolPrices country label → ISO-3166 alpha-2 map.
- `server.py` — `FuelService` in the lifespan; `GET /api/fuel/bootstrap`
  (latest crude + retail freshness), `GET /api/fuel/crude?symbol=&days=`
  (stored series for the sparkline), `GET /api/fuel/retail?kind=` (ISO →
  USD/litre for the choropleth). `fuel` gate on.
- `web/fuel.js` — layer module: a left-docked panel (WTI/Brent price tiles with
  day-over-day %, a hand-rolled inline-SVG dual sparkline, a petrol/diesel
  toggle, a price-ramp legend, retail freshness / STALE line) **and** a retail
  pump-price world choropleth — a `fill` layer over the vendored countries
  GeoJSON, recoloured on a semantic green→red ramp, inserted below the AIS /
  conflict overlays. Country click → price popup. Live via `fuel_tick` /
  `fuel_retail_ready` / `fuel_status`.
- `web/vendor/world-countries.geo.json` — Natural Earth 110m admin-0, slimmed to
  `{iso, name}` + 2-dp coords (~170 KB). No CDN.
- `index.html` / `app.css` — load `fuel.js`, un-gate FUEL, panel + legend +
  themed map-popup styling.
- `config` — `[fuel]` adds `eia_key` (optional; empty → Yahoo).
- Tests: `test_fuel.py` (GPP parser, Yahoo + EIA sources, crude tick/backfill,
  retail ISO mapping + unknown-drop, STALE on HTTP error, reads) via
  `MockTransport`; `test_server.py` covers the three `/api/fuel/*` routes +
  bad-param 422s. 53 pass.
- Live smoke: Yahoo WTI/Brent + 1 y backfill, GlobalPetrolPrices scrape → 170
  gasoline / 169 diesel countries mapped to ISO.

## 0.3.1 — 2026-09-10 — Phase 2 polish: intel-panel media

- `web/conflict.js` — the conflict detail panel now surfaces what GeoConfirmed
  actually gives us for media:
  - **Source media-type chip** in the header from the `origin` code
    (VID/PIC/IMG/UAV/SAT/… → "▶ video", "◹ drone", "◍ satellite", …; unknown
    codes pass through).
  - **YouTube thumbnails** — any `youtube.com` / `youtu.be` / `shorts` / `live`
    URL in `originalSource` or `geolocation` renders `img.youtube.com/vi/<id>/
    hqdefault.jpg` with a play overlay, linking out. A broken thumb removes
    itself. This is the only source we can preview without a heavyweight embed —
    GeoConfirmed's detail API returns **no** thumbnail field, and X/Telegram
    have no free preview path, so those stay as labelled link chips.
  - **`gear` + `units`** free-text rows added (e.g. "Bulldozer", "45th Separate
    Artillery Brigade") — previously dropped.
  - Text fields (`name`, `description`, `gear`, `units`, link hostnames) are now
    HTML-escaped on the way into the panel.
- `web/app.css` — chip + thumbnail-strip styling; source link groups left-aligned.

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
