"""Correlation readout: the daily aggregation helpers, Pearson r edge cases,
and the end-to-end `readout()` shape including its "no data" notes."""
import pytest

from omocrisismonitor import db
from omocrisismonitor.correlate import (
    DAY, daily_conflict_counts, daily_crude, daily_ship_sightings, pearson, readout,
)

BOX = [[10.0, 40.0], [20.0, 50.0]]          # s,w / n,e
INSIDE = (15.0, 45.0)
OUTSIDE = (60.0, 60.0)


def _con(tmp_path):
    return db.init(tmp_path / "h.db")


def _conflict_row(con, ext_id, lat, lon, day_ts):
    con.execute(
        "INSERT INTO conflict_event(ext_id,conflict,lat,lon,date,date_sort,"
        "faction_id,color,icon,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ext_id, "x", lat, lon, "2026-01-01", day_ts // DAY, 1, "#f00", "", day_ts, day_ts),
    )


def _crude_row(con, symbol, ts, usd):
    con.execute("INSERT INTO fuel_crude(ts,symbol,usd,source) VALUES (?,?,?,?)",
               (ts, symbol, usd, "test"))


def _track_row(con, mmsi, lat, lon, ts):
    con.execute("INSERT INTO ais_track(mmsi,ts,lat,lon,sog,cog) VALUES (?,?,?,?,?,?)",
               (mmsi, ts, lat, lon, 10.0, 90.0))


# ---- pearson --------------------------------------------------------

def test_pearson_perfect_positive():
    assert pearson([(1, 2), (2, 4), (3, 6)]) == pytest.approx(1.0)


def test_pearson_perfect_negative():
    assert pearson([(1, 6), (2, 4), (3, 2)]) == pytest.approx(-1.0)


def test_pearson_needs_at_least_3_points():
    assert pearson([(1, 1), (2, 2)]) is None


def test_pearson_none_for_constant_series():
    assert pearson([(1, 5), (2, 5), (3, 5)]) is None


# ---- daily aggregations ----------------------------------------

def test_daily_conflict_counts_filters_by_box_and_groups_by_day(tmp_path):
    con = _con(tmp_path)
    d0 = 20700 * DAY
    _conflict_row(con, "a", *INSIDE, d0)
    _conflict_row(con, "b", *INSIDE, d0)
    _conflict_row(con, "c", *INSIDE, d0 + DAY)
    _conflict_row(con, "d", *OUTSIDE, d0)          # outside the box
    con.commit()
    out = daily_conflict_counts(con, BOX, d0 - DAY, d0 + 2 * DAY)
    assert out == {d0: 2, d0 + DAY: 1}


def test_daily_crude_last_value_of_day_wins(tmp_path):
    con = _con(tmp_path)
    d0 = 20700 * DAY
    _crude_row(con, "wti", d0 + 1000, 80.0)
    _crude_row(con, "wti", d0 + 5000, 82.5)         # later same day -> wins
    _crude_row(con, "brent", d0 + 1000, 85.0)        # different symbol, ignored
    con.commit()
    out = daily_crude(con, "wti", d0 - DAY, d0 + DAY)
    assert out == {d0: 82.5}


def test_daily_ship_sightings_counts_distinct_vessels_per_day(tmp_path):
    con = _con(tmp_path)
    d0 = 20700 * DAY
    _track_row(con, 111, *INSIDE, d0 + 10)
    _track_row(con, 111, *INSIDE, d0 + 20)          # same vessel, same day -> counted once
    _track_row(con, 222, *INSIDE, d0 + 30)
    _track_row(con, 333, *OUTSIDE, d0 + 10)          # outside the box
    con.commit()
    out = daily_ship_sightings(con, BOX, d0 - DAY, d0 + DAY)
    assert out == {d0: 2}


# ---- end-to-end readout ------------------------------------------

def test_readout_shape_and_notes_when_no_data(tmp_path):
    con = _con(tmp_path)
    r = readout(con, BOX, days=10)
    assert r["bbox"] == BOX and r["days"] == 10
    assert len(r["axis"]) == len(r["series"]["conflict"]) == 11
    assert all(v == 0 for v in r["series"]["conflict"])
    assert r["correlations"] == {
        "conflict_vs_wti": None, "conflict_vs_brent": None, "conflict_vs_ships": None,
    }
    assert any("AIS data" in n for n in r["notes"])
    assert any("crude price" in n for n in r["notes"])


def test_readout_finds_a_correlation_when_data_moves_together(tmp_path):
    con = _con(tmp_path)
    import time
    today = int(time.time())
    day0 = today - (today % DAY) - 5 * DAY
    for i in range(6):
        d = day0 + i * DAY
        # more conflict events on later days, crude rises in step -> strong +r
        for _ in range(i + 1):
            _conflict_row(con, f"e{i}-{_}", *INSIDE, d + 100)
        _crude_row(con, "wti", d + 200, 70.0 + i * 5)
        _track_row(con, 1000 + i, *INSIDE, d + 300)
    con.commit()

    r = readout(con, BOX, days=10)
    assert r["correlations"]["conflict_vs_wti"] > 0.9
    assert r["correlations"]["conflict_vs_ships"] is not None
    assert not any("AIS data" in n for n in r["notes"])
