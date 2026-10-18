"""Turn the robot's raw reported state into the values the HA entities publish.

Everything here is pure: a dict of reported state in, plain values out. The
bridge decides when to publish; this module only decides what.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from roombapy import ROOMBA_ERROR_MESSAGES

log = logging.getLogger(__name__)

# Clean Base (dock) state codes, from `reported.dock.state`.
# 301, 302 and 351 were observed on this s9+ / dock 4500490 on 2026-10-18
# (ready, emptying the bin, clogged). The rest are the community mapping used
# by dorita980/rest980 users and have NOT been seen here.
DOCK_STATES: dict[int, str] = {
    300: "ready",
    301: "ready",
    302: "emptying",
    303: "emptying",
    350: "bag_missing",
    351: "clogged",
    352: "sealing_problem",
    353: "bag_full",
    360: "comms_problem",
}
DOCK_OPTIONS = ["ready", "emptying", "clogged", "bag_full", "bag_missing",
                "sealing_problem", "comms_problem", "problem", "none", "unknown"]

# Headline status, most urgent first where several are true at once.
STATUS_OPTIONS = ["error", "stuck", "cleaning", "returning", "dock_clogged",
                  "dock_bag_full", "dock_problem", "emptying", "finishing", "recharging", "bin_full",
                  "paused", "charging", "docked", "idle"]

# finishing: back on the dock after cleaning, job not yet closed (the robot
# saves its map here). recharging: on the dock mid-job, will carry on.
MISSION_OPTIONS = ["idle", "running", "returning", "paused", "emptying", "finishing", "recharging"]
LAST_MISSION_OPTIONS = ["ok", "failed", "cancelled"]

# HA's MQTT vacuum states.
VACUUM_STATES = ["cleaning", "docked", "paused", "idle", "returning", "error"]

# Suction is two robot preferences resolved together (see roombapy
# set_preferences). Same mapping the HA core integration uses.
FAN_SPEEDS: dict[str, dict[str, bool]] = {
    "Automatic": {"carpetBoost": True, "vacHigh": False},
    "Eco": {"carpetBoost": False, "vacHigh": False},
    "Performance": {"carpetBoost": False, "vacHigh": True},
}

RETURNING_PHASES = {"hmUsrDock", "hmPostMsn", "hmMidMsn", "dock", "dockend"}
RUNNING_PHASES = {"run", "new", "resume", "spot"}
DOCK_PROBLEMS = {"clogged", "bag_full", "bag_missing", "sealing_problem",
                 "comms_problem", "problem"}


def dock_state(reported: dict[str, Any]) -> tuple[str, int | None]:
    dock = reported.get("dock")
    if not isinstance(dock, dict) or not dock.get("known", True):
        return "none", None
    code = dock.get("state")
    if not isinstance(code, int):
        return "unknown", None
    if code in DOCK_STATES:
        return DOCK_STATES[code], code
    # Unlisted codes: the known ones split ready/active below 350 and faults
    # from 350 up, so keep that split rather than calling a fault "unknown".
    return ("problem" if code >= 350 else "unknown"), code


def _mission(reported: dict[str, Any]) -> dict[str, Any]:
    status = reported.get("cleanMissionStatus")
    return status if isinstance(status, dict) else {}


def error_text(reported: dict[str, Any]) -> str | None:
    code = _mission(reported).get("error") or 0
    if not code:
        return None
    return ROOMBA_ERROR_MESSAGES.get(code, f"Error {code}")


def bin_full(reported: dict[str, Any]) -> bool:
    b = reported.get("bin")
    return bool(isinstance(b, dict) and b.get("full"))


def vacuum_state(reported: dict[str, Any]) -> str:
    m = _mission(reported)
    phase, cycle = m.get("phase", ""), m.get("cycle", "none")
    if m.get("error") or phase == "stuck":
        return "error"
    if phase in RUNNING_PHASES:
        return "cleaning"
    if phase in RETURNING_PHASES:
        return "returning"
    if phase in {"charge", "evac", "recharge", "completed"}:
        return "docked"
    if phase in {"stop", "pause"}:
        return "paused" if cycle not in (None, "", "none") else "idle"
    return "idle"


def fan_speed(reported: dict[str, Any]) -> str | None:
    for name, flags in FAN_SPEEDS.items():
        if all(reported.get(k) == v for k, v in flags.items()):
            return name
    return None


def mission_state(reported: dict[str, Any], has_run: bool = False) -> str:
    """`has_run`: this job has already been off the dock (see MissionTracker).

    On the dock with the job still open means three different things: about
    to leave (the first second of a job), recharging to carry on, or wrapping
    up after cleaning while the map is saved. Seen 2026-10-18: ~17 s of
    `charge` with cycle `clean` after a job's final bin empty.
    """
    m = _mission(reported)
    phase, cycle = m.get("phase", ""), m.get("cycle", "none")
    if cycle in (None, "", "none"):
        return "idle"
    if phase in RETURNING_PHASES:
        return "returning"
    if phase in {"stop", "pause", "stuck"}:
        return "paused"
    if phase == "evac":
        return "emptying"
    if phase in {"charge", "recharge"}:
        if phase == "recharge" or (m.get("rechrgM") or 0) > 0:
            return "recharging"
        return "finishing" if has_run else "running"
    return "running"


def command_rooms(command: dict[str, Any] | None, rooms: dict[str, str]) -> list[str] | None:
    """Room names a start command targeted; None for a whole-house run."""
    if not isinstance(command, dict) or command.get("command") != "start":
        return None
    regions = command.get("regions") or []
    if not regions:
        return None
    return [rooms.get(str(r.get("region_id")), f"room {r.get('region_id')}") for r in regions]


def _ts(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _emptying(reported: dict[str, Any]) -> bool:
    return _mission(reported).get("phase") == "evac" or dock_state(reported)[0] == "emptying"


def attention(reported: dict[str, Any]) -> list[str]:
    """Reasons someone should look at the robot, most urgent first."""
    reasons: list[str] = []
    err = error_text(reported)
    m = _mission(reported)
    if m.get("phase") == "stuck":
        reasons.append("stuck")
    elif err:
        reasons.append(f"error: {err}")
    dock, _ = dock_state(reported)
    if dock in DOCK_PROBLEMS:
        reasons.append(f"dock_{dock}")
    # With a self-emptying dock, a full bin while docked means emptying failed
    # (but not while the dock is still emptying it).
    if bin_full(reported) and vacuum_state(reported) in {"docked", "idle"} and not _emptying(reported):
        reasons.append("bin_full")
    if mission_state(reported) == "paused" and "stuck" not in reasons:
        reasons.append("paused")
    return reasons


def status(reported: dict[str, Any], has_run: bool = False) -> str:
    m = _mission(reported)
    phase = m.get("phase", "")
    if phase == "stuck":
        return "stuck"
    if m.get("error"):
        return "error"
    vac = vacuum_state(reported)
    if vac == "cleaning":
        return "cleaning"
    if vac == "returning":
        return "returning"
    dock, _ = dock_state(reported)
    if dock == "clogged":
        return "dock_clogged"
    if dock == "bag_full":
        return "dock_bag_full"
    if dock in DOCK_PROBLEMS:
        return "dock_problem"
    if _emptying(reported):
        return "emptying"
    # A job still open on the dock outranks the bin: if it is still full once
    # the job closes, "bin_full" takes over then.
    ms = mission_state(reported, has_run)
    if ms in {"finishing", "recharging"}:
        return ms
    if bin_full(reported) and vac in {"docked", "idle"}:
        return "bin_full"
    if vac == "paused":
        return "paused"
    if vac == "docked":
        bat = reported.get("batPct")
        return "charging" if isinstance(bat, int) and bat < 100 else "docked"
    return "idle"


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    suction: str = "Automatic"
    passes: int = 1


@dataclass(frozen=True)
class RobotMap:
    """The app's map ids and rooms, in cleaning order.

    `user_pmapv_id` must be the value the app sends, which is not the version
    the robot lists under `pmaps`. Both ids change when rooms are edited in
    the app, so this is configuration, re-captured after a map edit.
    """

    pmap_id: str
    user_pmapv_id: str
    rooms: tuple[Room, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RobotMap:
        return cls(
            pmap_id=data["pmap_id"],
            user_pmapv_id=data["user_pmapv_id"],
            rooms=tuple(Room(id=str(r["id"]), name=r["name"],
                             suction=r.get("suction", "Automatic"),
                             passes=int(r.get("passes", 1)))
                        for r in data["rooms"]),
        )

    @property
    def names(self) -> dict[str, str]:
        return {r.id: r.name for r in self.rooms}

    def resolve(self, room: str) -> Room:
        """A room by id or (case-insensitive) name."""
        for r in self.rooms:
            if room == r.id or room.casefold() == r.name.casefold():
                return r
        raise ValueError(f"Unknown room {room!r}; known: {', '.join(r.name for r in self.rooms)}")

    def start_params(self, rooms: list[str], suction: str | None = None,
                     passes: int | None = None) -> dict[str, Any]:
        """Params for a room-scoped `start`, shaped exactly as the app sends it.

        Rooms run in the map's order, not the caller's. `suction`/`passes`
        override every room's default for this job only; the robot's saved
        preferences are untouched.
        """
        if suction is not None and suction not in FAN_SPEEDS:
            raise ValueError(f"Unknown suction {suction!r}; use one of {', '.join(FAN_SPEEDS)}")
        if passes is not None and passes not in (1, 2):
            raise ValueError("passes must be 1 or 2")
        wanted = {self.resolve(r).id for r in rooms}
        if not wanted:
            # The robot reads an empty region list as "everywhere".
            raise ValueError("No rooms given; refusing a whole-house start")
        regions = []
        for r in self.rooms:
            if r.id not in wanted:
                continue
            p = passes or r.passes
            regions.append({
                "params": {**FAN_SPEEDS[suction or r.suction], "gentleMode": 0,
                           "noAutoPasses": True, "twoPass": p == 2},
                "region_id": r.id,
                "type": "rid",
            })
        return {"ordered": 1, "pmap_id": self.pmap_id,
                "user_pmapv_id": self.user_pmapv_id, "regions": regions}


@dataclass
class MissionTracker:
    """Detects a mission ending from the robot's lifetime counters.

    `bbmssn` holds ok/failed/cancelled totals and is updated when a mission
    finishes, so whichever counter moved is the result. Counters seen on the
    first full state are the baseline, so a restart does not invent a result.

    A job's rooms are fixed when it starts. A `start` without regions that the
    robot reports while a job is already running cannot change them (a
    whole-house run begins from idle), so it is logged and ignored rather than
    turning the job into "all".
    """

    rooms: dict[str, str] = field(default_factory=dict)
    _counts: dict[str, int] | None = None
    _start_command: dict[str, Any] | None = None
    _seen_start: Any = None
    _ran: Any = None  # missionId of the job last seen off the dock

    def has_run(self, reported: dict[str, Any]) -> bool:
        mid = _mission(reported).get("missionId")
        return mid is not None and mid == self._ran

    @property
    def start_command(self) -> dict[str, Any] | None:
        return self._start_command

    def update(self, reported: dict[str, Any]) -> dict[str, Any] | None:
        cmd = reported.get("lastCommand")
        if isinstance(cmd, dict) and cmd.get("command") == "start" and cmd.get("time") != self._seen_start:
            self._seen_start = cmd.get("time")
            active = _mission(reported).get("cycle") not in (None, "", "none")
            if cmd.get("regions") or not active or self._start_command is None:
                # A copy: roombapy updates `lastCommand` in place, so a kept
                # reference would turn into the next command (pause, resume…).
                self._start_command = copy.deepcopy(cmd)
            else:
                log.warning("Ignoring a start without regions during a running job: %s", cmd)
        m = _mission(reported)
        if m.get("cycle") not in (None, "", "none") and m.get("phase") in (
                RUNNING_PHASES | RETURNING_PHASES | {"stop", "pause", "stuck"}):
            self._ran = m.get("missionId")
        bb = reported.get("bbmssn")
        if not isinstance(bb, dict):
            return None
        counts = {k: int(bb.get(k) or 0) for k in ("nMssnOk", "nMssnF", "nMssnC")}
        previous, self._counts = self._counts, counts
        if previous is None or counts == previous:
            return None
        result = ("ok" if counts["nMssnOk"] > previous["nMssnOk"]
                  else "failed" if counts["nMssnF"] > previous["nMssnF"]
                  else "cancelled" if counts["nMssnC"] > previous["nMssnC"]
                  else None)
        if result is None:
            return None
        m = _mission(reported)
        return {
            "result": result,
            "ended": datetime.now(UTC).isoformat(),
            "started": _ts(m.get("mssnStrtTm")),
            "rooms": command_rooms(self._start_command, self.rooms) or "all",
            "initiator": m.get("initiator"),
            "mission_id": m.get("missionId"),
        }


def mission_attributes(reported: dict[str, Any], rooms: dict[str, str],
                       start_command: dict[str, Any] | None, has_run: bool = False) -> dict[str, Any]:
    """`start_command` is the tracker's, not `lastCommand`: the app polls the
    robot with `querydock` mid-mission, which replaces `lastCommand`."""
    m = _mission(reported)
    active = mission_state(reported, has_run) != "idle"
    return {
        "rooms": (command_rooms(start_command, rooms) or "all") if active else None,
        "started": _ts(m.get("mssnStrtTm")) if active else None,
        "elapsed_min": m.get("mssnM") if active else None,
        "initiator": m.get("initiator") if active else None,
        "phase": m.get("phase"),
    }
