"""Auto run: clean a fixed room order while nobody is home.

Home Assistant decides *when* (everyone left → `auto_start`; someone back, or
the switch turned off → `auto_cancel`). This decides *what*: the rooms run as a
chain of single-room jobs, because the robot never reports which room it is in.
One room per job means the room a run was interrupted in is always known, and
the next run starts there and wraps round the order.

Pure: the bridge feeds it robot state and runs the actions it returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PHASES = ["off", "waiting", "starting", "cleaning"]

# Faults that mean "don't start anything until a person has looked".
_BLOCKING = ("stuck", "error", "dock_")

# How long a start may go unanswered before trying again (the robot holds or
# drops starts it isn't ready for).
START_TIMEOUT = 120.0
# A room job that ended without the end being seen (e.g. the bridge restarted
# meanwhile): once the robot has been idle this long, redo that room.
LOST_END_TIMEOUT = 300.0


@dataclass(frozen=True)
class RobotView:
    """What auto needs to know about the robot, derived by the bridge."""

    mission: str                       # state.mission_state(): idle, running, ...
    on_dock: bool                      # docked (charging) rather than out on the floor
    not_ready: int                     # robot's own "won't start" code, 0 when ready
    job_rooms: list[str] | str | None  # rooms of the job on the floor ("all", or None)
    reasons: tuple[str, ...] = ()      # attention reasons from state.attention()
    battery: int | None = None         # percent

    @property
    def busy(self) -> bool:
        return self.mission != "idle"

    @property
    def fault(self) -> bool:
        return any(str(r).startswith(_BLOCKING) for r in self.reasons)


# Below this, an auto room job waits for Roomba to charge (start only when above it).
MIN_BATTERY = 75


@dataclass
class AutoRun:
    order: list[str]
    min_battery: int = MIN_BATTERY
    active: bool = False
    index: int = 0          # the room to clean next (or being cleaned now)
    cleaned: int = 0        # rooms finished in this run
    phase: str = "off"
    started_at: float | None = field(default=None, compare=False)
    idle_since: float | None = field(default=None, compare=False)
    # Why a waiting run hasn't started its next room: "battery", "ready" or None.
    waiting_for: str | None = field(default=None, compare=False)

    # ------------------------------------------------------------- state
    @property
    def room(self) -> str:
        return self.order[self.index % len(self.order)]

    def snapshot(self) -> dict[str, Any]:
        n = len(self.order)
        return {
            "state": self.phase,
            "active": self.active,
            "order": self.order,
            "index": self.index,
            "cleaned": self.cleaned,
            "next_room": self.room,
            "current_room": self.room if self.phase in ("starting", "cleaning") else None,
            "step": f"{min(self.cleaned + 1, n)} of {n}" if self.active else None,
            "waiting_for": self.waiting_for if self.phase == "waiting" else None,
            "min_battery": self.min_battery,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Pick up where a previous bridge left off (from the retained message).

        A run in flight comes back as `waiting`: whatever job it had started is
        either still on the floor (its end is then handled as usual once the
        tracker sees it) or long gone.
        """
        if not isinstance(data, dict) or data.get("order") != self.order:
            return  # a different order: start fresh rather than resume a stale index
        self.index = int(data.get("index") or 0) % len(self.order)
        self.cleaned = int(data.get("cleaned") or 0)
        self.active = bool(data.get("active"))
        prior = data.get("state")
        self.phase = ("cleaning" if prior in ("starting", "cleaning") else "waiting") if self.active else "off"

    # ---------------------------------------------------------- commands
    def start(self) -> list[tuple]:
        if self.active:
            return []
        self.active, self.cleaned, self.phase = True, 0, "waiting"
        self.index %= len(self.order)
        return []

    def cancel(self, view: RobotView) -> list[tuple]:
        """Stop the run. The interrupted room stays next."""
        if not self.active:
            return []
        on_floor = self.phase in ("starting", "cleaning") and view.busy
        self.active, self.phase = False, "off"
        return [("dock",)] if on_floor else []

    def step_aside(self) -> None:
        """Someone started a job by other means: stop chaining, keep the index."""
        if self.active:
            self.active, self.phase = False, "off"

    # ------------------------------------------------------------ updates
    def on_update(self, view: RobotView, ended: dict[str, Any] | None, now: float) -> list[tuple]:
        if not self.active:
            return []
        mine = isinstance(view.job_rooms, list) and view.job_rooms == [self.room]

        if ended and self.phase in ("starting", "cleaning") and ended.get("rooms") == [self.room]:
            if ended.get("result") == "ok":
                self.cleaned += 1
                if self.cleaned >= len(self.order):
                    # Whole order done: the next run starts from the top.
                    self.active, self.phase, self.index, self.cleaned = False, "off", 0, 0
                    return []
                self.index = (self.index + 1) % len(self.order)
                self.phase = "waiting"
            else:
                # Cancelled or failed: resume this room next time.
                self.active, self.phase = False, "off"
            return []

        if self.phase == "starting":
            if view.busy and mine:
                self.phase = "cleaning"
            elif view.busy:
                self.step_aside()
            elif self.started_at is not None and now - self.started_at > START_TIMEOUT:
                self.phase = "waiting"
            return []

        if self.phase == "cleaning":
            if view.busy:
                self.idle_since = None
                if not mine and view.job_rooms is not None:
                    self.step_aside()  # a different job replaced ours
            else:
                self.idle_since = self.idle_since if self.idle_since is not None else now
                if now - self.idle_since > LOST_END_TIMEOUT:
                    self.phase, self.idle_since = "waiting", None
            return []

        # waiting
        if view.busy:
            self.step_aside()  # someone else's job
            return []
        if not (view.on_dock and not view.not_ready and not view.fault):
            self.waiting_for = "ready"
            return []
        # Each room starts on a good charge: Roomba charges on the dock between rooms.
        if view.battery is None or view.battery <= self.min_battery:
            self.waiting_for = "battery"
            return []
        self.waiting_for = None
        self.phase, self.started_at = "starting", now
        return [("start", self.room)]
