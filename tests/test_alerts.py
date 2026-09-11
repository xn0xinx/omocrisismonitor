"""Alerts: param validation, rule CRUD, each rule kind's evaluation (watermark
for conflict_region, edge-triggering for ship_box/fuel_threshold), and that
one bad rule can't sink the rest of the eval pass. `_default_notify` (the real
`notify-send` subprocess) is never exercised — it would pop a real desktop
notification — only its failure-swallowing is checked, via a monkeypatched
subprocess call.
"""
import asyncio

import pytest

from omocrisismonitor import config, db
from omocrisismonitor.alerts import AlertService, validate_params


def _cfg(tmp_path, **alerts):
    c = config.load(tmp_path / "none.toml")
    for k, v in alerts.items():
        setattr(c.alerts, k, v)
    return c


class FakeHub:
    def __init__(self):
        self.events = []

    def emit(self, e):
        self.events.append(e)

    def of(self, t):
        return [e for e in self.events if e["type"] == t]


class FakeAis:
    def __init__(self, count=0):
        self.count = count

    def count_in_box(self, bbox, category=None):
        return self.count


class FakeFuel:
    def __init__(self, usd=None):
        self.usd = usd

    def snapshot(self):
        crude = {"wti": {"usd": self.usd}} if self.usd is not None else {}
        return {"crude": crude}


def _svc(tmp_path, ais=None, fuel=None, notifier=None, **cfg_kw):
    con = db.init(tmp_path / "h.db")
    hub = FakeHub()

    async def default_notifier(title, body):
        pass

    svc = AlertService(
        _cfg(tmp_path, **cfg_kw), hub, con,
        ais=ais or FakeAis(), conflict=object(), fuel=fuel or FakeFuel(),
        notifier=notifier or default_notifier,
    )
    return svc, hub, con


def _insert_conflict_event(con, ext_id, conflict, lat, lon, first_seen):
    con.execute(
        "INSERT INTO conflict_event(ext_id,conflict,lat,lon,date,date_sort,"
        "faction_id,color,icon,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ext_id, conflict, lat, lon, "2026-09-10", 20706, 1, "#f00", "", first_seen, first_seen),
    )
    con.commit()


# ---- param validation --------------------------------------------

def test_validate_conflict_region_requires_conflict_or_bbox():
    with pytest.raises(ValueError):
        validate_params("conflict_region", {})
    p = validate_params("conflict_region", {"conflict": "ukraine"})
    assert p == {"conflict": "ukraine", "bbox": None, "min_new": 1}


def test_validate_ship_box_requires_bbox():
    with pytest.raises(ValueError):
        validate_params("ship_box", {"category": "tanker"})
    p = validate_params("ship_box", {"bbox": [[1, 2], [3, 4]], "min_count": 2})
    assert p == {"bbox": [[1, 2], [3, 4]], "category": "any", "min_count": 2}


def test_validate_fuel_threshold_requires_symbol_op_value():
    with pytest.raises(ValueError):
        validate_params("fuel_threshold", {"symbol": "wti"})
    with pytest.raises(ValueError):
        validate_params("fuel_threshold", {"symbol": "gold", "op": ">", "value": 1})
    p = validate_params("fuel_threshold", {"symbol": "brent", "op": "<", "value": "80"})
    assert p == {"symbol": "brent", "op": "<", "value": 80.0}


def test_validate_unknown_kind():
    with pytest.raises(ValueError):
        validate_params("weather", {})


# ---- rule CRUD ----------------------------------------------------

def test_create_list_patch_delete(tmp_path):
    svc, _, _ = _svc(tmp_path)
    rule = svc.create_rule("fuel_threshold", "WTI spike",
                           {"symbol": "wti", "op": ">", "value": 100})
    assert rule["id"] and rule["enabled"] is True
    assert svc.list_rules() == [rule]
    assert svc.set_enabled(rule["id"], False) is True
    assert svc.get_rule(rule["id"])["enabled"] is False
    assert svc.set_enabled(999, False) is False           # unknown -> False, not an error
    assert svc.delete_rule(rule["id"]) is True
    assert svc.list_rules() == []


def test_create_rejects_invalid_params(tmp_path):
    svc, _, _ = _svc(tmp_path)
    with pytest.raises(ValueError):
        svc.create_rule("ship_box", "bad", {})


# ---- conflict_region: watermarked, not edge-triggered -------------

async def test_conflict_region_first_eval_arms_without_firing(tmp_path):
    svc, hub, con = _svc(tmp_path)
    rule = svc.create_rule("conflict_region", "Ukraine watch", {"conflict": "ukraine"})
    _insert_conflict_event(con, "e1", "ukraine", 50, 30, first_seen=1)
    await svc._evaluate(svc.get_rule(rule["id"]))
    assert hub.of("alert_hit") == []                       # backlog never replayed


