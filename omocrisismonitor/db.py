"""SQLite history store. Stdlib sqlite3, WAL, one file at
$XDG_DATA_HOME/omocrisismonitor/history.db.

Schema is created up-front for every phase (tables stay empty until their
phase lands). Bump SCHEMA_VERSION + add an `if v < N` block in `_migrate`
for changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Phase 3: fuel -----------------------------------------------------------
-- crude benchmarks, one row per poll per symbol (brent, wti, ...)
CREATE TABLE IF NOT EXISTS fuel_crude (
    ts       INTEGER NOT NULL,        -- unix seconds
    symbol   TEXT    NOT NULL,
    usd      REAL    NOT NULL,
    source   TEXT,
    PRIMARY KEY (ts, symbol)
);
-- retail pump prices per country, one row per country per weekly refresh
CREATE TABLE IF NOT EXISTS fuel_retail (
    ts        INTEGER NOT NULL,
    country   TEXT    NOT NULL,        -- ISO-3166 alpha-2
    kind      TEXT    NOT NULL,        -- 'gasoline' | 'diesel'
    usd_per_l REAL    NOT NULL,
    source    TEXT,
    PRIMARY KEY (ts, country, kind)
);

-- Phase 2: conflict (GeoConfirmed) -------------------------------------
-- Event store, not daily full snapshots. One row per GeoConfirmed placemark,
-- upserted every refresh; each carries its own event date, so "scrub the map
-- back in time" is just  WHERE date_sort <= T. Detail (description / sources /
-- geolocation) is fetched live per-id on click, not stored.
CREATE TABLE IF NOT EXISTS conflict_event (
    ext_id      TEXT    PRIMARY KEY,   -- GeoConfirmed placemark guid
    conflict    TEXT    NOT NULL,      -- theatre url slug (ukraine, israel, ...)
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    date        TEXT,                  -- event date, ISO (from the feed)
    date_sort   INTEGER,               -- GeoConfirmed's dateSort int, for range scans
    faction_id  INTEGER,
    color       TEXT,                  -- faction colour, "#rrggbb"
    icon        TEXT,                  -- icon path on geoconfirmed.org
    first_seen  INTEGER NOT NULL,      -- unix s, first time we ingested it
    last_seen   INTEGER NOT NULL       -- unix s, last refresh that still carried it
);
CREATE INDEX IF NOT EXISTS ix_conflict_event_date ON conflict_event(date_sort);
CREATE INDEX IF NOT EXISTS ix_conflict_event_conflict ON conflict_event(conflict);

-- per-theatre faction palette, replaced wholesale each refresh
CREATE TABLE IF NOT EXISTS conflict_faction (
    conflict TEXT    NOT NULL,
    id       INTEGER NOT NULL,
    name     TEXT,
    color    TEXT,
    PRIMARY KEY (conflict, id)
);

-- provenance only: one row per theatre per refresh cycle (no full copies)
CREATE TABLE IF NOT EXISTS conflict_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    conflict    TEXT    NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,   -- events carried by this refresh
    added       INTEGER NOT NULL DEFAULT 0    -- of which newly seen
);

-- Phase 1: AIS --------------------------------------------------------------
-- last-known static data per vessel we've seen
CREATE TABLE IF NOT EXISTS ais_vessel (
    mmsi        INTEGER PRIMARY KEY,
    name        TEXT,
    imo         INTEGER,
    callsign    TEXT,
    ship_type   INTEGER,
    dim_a       INTEGER, dim_b INTEGER, dim_c INTEGER, dim_d INTEGER,
    draught     REAL,
    destination TEXT,
    eta         TEXT,
    updated     INTEGER
);
-- sparse position trail for vessels while they were in a viewed area
CREATE TABLE IF NOT EXISTS ais_track (
    mmsi   INTEGER NOT NULL,
    ts     INTEGER NOT NULL,
    lat    REAL NOT NULL,
    lon    REAL NOT NULL,
    sog    REAL,
    cog    REAL,
    PRIMARY KEY (mmsi, ts)
);

-- Phase 5: alerts ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rule (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,            -- 'conflict_region' | 'ship_box' | 'fuel_threshold'
    label    TEXT,
    params   TEXT NOT NULL,            -- json
    enabled  INTEGER NOT NULL DEFAULT 1,
    created  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_hit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id  INTEGER NOT NULL REFERENCES alert_rule(id) ON DELETE CASCADE,
    ts       INTEGER NOT NULL,
    summary  TEXT NOT NULL,
    payload  TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=4000")
    return con


def init(path: Path) -> sqlite3.Connection:
    con = connect(path)
    # meta first so _migrate can read the version; migrations bring any existing
    # tables to the current shape; _SCHEMA then fills in whatever's still missing
    # (fresh install, or a table added in a later phase). Order matters — _SCHEMA
    # assumes current column names.
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    _migrate(con)
    con.executescript(_SCHEMA)
    con.commit()
    return con


def _get_version(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


_MIGRATIONS: dict[int, str] = {
    # v1 -> v2: Phase 2 reworked the conflict tables from the daily-full-snapshot
    # model to an event store. The v1 tables never carried data (Phase 2 hadn't
    # shipped), so just drop and let _SCHEMA above recreate them.
    2: """
        DROP TABLE IF EXISTS conflict_event;
        DROP TABLE IF EXISTS conflict_snapshot;
        CREATE TABLE IF NOT EXISTS conflict_event (
            ext_id      TEXT    PRIMARY KEY,
            conflict    TEXT    NOT NULL,
            lat         REAL    NOT NULL,
            lon         REAL    NOT NULL,
            date        TEXT,
            date_sort   INTEGER,
            faction_id  INTEGER,
            color       TEXT,
            icon        TEXT,
            first_seen  INTEGER NOT NULL,
            last_seen   INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_conflict_event_date ON conflict_event(date_sort);
        CREATE INDEX IF NOT EXISTS ix_conflict_event_conflict ON conflict_event(conflict);
        CREATE TABLE IF NOT EXISTS conflict_faction (
            conflict TEXT    NOT NULL,
            id       INTEGER NOT NULL,
            name     TEXT,
            color    TEXT,
            PRIMARY KEY (conflict, id)
        );
        CREATE TABLE IF NOT EXISTS conflict_snapshot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,
            conflict    TEXT    NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0,
            added       INTEGER NOT NULL DEFAULT 0
        );
    """,
}


def _migrate(con: sqlite3.Connection) -> None:
    v = _get_version(con)
    for target in range(v + 1, SCHEMA_VERSION + 1):
        script = _MIGRATIONS.get(target)
        if script:
            con.executescript(script)
    if v != SCHEMA_VERSION:
        con.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
