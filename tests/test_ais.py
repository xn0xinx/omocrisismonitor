"""AIS layer: the pure reference data, the frame parser / vessel merge, the
viewport→subscription logic, and one end-to-end run over an injected socket.
No network — `AisService` takes a `connector=` seam for exactly this.
"""
import asyncio
import json
from pathlib import Path

from omocrisismonitor import config
from omocrisismonitor.ais import AisService
from omocrisismonitor.aismeta import flag_for_mmsi, type_info


def _cfg():
    c = config.load(Path("/nonexistent/omocrisismonitor.toml"))  # DEFAULTS only
    c.ais.api_key = "TESTKEY"
    c.ais.throttle_ms = 120          # keep the flush loop snappy for tests
    return c


class FakeHub:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def of(self, t):
        return [e for e in self.events if e["type"] == t]


class FakeWS:
    """Async-iterable stand-in for a websockets connection."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self._drained = asyncio.Event()

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self._drained.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        await self._drained.wait()   # block instead of reconnect-spinning
        raise StopAsyncIteration


def _connector_for(ws):
    async def _c(url):
        return ws
    return _c


def _pos(mmsi, lat, lon, **kw):
    body = {
        "Latitude": lat, "Longitude": lon, "Sog": 12.4, "Cog": 91.0,
        "TrueHeading": 90, "NavigationalStatus": 0,
    }
    body.update(kw)
    return json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": mmsi, "ShipName": "TEST SHIP"},
        "Message": {"PositionReport": body},
    })


def _static(mmsi, name="TEST SHIP", typ=70):
    return json.dumps({
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": mmsi},
        "Message": {"ShipStaticData": {
            "Name": name, "Type": typ, "ImoNumber": 9111111, "CallSign": "TST1",
            "Destination": "HAMBURG", "MaximumStaticDraught": 8.5,
            "Dimension": {"A": 120, "B": 60, "C": 12, "D": 12},
            "Eta": {"Month": 6, "Day": 15, "Hour": 12, "Minute": 30},
        }},
    })


# ---- aismeta ------------------------------------------------------------

def test_flag_from_mmsi_mid():
    assert flag_for_mmsi(211234567) == "DE"    # 211 → Germany
    assert flag_for_mmsi(367123456) == "US"
    assert flag_for_mmsi("235999999") == "GB"


def test_flag_strips_service_prefixes_and_handles_junk():
    assert flag_for_mmsi("002110000") == "DE"  # 00 = coast station, then 211
    assert flag_for_mmsi(0) == ""
    assert flag_for_mmsi(None) == ""
    assert flag_for_mmsi(999999999) == ""      # unknown MID


def test_type_info_buckets():
    assert type_info(70) == ("Cargo", "cargo")
    assert type_info(84) == ("Tanker", "tanker")
    assert type_info(30) == ("Fishing", "fishing")
    assert type_info(60) == ("Passenger", "passenger")
    assert type_info(None) == ("Unknown", "other")


# ---- frame parsing / vessel merge ------------------------------------

def test_ingest_merges_position_then_static():
    svc = AisService(_cfg(), FakeHub(), None)
    svc._ingest(_pos(211234567, 53.55, 8.12))
    svc._ingest(_static(211234567, name="MV POTATO", typ=80))

    v = svc.vessel(211234567)
    assert v["name"] == "MV POTATO"
    assert v["flag"] == "DE"
    assert v["type_label"] == "Tanker" and v["cat"] == "tanker"
    assert v["callsign"] == "TST1"
    assert v["length"] == 180 and v["beam"] == 24      # A+B, C+D
    assert v["navstat_label"] == "Under way (engine)"
    assert 211234567 in svc._dirty                      # position → flush queue


def test_ingest_ignores_null_island_and_bad_json():
    svc = AisService(_cfg(), FakeHub(), None)
    svc._ingest("not json at all")
    svc._ingest(_pos(211000000, 0.0, 0.0))             # 0,0 = no fix
    assert svc.vessel(211000000) is None or 211000000 not in svc._dirty


# ---- viewport → subscription --------------------------------------

def test_subscription_json_shape():
    svc = AisService(_cfg(), FakeHub(), None)
    svc.set_view([[50.0, 5.0], [56.0, 12.0]])
    sub = json.loads(svc._subscription())
    assert sub["APIKey"] == "TESTKEY"
    assert sub["BoundingBoxes"] == [[[50.0, 5.0], [56.0, 12.0]]]
    assert set(sub["FilterMessageTypes"]) == {"PositionReport", "ShipStaticData"}


def test_span_guard_blocks_giant_viewport():
    hub = FakeHub()
    svc = AisService(_cfg(), hub, None)
    svc.set_view([[-80.0, -170.0], [80.0, 170.0]])
    assert svc._subscription() is None
    assert svc.state == "zoomed_out"
    assert hub.of("ais_status")[-1]["state"] == "zoomed_out"


def test_set_view_reports_connecting_when_viewport_becomes_usable():
    hub = FakeHub()
    svc = AisService(_cfg(), hub, None)
    svc.set_view([[-80.0, -170.0], [80.0, 170.0]])   # too big → zoomed_out
    assert svc.state == "zoomed_out"
    svc.set_view([[35.0, 10.0], [42.0, 20.0]])       # usable → connecting now
    assert svc.state == "connecting"
    assert hub.of("ais_status")[-1] == {"type": "ais_status", "state": "connecting", "count": 0}


# ---- end to end over an injected socket ------------------------------

async def test_stream_roundtrip_emits_upsert_and_subscribes():
    hub = FakeHub()
    ws = FakeWS([_pos(211234567, 53.55, 8.12), _static(211234567, typ=70)])
    svc = AisService(_cfg(), hub, None, connector=_connector_for(ws))
    svc.set_view([[50.0, 5.0], [56.0, 12.0]])
    await svc.start()
    try:
        for _ in range(50):
            if hub.of("ais_upsert"):
                break
            await asyncio.sleep(0.05)
        assert hub.of("ais_upsert"), "no ais_upsert within 2.5s"
        vessels = hub.of("ais_upsert")[-1]["vessels"]
        assert vessels[0]["mmsi"] == 211234567
        assert vessels[0]["flag"] == "DE"
        assert ws.sent, "subscription frame was never sent"
        assert "BoundingBoxes" in json.loads(ws.sent[0])
        assert svc.state == "live"
    finally:
        await svc.stop()


async def test_stop_is_clean_when_idle():
    svc = AisService(_cfg(), FakeHub(), None, connector=_connector_for(FakeWS([])))
    await svc.start()
    await svc.stop()          # must not raise even though nothing ever connected
