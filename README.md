# OmoCrisisMonitor

A themed desktop **situational-awareness console** for Omarchy. One interactive
map, three toggleable live layers, and an AI synthesis sidebar:

- **AIS shipping** — real-time vessel positions + detail (viewport-scoped), via [aisstream.io](https://aisstream.io)
- **Conflict events** — worldwide geolocated events with scrub-back history, via [GeoConfirmed](https://geoconfirmed.org)
- **Fuel prices** — crude benchmarks (live) + retail pump prices per country

The map re-skins itself to whatever Omarchy theme is active. Built to run lean
as a second Chromium `--app` window alongside everything else on the box.

> Status: **Phase 0** — shell (map + theme sync + window). See
> [`docs/SPEC.md`](docs/SPEC.md) for the full plan and phase breakdown.

## Setup

```bash
git clone https://github.com/xn0xinx/omocrisismonitor
cd omocrisismonitor
./install.sh                     # runtime venv + launcher + theme hook + seeds config.toml
```

Then put your free keys in `~/.config/omocrisismonitor/config.toml`:

- `map.maptiler_key` — free at [maptiler.com](https://www.maptiler.com/cloud/)
- `ais.api_key` — free at [aisstream.io](https://aisstream.io) (stays server-side)

Launch:

```bash
omocrisismonitor                 # serves 127.0.0.1:8792 + opens the window (DP-2)
omocrisismonitor --no-open -p 8793   # headless, for dev
```

For the window to float / land on the Hisense, append
`share/hyprland-windowrule.conf` to your Hyprland config.

## Dev

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Data sources & terms

Uses only sanctioned access: aisstream.io's free WebSocket, GeoConfirmed's
public API, free crude-oil APIs, and a light personal-use scrape of retail pump
prices (degrades to a STALE badge on failure). **MarineTraffic is not used** —
no free API and its ToS forbids scraping.

MIT licensed. Not affiliated with any data provider.
