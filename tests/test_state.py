"""State derivation, checked against a real capture.

fixtures/capture-2026-10-18.jsonl is the robot's own traffic from 2026-10-18,
reduced to the fields the bridge reads: two app-started jobs that both failed
on a clogged Clean Base. Replaying it builds the accumulated state the way the
robot client does, so these tests see exactly what the bridge would.
"""

import json
from pathlib import Path

from roomba_mqtt_bridge import state

ROOMS = {"1": "Master Bedroom", "3": "Kitchen", "4": "Living Room", "24": "Bathroom",
         "25": "Entryway", "26": "Dining Room", "27": "Laundry Room", "23": "Master Bathroom"}
FIXTURE = Path(__file__).parent / "fixtures" / "capture-2026-10-18.jsonl"


def replay():
    """Yield (time, accumulated reported state) after each message."""
    acc: dict = {}
    for line in FIXTURE.read_text().splitlines():
        m = json.loads(line)
        acc.update(m["reported"])
        yield m["t"], dict(acc)


def at(t):
    last = None
    for ts, rep in replay():
        if ts <= t:
            last = rep
    return last


def test_docked_before_first_job():
    rep = at("10:49:00")
    assert state.dock_state(rep) == ("ready", 301)
    assert state.vacuum_state(rep) == "docked"
    # The bin was already reporting full: on a self-emptying dock that is a
    # problem in its own right, and it was the only early clue.
    assert state.status(rep) == "bin_full"
    assert state.attention(rep) == ["bin_full"]
    assert state.fan_speed(rep) == "Automatic"


def test_emptying_at_job_start():
    rep = at("10:49:30")
    assert rep["cleanMissionStatus"]["phase"] == "evac"
    assert state.vacuum_state(rep) == "docked"
    assert state.mission_state(rep) == "emptying"
    assert state.dock_state(rep)[0] == "emptying"


def test_clog_while_mission_running_shows_cleaning_but_attention():
    rep = at("10:50:07")
    assert state.dock_state(rep) == ("clogged", 351)
    assert state.vacuum_state(rep) == "cleaning"
    assert state.status(rep) == "cleaning"
    assert "dock_clogged" in state.attention(rep)


def test_clogged_after_mission_fails():
    rep = at("10:50:30")
    assert state.vacuum_state(rep) == "docked"
    assert state.status(rep) == "dock_clogged"
    assert state.attention(rep) == ["dock_clogged", "bin_full"]


def test_mission_tracker_reports_both_failures_with_rooms():
    tracker = state.MissionTracker(rooms=ROOMS)
    ended = []
    for t, rep in replay():
        result = tracker.update(rep)
        if result:
            ended.append((t, result))
    assert [r["result"] for _, r in ended] == ["failed", "failed"]
    assert ended[0][1]["rooms"] == ["Living Room"]
    assert ended[1][1]["rooms"] == ["Master Bedroom", "Kitchen", "Living Room", "Bathroom",
                                    "Entryway", "Dining Room", "Laundry Room", "Master Bathroom"]


def test_mission_rooms_survive_querydock_mid_mission():
    tracker = state.MissionTracker(rooms=ROOMS)
    for t, rep in replay():
        tracker.update(rep)
        if t == "10:50:08":  # the app's querydock replaced lastCommand here
            assert rep["lastCommand"]["command"] == "querydock"
            attrs = state.mission_attributes(rep, ROOMS, tracker.start_command)
            assert attrs["rooms"] == ["Living Room"]
            return
    raise AssertionError("fixture changed: no message at 10:50:08")


def test_tracker_baseline_does_not_invent_a_result():
    tracker = state.MissionTracker()
    rep = at("10:49:00")
    assert tracker.update(rep) is None
    assert tracker.update(rep) is None


def test_stuck_outranks_everything():
    rep = at("10:50:30") | {"cleanMissionStatus": {"cycle": "clean", "phase": "stuck", "error": 0}}
    assert state.status(rep) == "stuck"
    assert state.vacuum_state(rep) == "error"
    assert state.attention(rep)[0] == "stuck"


def test_robot_error_code_is_named():
    rep = {"cleanMissionStatus": {"cycle": "clean", "phase": "stop", "error": 17}}
    assert state.status(rep) == "error"
    assert state.attention(rep)[0].startswith("error: ")


def test_unlisted_dock_fault_is_a_problem_not_unknown():
    assert state.dock_state({"dock": {"known": True, "state": 357}}) == ("problem", 357)
    assert state.dock_state({"dock": {"known": False}}) == ("none", None)


def test_charging_vs_docked():
    base = {"cleanMissionStatus": {"cycle": "none", "phase": "charge", "error": 0},
            "dock": {"known": True, "state": 301}, "bin": {"full": False}}
    assert state.status(base | {"batPct": 80}) == "charging"
    assert state.status(base | {"batPct": 100}) == "docked"


def test_every_derived_value_is_a_declared_option():
    for _, rep in replay():
        assert state.status(rep) in state.STATUS_OPTIONS
        assert state.dock_state(rep)[0] in state.DOCK_OPTIONS
        assert state.mission_state(rep) in state.MISSION_OPTIONS
        assert state.vacuum_state(rep) in state.VACUUM_STATES


def _job_state(cmd, cycle, counts):
    return {"lastCommand": cmd, "cleanMissionStatus": {"cycle": cycle, "phase": "run" if cycle != "none" else "charge"},
            "bbmssn": dict(zip(("nMssnOk", "nMssnF", "nMssnC"), counts))}


def test_regionless_start_mid_job_keeps_the_jobs_rooms():
    # 2026-10-18: a Living Room job stopped from the dashboard was recorded as
    # rooms "all". Whatever reported a region-less start mid-job, it must not
    # rewrite the rooms of the job already running.
    tracker = state.MissionTracker(rooms=ROOMS)
    living = {"command": "start", "time": 1, "regions": [{"region_id": "4", "type": "rid"}]}
    plain = {"command": "start", "time": 2, "regions": None}
    tracker.update(_job_state(living, "none", (253, 33, 90)))     # baseline, command just sent
    tracker.update(_job_state(living, "clean", (253, 33, 90)))    # job running
    tracker.update(_job_state(plain, "clean", (253, 33, 90)))     # stray start mid-job
    ended = tracker.update(_job_state(plain, "none", (253, 33, 91)))
    assert ended["result"] == "cancelled"
    assert ended["rooms"] == ["Living Room"]


def test_regionless_start_from_idle_is_a_whole_house_job():
    tracker = state.MissionTracker(rooms=ROOMS)
    plain = {"command": "start", "time": 5, "regions": None}
    tracker.update(_job_state({"command": "dock", "time": 4}, "none", (253, 33, 90)))
    tracker.update(_job_state(plain, "none", (253, 33, 90)))
    tracker.update(_job_state(plain, "clean", (253, 33, 90)))
    ended = tracker.update(_job_state(plain, "none", (254, 33, 90)))
    assert ended["result"] == "ok" and ended["rooms"] == "all"
