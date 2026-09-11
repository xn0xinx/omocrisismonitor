"""Correlation readout (Phase 5): pure read + compute over data already sitting
in the DB — no new external source. Given a bounding box and a day window,
aligns daily conflict-event counts, crude closes, and distinct-vessel
sightings onto a common day axis and reports a Pearson r between conflict and
each of the others (e.g. "Red Sea conflict events vs Brent vs Suez transits").

AIS coverage is only ever whatever the box happened to have open while the
window elapsed (`ais_track` is populated solely by viewports the user actually
viewed — see ais.py's span guard) — an all-zero ships series is expected, not
an error, and gets a note instead of being hidden.

No service/class needed — one entry point, `readout(con, bbox, days)`, called
straight from the server route.
"""
from __future__ import annotations

import time

DAY = 86400


def _day_start(ts: int) -> int:
    return ts - (ts % DAY)


def daily_conflict_counts(con, bbox: list[list[float]], since_ts: int, until_ts: int) -> dict[int, int]:
    (s, w), (n, e) = bbox
    since_sort, until_sort = int(since_ts // DAY), int(until_ts // DAY)
    rows = con.execute(
        "SELECT date_sort, COUNT(*) c FROM conflict_event "
        "WHERE date_sort BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
        "GROUP BY date_sort",
        (since_sort, until_sort, s, n, w, e),
    ).fetchall()
    return {r["date_sort"] * DAY: r["c"] for r in rows}


def daily_crude(con, symbol: str, since_ts: int, until_ts: int) -> dict[int, float]:
    rows = con.execute(
        "SELECT ts, usd FROM fuel_crude WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts",
        (symbol, since_ts, until_ts),
    ).fetchall()
    out: dict[int, float] = {}
    for r in rows:                      # ascending ts -> last close of the day wins
        out[_day_start(r["ts"])] = r["usd"]
    return out


def daily_ship_sightings(con, bbox: list[list[float]], since_ts: int, until_ts: int) -> dict[int, int]:
    (s, w), (n, e) = bbox
    rows = con.execute(
        "SELECT ts, mmsi FROM ais_track WHERE ts BETWEEN ? AND ? "
        "AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (since_ts, until_ts, s, n, w, e),
    ).fetchall()
    by_day: dict[int, set] = {}
    for r in rows:
        by_day.setdefault(_day_start(r["ts"]), set()).add(r["mmsi"])
    return {d: len(v) for d, v in by_day.items()}


def pearson(pairs: list[tuple[float, float]]) -> float | None:
    """None below 3 points or a constant series (undefined / meaningless r)."""
    n = len(pairs)
    if n < 3:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def _paired(a: list, b: list) -> list[tuple[float, float]]:
    return [(x, y) for x, y in zip(a, b) if x is not None and y is not None]


def readout(con, bbox: list[list[float]], days: int = 90) -> dict:
    now = int(time.time())
    since = now - days * DAY
    axis_start = _day_start(since)
    axis = list(range(axis_start, _day_start(now) + DAY, DAY))

    conflict = daily_conflict_counts(con, bbox, since, now)
    wti = daily_crude(con, "wti", since, now)
    brent = daily_crude(con, "brent", since, now)
    ships = daily_ship_sightings(con, bbox, since, now)

    conflict_s = [conflict.get(d, 0) for d in axis]
    wti_s = [wti.get(d) for d in axis]
    brent_s = [brent.get(d) for d in axis]
    ships_s = [ships.get(d, 0) for d in axis]

    r_wti = pearson(_paired(conflict_s, wti_s))
    r_brent = pearson(_paired(conflict_s, brent_s))
    r_ships = pearson(_paired(conflict_s, ships_s)) if any(ships_s) else None

    notes = []
    if not any(ships_s):
        notes.append(
            "no AIS data for this box — only regions you've had the map open "
            "over the window are covered"
        )
    if all(v is None for v in wti_s):
        notes.append("no crude price history for this window yet")

    return {
        "bbox": bbox, "days": days, "axis": axis,
        "series": {"conflict": conflict_s, "wti": wti_s, "brent": brent_s, "ships": ships_s},
        "correlations": {
            "conflict_vs_wti": r_wti, "conflict_vs_brent": r_brent, "conflict_vs_ships": r_ships,
        },
        "notes": notes,
    }
