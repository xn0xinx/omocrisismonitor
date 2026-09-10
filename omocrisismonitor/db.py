"""SQLite history store. Stdlib sqlite3, WAL, one file at
$XDG_DATA_HOME/omocrisismonitor/history.db.

Schema is created up-front for every phase (tables stay empty until their
phase lands). Bump SCHEMA_VERSION + add an `if v < N` block in `_migrate`
for changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

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

-- Phase 2: conflict -----------------------------------------------------
-- one snapshot per capture (Q9: ~daily); events belong to a snapshot so we
-- can scrub the map back in time.
CREATE TABLE IF NOT EXISTS conflict_snapshot (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conflict_event (
    snapshot_id INTEGER NOT NULL REFERENCES conflict_snapshot(id) ON DELETE CASCADE,
    ext_id      TEXT    NOT NULL,      -- GeoConfirmed id
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    date        TEXT,
    category    TEXT,
    country     TEXT,
    title       TEXT,
    description TEXT,
    sources     TEXT,                  -- json array of urls
    media       TEXT,                  -- json array of urls
    raw         TEXT,                  -- json passthrough of the source record
    PRIMARY KEY (snapshot_id, ext_id)
);
CREATE INDEX IF NOT EXISTS ix_conflict_event_snap ON conflict_event(snapshot_id);

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
    con.executescript(_SCHEMA)
    _migrate(con)
    con.commit()
    return con


def _get_version(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def _migrate(con: sqlite3.Connection) -> None:
    v = _get_version(con)
    # future: if v < 2: con.executescript(...)
    if v != SCHEMA_VERSION:
        con.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
