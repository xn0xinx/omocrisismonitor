from omocrisismonitor import db


def test_init_creates_schema(tmp_path):
    con = db.init(tmp_path / "h.db")
    names = {
        r["name"]
        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for t in (
        "meta",
        "fuel_crude",
        "fuel_retail",
        "conflict_snapshot",
        "conflict_event",
        "ais_vessel",
        "ais_track",
        "alert_rule",
        "alert_hit",
    ):
        assert t in names, t
    v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert int(v["value"]) == db.SCHEMA_VERSION
    con.close()


def test_init_is_idempotent(tmp_path):
    p = tmp_path / "h.db"
    db.init(p).close()
    con = db.init(p)  # second run must not raise
    con.execute("INSERT INTO fuel_crude(ts,symbol,usd) VALUES (1,'brent',80.5)")
    con.commit()
    assert con.execute("SELECT COUNT(*) c FROM fuel_crude").fetchone()["c"] == 1
    con.close()
