# Changelog

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
