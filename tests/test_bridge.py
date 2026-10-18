import asyncio
import json

import pytest
from roombapy import RoombaScopeError

from pathlib import Path

from roomba_mqtt_bridge.bridge import Bridge, Config
from roomba_mqtt_bridge import state

# Same shape as the ROOMBA_MAP env in the Flux deployment.
ROBOT_MAP = {
    "pmap_id": "v-rKZAMrS_izgoJJqfRnsg",
    "user_pmapv_id": "260411T185825",
    "rooms": [
        {"id": "1", "name": "Master Bedroom", "suction": "Automatic", "passes": 1},
        {"id": "3", "name": "Kitchen", "suction": "Eco", "passes": 1},
        {"id": "4", "name": "Living Room", "suction": "Automatic", "passes": 2},
        {"id": "24", "name": "Bathroom", "suction": "Eco", "passes": 1},
        {"id": "25", "name": "Entryway", "suction": "Eco", "passes": 1},
        {"id": "26", "name": "Dining Room", "suction": "Eco", "passes": 1},
        {"id": "27", "name": "Laundry Room", "suction": "Eco", "passes": 1},
        {"id": "23", "name": "Master Bathroom", "suction": "Eco", "passes": 1},
    ],
}
FIXTURE = Path(__file__).parent / "fixtures" / "capture-2026-10-18.jsonl"


def captured_start_commands():
    """Distinct app start commands; the robot repeats lastCommand in full-state dumps."""
    seen = {}
    for line in FIXTURE.read_text().splitlines():
        cmd = json.loads(line)["reported"].get("lastCommand")
        if cmd and cmd["command"] == "start":
            seen[cmd["time"]] = {k: cmd[k] for k in ("ordered", "pmap_id", "user_pmapv_id", "regions")}
    return list(seen.values())


def make_bridge() -> Bridge:
    # Constructing the robot client does no I/O.
    return Bridge(Config(roomba_host="192.0.2.1", roomba_blid="ABC123", roomba_password="x",
                         mqtt_host="localhost",
                         robot_map=state.RobotMap.from_json(ROBOT_MAP)))


def test_built_commands_match_what_the_app_sent():
    single, every = captured_start_commands()
    m = state.RobotMap.from_json(ROBOT_MAP)
    # Job 1: Living Room, Performance, 2 passes.
    assert m.start_params(["Living Room"], "Performance", 2) == single
    # Job 2: all rooms, each on its saved defaults, in the app's order -
    # requested here in a different order and by a mix of names and ids.
    assert m.start_params(["23", "kitchen", "Master Bedroom", "4", "24", "25", "26", "27"]) == every


@pytest.mark.parametrize("rooms,kw", [
    ([], {}),
    (["Garage"], {}),
    (["Kitchen"], {"suction": "Turbo"}),
    (["Kitchen"], {"passes": 3}),
])
def test_bad_room_requests_are_refused(rooms, kw):
    with pytest.raises(ValueError):
        state.RobotMap.from_json(ROBOT_MAP).start_params(rooms, **kw)


def test_discovery_entity_ids_match_the_card_contract():
    cmps = make_bridge().discovery()["cmps"]
    ids = {c["default_entity_id"] for c in cmps.values()}
    assert ids == {
        "vacuum.roomba", "sensor.roomba_status", "sensor.roomba_dock", "sensor.roomba_mission",
        "sensor.roomba_last_mission", "sensor.roomba_battery", "binary_sensor.roomba_attention",
        "binary_sensor.roomba_bin_full", "binary_sensor.roomba_charging",
    }
    assert len({c["unique_id"] for c in cmps.values()}) == len(cmps)
    assert cmps["status"]["options"] == state.STATUS_OPTIONS
    assert cmps["last_mission"]["value_template"] == "{{ value_json.result }}"
    assert cmps["last_mission"]["state_topic"] == "roomba/last_mission"
    assert cmps["vacuum"]["clean_segments_command_topic"] == "roomba/clean_segments"
    for c in cmps.values():
        assert c["availability_mode"] == "all"
    json.dumps(make_bridge().discovery())  # must serialise


def test_snapshot_carries_vacuum_keys_and_card_fields():
    b = make_bridge()
    rep = {"cleanMissionStatus": {"cycle": "none", "phase": "charge", "error": 0},
           "dock": {"known": True, "state": 351}, "bin": {"full": True}, "batPct": 100,
           "carpetBoost": True, "vacHigh": False}
    snap = b.snapshot(rep)
    assert snap["state"] == "docked" and snap["fan_speed"] == "Automatic"
    assert snap["status"] == "dock_clogged"
    assert snap["attention"] is True
    assert snap["attention_reasons"] == ["dock_clogged", "bin_full"]
    assert snap["charging"] is False
    assert list(snap["segments"].values())[:2] == ["Master Bedroom", "Kitchen"]


def test_clean_rooms_and_segments_send_room_scoped_starts(monkeypatch):
    b = make_bridge()
    sent = []

    async def fake_send(command, params=None):
        sent.append((command, params))

    monkeypatch.setattr(b.robot, "send_command", fake_send)
    # The script's shape, via HA's MQTT vacuum send_command (params merged in).
    asyncio.run(b.handle_send_command(json.dumps(
        {"command": "clean_rooms", "rooms": ["Kitchen"], "suction": "Performance", "passes": "2"})))
    # vacuum.clean_area, after HA maps areas to segment ids.
    asyncio.run(b.handle_segments(json.dumps(["26", "3"])))
    (c1, p1), (c2, p2) = sent
    assert c1 == c2 == "start"
    assert [r["region_id"] for r in p1["regions"]] == ["3"]
    assert p1["regions"][0]["params"]["vacHigh"] is True
    assert p1["regions"][0]["params"]["twoPass"] is True
    assert [r["region_id"] for r in p2["regions"]] == ["3", "26"]
    assert p2["regions"][1]["params"]["carpetBoost"] is False


def test_empty_clean_rooms_never_reaches_the_robot(monkeypatch):
    b = make_bridge()
    sent = []
    monkeypatch.setattr(b.robot, "send_command", lambda *a: sent.append(a))
    for payload in ({"command": "clean_rooms", "rooms": []}, {"command": "clean_rooms"}):
        with pytest.raises(ValueError):
            asyncio.run(b.handle_send_command(json.dumps(payload)))
    with pytest.raises(ValueError):
        asyncio.run(b.handle_segments("[]"))
    assert sent == []


@pytest.mark.parametrize("payload", [
    # HA's MQTT vacuum shape: params merged beside "command".
    {"command": "start", "ordered": 1, "regions": []},
    # Hand-made shape with nested params.
    {"command": "start", "params": {"regions": []}},
])
def test_empty_region_list_is_refused_before_anything_is_sent(payload):
    b = make_bridge()
    with pytest.raises(RoombaScopeError):
        asyncio.run(b.handle_send_command(json.dumps(payload)))


def test_send_command_forwards_ha_shaped_params(monkeypatch):
    b = make_bridge()
    sent = []

    async def fake_send(command, params=None):
        sent.append((command, params))

    monkeypatch.setattr(b.robot, "send_command", fake_send)
    region = {"region_id": "4", "type": "rid", "params": {"vacHigh": True}}
    asyncio.run(b.handle_send_command(json.dumps(
        {"command": "start", "ordered": 1, "pmap_id": "p", "user_pmapv_id": "v", "regions": [region]})))
    asyncio.run(b.handle_send_command("find"))
    assert sent == [
        ("start", {"ordered": 1, "pmap_id": "p", "user_pmapv_id": "v", "regions": [region]}),
        ("find", None),
    ]