async def test_conflict_region_fires_on_new_events_since_watermark(tmp_path):
    svc, hub, con = _svc(tmp_path)
    rule = svc.create_rule("conflict_region", "Ukraine watch",
                           {"conflict": "ukraine", "min_new": 2})
    await svc._evaluate(svc.get_rule(rule["id"]))          # arm
    now = svc._watermarks[rule["id"]]
    _insert_conflict_event(con, "old", "ukraine", 50, 30, first_seen=now - 100)  # before watermark
    _insert_conflict_event(con, "e1", "ukraine", 50, 30, first_seen=now + 10)
    _insert_conflict_event(con, "e2", "venezuela", 10, -66, first_seen=now + 10)  # wrong theatre
    await svc._evaluate(svc.get_rule(rule["id"]))
    assert hub.of("alert_hit") == []                       # only 1 matching new event, min_new=2

    _insert_conflict_event(con, "e3", "ukraine", 50, 30, first_seen=now + 20)
    await svc._evaluate(svc.get_rule(rule["id"]))
    hits = hub.of("alert_hit")
    assert len(hits) == 1 and "2 new conflict events" in hits[0]["summary"]


# ---- ship_box: edge-triggered -------------------------------------

async def test_ship_box_fires_once_per_crossing(tmp_path):
    ais = FakeAis(count=0)
    svc, hub, _ = _svc(tmp_path, ais=ais)
    rule = svc.create_rule("ship_box", "Strait watch",
                           {"bbox": [[0, 0], [1, 1]], "min_count": 2})
    r = svc.get_rule(rule["id"])

    ais.count = 3
    await svc._evaluate(r)
    ais.count = 3
    await svc._evaluate(r)                                  # still over -> no re-fire
    ais.count = 1
    await svc._evaluate(r)                                  # drops below -> disarm
    ais.count = 4
    await svc._evaluate(r)                                  # crosses again -> fires

    assert len(hub.of("alert_hit")) == 2


# ---- fuel_threshold: edge-triggered --------------------------------

async def test_fuel_threshold_fires_once_per_crossing(tmp_path):
    fuel = FakeFuel(usd=90.0)
    svc, hub, _ = _svc(tmp_path, fuel=fuel)
    rule = svc.create_rule("fuel_threshold", "WTI spike",
                           {"symbol": "wti", "op": ">", "value": 100})
    r = svc.get_rule(rule["id"])

    await svc._evaluate(r)                                  # below -> nothing
    fuel.usd = 105.0
    await svc._evaluate(r)                                  # crosses -> fires
    await svc._evaluate(r)                                  # still over -> no re-fire
    fuel.usd = 95.0
    await svc._evaluate(r)                                  # disarm
    fuel.usd = 110.0
    await svc._evaluate(r)                                  # fires again

    hits = hub.of("alert_hit")
    assert len(hits) == 2
    assert "WTI $105.00 > $100.00" in hits[0]["summary"]


async def test_fuel_threshold_no_data_is_a_noop(tmp_path):
    svc, hub, _ = _svc(tmp_path, fuel=FakeFuel(usd=None))
    rule = svc.create_rule("fuel_threshold", "x", {"symbol": "wti", "op": ">", "value": 1})
    await svc._evaluate(svc.get_rule(rule["id"]))
    assert hub.of("alert_hit") == []


# ---- firing side effects + resilience ------------------------------

async def test_fire_writes_hit_emits_hub_and_notifies(tmp_path):
    calls = []

    async def notifier(title, body):
        calls.append((title, body))

    svc, hub, con = _svc(tmp_path, notifier=notifier)
    rule = svc.create_rule("fuel_threshold", "WTI spike",
                           {"symbol": "wti", "op": ">", "value": 1})
    await svc._fire(rule, "test summary", {"usd": 2})

    row = con.execute("SELECT * FROM alert_hit WHERE rule_id=?", (rule["id"],)).fetchone()
    assert row["summary"] == "test summary"
    assert hub.of("alert_hit")[0]["label"] == "WTI spike"
    assert calls == [("WTI spike", "test summary")]
    assert svc.recent_hits()[0]["summary"] == "test summary"


async def test_notify_disabled_skips_notifier(tmp_path):
    calls = []

    async def notifier(title, body):
        calls.append(1)

    svc, hub, _ = _svc(tmp_path, notifier=notifier, notify=False)
    rule = svc.create_rule("fuel_threshold", "x", {"symbol": "wti", "op": ">", "value": 1})
    await svc._fire(rule, "s", {})
    assert calls == [] and hub.of("alert_hit")


async def test_evaluate_all_one_bad_rule_does_not_sink_others(tmp_path):
    fuel = FakeFuel(usd=200.0)
    svc, hub, con = _svc(tmp_path, fuel=fuel)
    good = svc.create_rule("fuel_threshold", "ok", {"symbol": "wti", "op": ">", "value": 1})
    bad = svc.create_rule("fuel_threshold", "ok2", {"symbol": "wti", "op": ">", "value": 1})
    # bypass validate_params to land a value that blows up the comparison at
    # eval time (valid JSON, so list_rules() itself is unaffected) — this is
    # the isolation evaluate_all() is meant to survive.
    con.execute(
        "UPDATE alert_rule SET params=? WHERE id=?",
        ('{"symbol":"wti","op":">","value":"not-a-number"}', bad["id"]),
    )
    con.commit()

    await svc.evaluate_all()
    assert hub.of("alert_hit")                                 # the good rule still fired
    assert hub.of("alert_status")[-1]["rule_id"] == bad["id"]   # the bad one reported, not raised


async def test_default_notify_swallows_missing_binary(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("no notify-send")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    svc, _, _ = _svc(tmp_path)
    await svc._default_notify("t", "b")     # must not raise
