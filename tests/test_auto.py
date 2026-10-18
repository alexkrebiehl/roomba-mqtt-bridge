from roomba_mqtt_bridge.auto import LOST_END_TIMEOUT, START_TIMEOUT, AutoRun, RobotView

ORDER = ["Living Room", "Laundry Room", "Kitchen", "Dining Room", "Entryway"]

DOCKED = RobotView(mission="idle", on_dock=True, not_ready=0, job_rooms=None)


def cleaning(room, mission="running"):
    return RobotView(mission=mission, on_dock=False, not_ready=0, job_rooms=[room])


def ended(room, result="ok"):
    return {"result": result, "rooms": [room]}


def run_room(auto, room, result="ok", t=0.0):
    """Drive one room job: start it, see it on the floor, see it end."""
    actions = auto.on_update(DOCKED, None, t)
    assert actions == [("start", room)], (auto.phase, actions)
    auto.on_update(cleaning(room), None, t + 1)
    assert auto.phase == "cleaning"
    return auto.on_update(RobotView("idle", True, 68, None), ended(room, result), t + 2)


def test_full_run_cleans_the_order_then_starts_over_next_time():
    auto = AutoRun(ORDER)
    auto.start()
    for room in ORDER:
        run_room(auto, room)
    assert not auto.active and auto.phase == "off"
    assert auto.index == 0 and auto.room == "Living Room"


def test_cancel_mid_room_sends_it_home_and_resumes_that_room_with_wrap():
    auto = AutoRun(ORDER)
    auto.start()
    for room in ORDER[:3]:
        run_room(auto, room)
    assert auto.on_update(DOCKED, None, 10) == [("start", "Dining Room")]
    auto.on_update(cleaning("Dining Room"), None, 11)
    assert auto.cancel(cleaning("Dining Room")) == [("dock",)]
    # The dock ends the job as cancelled; auto is already off and ignores it.
    auto.on_update(RobotView("idle", True, 0, None), ended("Dining Room", "cancelled"), 12)
    assert not auto.active and auto.room == "Dining Room"
    # Next departure: Dining, Entryway, Living, Laundry, Kitchen.
    auto.start()
    for room in ["Dining Room", "Entryway", "Living Room", "Laundry Room", "Kitchen"]:
        run_room(auto, room, t=20)
    assert not auto.active and auto.room == "Living Room"


def test_cancel_while_waiting_between_rooms_does_not_dock_and_resumes_next_room():
    auto = AutoRun(ORDER)
    auto.start()
    run_room(auto, "Living Room")
    # Robot back on the dock, not ready yet (post-job spell): auto waits.
    assert auto.on_update(RobotView("idle", True, 68, None), None, 5) == []
    assert auto.cancel(RobotView("idle", True, 68, None)) == []
    assert auto.room == "Laundry Room"


def test_failed_room_is_retried_next_run():
    auto = AutoRun(ORDER)
    auto.start()
    run_room(auto, "Living Room")
    run_room(auto, "Laundry Room", result="failed")
    assert not auto.active and auto.room == "Laundry Room"


def test_waits_while_not_ready_stranded_or_faulted():
    auto = AutoRun(ORDER)
    auto.start()
    assert auto.on_update(RobotView("idle", True, 68, None), None, 0) == []
    assert auto.on_update(RobotView("idle", False, 6, None), None, 1) == []        # stranded
    assert auto.on_update(RobotView("idle", True, 0, None, ("dock_clogged",)), None, 2) == []
    assert auto.on_update(RobotView("idle", True, 0, None, ("bin_full",)), None, 3) == [("start", "Living Room")]


def test_someone_elses_job_makes_auto_step_aside():
    auto = AutoRun(ORDER)
    auto.start()
    run_room(auto, "Living Room")
    auto.on_update(cleaning("Kitchen"), None, 5)  # a dashboard job, not ours
    assert not auto.active and auto.room == "Laundry Room"
    auto.start()
    auto.on_update(RobotView("running", False, 0, "all"), None, 6)  # app whole-house run
    assert not auto.active


def test_ignored_start_is_retried():
    auto = AutoRun(ORDER)
    auto.start()
    assert auto.on_update(DOCKED, None, 0) == [("start", "Living Room")]
    assert auto.on_update(DOCKED, None, START_TIMEOUT - 1) == []
    auto.on_update(DOCKED, None, START_TIMEOUT + 1)
    assert auto.phase == "waiting"
    assert auto.on_update(DOCKED, None, START_TIMEOUT + 2) == [("start", "Living Room")]


def test_lost_end_redoes_the_room():
    auto = AutoRun(ORDER)
    auto.start()
    auto.on_update(DOCKED, None, 0)
    auto.on_update(cleaning("Living Room"), None, 1)
    auto.on_update(DOCKED, None, 2)
    auto.on_update(DOCKED, None, 2 + LOST_END_TIMEOUT + 1)
    assert auto.phase == "waiting" and auto.room == "Living Room"


def test_restore_resumes_from_the_retained_state():
    auto = AutoRun(ORDER)
    auto.start()
    run_room(auto, "Living Room")
    auto.on_update(DOCKED, None, 5)  # Laundry started
    saved = auto.snapshot()
    assert saved["state"] == "starting" and saved["current_room"] == "Laundry Room" and saved["step"] == "2 of 5"
    fresh = AutoRun(ORDER)
    fresh.restore(saved)
    assert fresh.active and fresh.phase == "cleaning" and fresh.room == "Laundry Room" and fresh.cleaned == 1
    # A different configured order starts fresh rather than resuming a stale index.
    other = AutoRun(ORDER[::-1])
    other.restore(saved)
    assert not other.active and other.index == 0


def test_start_while_active_is_a_no_op_and_cancel_while_off_does_nothing():
    auto = AutoRun(ORDER)
    assert auto.cancel(DOCKED) == []
    auto.start()
    run_room(auto, "Living Room")
    auto.start()
    assert auto.cleaned == 1 and auto.room == "Laundry Room"
