"""Hold the Roomba's single local connection and mirror it onto MQTT for HA.

The robot accepts one local client at a time, so this replaces Home
Assistant's `roomba` integration rather than running beside it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import aiomqtt
from roombapy import RoombaAuthError, RoombaClient, RoombaConnectionError

from . import state
from .auto import PHASES as AUTO_PHASES
from .auto import AutoRun, RobotView

log = logging.getLogger("roomba_mqtt_bridge")


@dataclass(frozen=True)
class Config:
    roomba_host: str
    roomba_blid: str
    roomba_password: str
    mqtt_host: str
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    base_topic: str = "roomba"
    discovery_prefix: str = "homeassistant"
    device_name: str = "Roomba"
    # The app's map ids and rooms; None disables room cleaning.
    robot_map: state.RobotMap | None = None
    # No message from the robot for this long means a dead session: the robot
    # reports Wi-Fi signal every few seconds even when idle on the dock.
    stale_seconds: float = 300

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            roomba_host=env["ROOMBA_HOST"],
            roomba_blid=env["ROOMBA_BLID"],
            roomba_password=env["ROOMBA_PASSWORD"],
            mqtt_host=env["MQTT_HOST"],
            mqtt_port=int(env.get("MQTT_PORT", "1883")),
            mqtt_username=env.get("MQTT_USERNAME") or None,
            mqtt_password=env.get("MQTT_PASSWORD") or None,
            base_topic=env.get("BASE_TOPIC", "roomba"),
            discovery_prefix=env.get("DISCOVERY_PREFIX", "homeassistant"),
            device_name=env.get("DEVICE_NAME", "Roomba"),
            robot_map=(state.RobotMap.from_json(json.loads(env["ROOMBA_MAP"]))
                       if env.get("ROOMBA_MAP") else None),
            stale_seconds=float(env.get("STALE_SECONDS", "300")),
        )


class Bridge:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        t = config.base_topic
        self.t_bridge = f"{t}/bridge"
        self.t_robot = f"{t}/robot"
        self.t_state = f"{t}/state"
        self.t_last = f"{t}/last_mission"
        self.t_command = f"{t}/command"
        self.t_fan = f"{t}/set_fan_speed"
        self.t_send = f"{t}/send_command"
        self.t_segments = f"{t}/clean_segments"
        self.t_auto = f"{t}/auto"
        self.t_ha_status = f"{config.discovery_prefix}/status"
        self.robot = RoombaClient(config.roomba_host, config.roomba_blid, config.roomba_password)
        self.rooms = config.robot_map.names if config.robot_map else {}
        self.tracker = state.MissionTracker(rooms=self.rooms)
        self.mqtt: aiomqtt.Client | None = None
        self._last_payload: str | None = None
        self._last_message = time.monotonic()
        self._device_info: dict[str, Any] = {}
        self._last_command_time: Any = None
        self._last_mission_line: str | None = None
        rmap = config.robot_map
        self.auto = (AutoRun(list(rmap.auto_order), min_battery=rmap.auto_min_battery)
                     if rmap and rmap.auto_order else None)
        # Auto acts only once its retained state has been read back (or found
        # absent), so a restart resumes rather than starting a fresh run.
        self._auto_restored = False
        self._last_auto: str | None = None

    # ------------------------------------------------------------- discovery

    def discovery(self) -> dict[str, Any]:
        uid = f"roomba_{self.cfg.roomba_blid.lower()}"
        avail = {
            "availability": [
                {"topic": self.t_bridge},
                {"topic": self.t_robot},
            ],
            "availability_mode": "all",
        }

        def enum_sensor(key: str, name: str, options: list[str], attrs: str | None = None,
                        topic: str | None = None) -> dict[str, Any]:
            c = {
                "p": "sensor", "name": name, "unique_id": f"{uid}_{key}",
                "default_entity_id": f"sensor.roomba_{key}",
                "device_class": "enum", "options": options,
                "state_topic": topic or self.t_state,
                "value_template": f"{{{{ value_json.{key} }}}}",
                **avail,
            }
            if attrs:
                c["json_attributes_topic"] = topic or self.t_state
                c["json_attributes_template"] = attrs
            return c

        def binary(key: str, name: str, device_class: str, attrs: str | None = None) -> dict[str, Any]:
            c = {
                "p": "binary_sensor", "name": name, "unique_id": f"{uid}_{key}",
                "default_entity_id": f"binary_sensor.roomba_{key}",
                "device_class": device_class, "state_topic": self.t_state,
                "value_template": f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
                **avail,
            }
            if attrs:
                c["json_attributes_topic"] = self.t_state
                c["json_attributes_template"] = attrs
            return c

        components = {
            "vacuum": {
                "p": "vacuum", "name": None, "unique_id": f"{uid}_vacuum",
                "default_entity_id": "vacuum.roomba",
                # The vacuum reads `state` and `fan_speed` straight from this JSON.
                "state_topic": self.t_state,
                "command_topic": self.t_command,
                "set_fan_speed_topic": self.t_fan,
                "send_command_topic": self.t_send,
                "fan_speed_list": list(state.FAN_SPEEDS),
                "supported_features": ["start", "stop", "pause", "return_home",
                                       "fan_speed", "send_command", "locate"],
                # Rooms arrive as `segments` in the state JSON; HA then offers
                # mapping them to its areas, and vacuum.clean_area publishes
                # the chosen ids here.
                **({"clean_segments_command_topic": self.t_segments}
                   if self.cfg.robot_map else {}),
                **avail,
            },
            "status": enum_sensor("status", "Status", state.STATUS_OPTIONS,
                                  "{{ {'reasons': value_json.attention_reasons, 'error': value_json.error} | tojson }}"),
            "dock": enum_sensor("dock", "Dock", state.DOCK_OPTIONS,
                                "{{ {'code': value_json.dock_code} | tojson }}"),
            "mission": enum_sensor("mission", "Mission", state.MISSION_OPTIONS,
                                   "{{ value_json.mission_attributes | tojson }}"),
            "last_mission": enum_sensor("last_mission", "Last mission", state.LAST_MISSION_OPTIONS,
                                        "{{ value_json | tojson }}", topic=self.t_last),
            **({"auto": enum_sensor("auto", "Auto run", AUTO_PHASES, "{{ value_json | tojson }}", topic=self.t_auto)}
               if self.auto else {}),
            "attention": binary("attention", "Attention", "problem",
                                "{{ {'reasons': value_json.attention_reasons} | tojson }}"),
            "bin_full": binary("bin_full", "Bin full", "problem"),
            "charging": binary("charging", "Charging", "battery_charging"),
            "battery": {
                "p": "sensor", "name": "Battery", "unique_id": f"{uid}_battery",
                "default_entity_id": "sensor.roomba_battery",
                "device_class": "battery", "unit_of_measurement": "%",
                "state_class": "measurement", "state_topic": self.t_state,
                "value_template": "{{ value_json.battery }}", **avail,
            },
        }
        components["last_mission"]["value_template"] = "{{ value_json.result }}"
        if self.auto:
            components["auto"]["value_template"] = "{{ value_json.state }}"
        return {
            "dev": {
                "identifiers": [uid],
                "name": self.cfg.device_name,
                "manufacturer": "iRobot",
                **self._device_info,
            },
            "o": {"name": "roomba-mqtt-bridge"},
            "cmps": components,
            "qos": 1,
        }

    async def publish_discovery(self) -> None:
        assert self.mqtt
        topic = f"{self.cfg.discovery_prefix}/device/roomba_{self.cfg.roomba_blid.lower()}/config"
        await self.mqtt.publish(topic, json.dumps(self.discovery()), qos=1, retain=True)

    # --------------------------------------------------------------- state

    def snapshot(self, reported: dict[str, Any]) -> dict[str, Any]:
        dock, code = state.dock_state(reported)
        reasons = state.attention(reported)
        ran = self.tracker.has_run(reported)
        mission = state.mission_state(reported, ran)
        bat = reported.get("batPct")
        phase = (reported.get("cleanMissionStatus") or {}).get("phase")
        return {
            "state": state.vacuum_state(reported),
            "fan_speed": state.fan_speed(reported),
            "status": state.status(reported, ran),
            "dock": dock,
            "dock_code": code,
            "mission": mission,
            "mission_attributes": state.mission_attributes(
                reported, self.rooms, self.tracker.start_command, ran, self.cfg.robot_map),
            "attention": bool(reasons),
            "attention_reasons": reasons,
            "error": state.error_text(reported),
            "battery": bat,
            "bin_full": state.bin_full(reported),
            "charging": phase in {"charge", "recharge"} and isinstance(bat, int) and bat < 100,
            # Read by the MQTT vacuum (and popped) for clean_area support.
            "segments": self.rooms,
        }

    async def on_robot_message(self, _message: dict[str, Any]) -> None:
        self._last_message = time.monotonic()
        reported = self.robot.master_state.get("state", {}).get("reported", {})
        if not self._device_info and reported.get("sku"):
            self._device_info = {"model": reported.get("sku"),
                                 "sw_version": reported.get("softwareVer")}
            await self.publish_discovery()
        # Every command the robot reports receiving, from any source (this
        # bridge, the iRobot app, its cloud), once each.
        cmd = reported.get("lastCommand")
        if isinstance(cmd, dict) and cmd.get("time") != self._last_command_time:
            if self._last_command_time is not None:
                log.info("Robot reports command: %s", json.dumps(cmd))
            self._last_command_time = cmd.get("time")
        # Mission status as the robot reports it, logged on change, so a robot
        # that ignores commands (notReady) can be diagnosed from the logs.
        m = reported.get("cleanMissionStatus") or {}
        line = json.dumps({k: m.get(k) for k in ("cycle", "phase", "error", "notReady", "condNotReady")}, sort_keys=True)
        if line != self._last_mission_line:
            log.info("Robot mission status: %s", line)
            self._last_mission_line = line
        ended = self.tracker.update(reported)
        if self.mqtt is None:
            return
        if ended:
            log.info("Mission ended: %s", ended)
            await self.mqtt.publish(self.t_last, json.dumps(ended), qos=1, retain=True)
        if self.auto and self._auto_restored:
            await self.run_auto(self.auto.on_update(self.robot_view(reported), ended, time.monotonic()))
        payload = json.dumps(self.snapshot(reported), sort_keys=True)
        if payload != self._last_payload:
            self._last_payload = payload
            await self.mqtt.publish(self.t_state, payload, qos=1, retain=True)

    async def on_robot_connection(self, conn: str, error: str | None) -> None:
        log.info("Robot %s%s", conn, f" ({error})" if error else "")
        if self.mqtt:
            await self.mqtt.publish(self.t_robot, "online" if conn == "connected" else "offline",
                                    qos=1, retain=True)

    # ------------------------------------------------------------ commands

    async def handle_command(self, payload: str) -> None:
        reported = self.robot.master_state.get("state", {}).get("reported", {})
        vac = state.vacuum_state(reported)
        log.info("HA command %r (vacuum %s, mission %s)", payload, vac, state.mission_state(reported))
        if payload == "start":
            if vac != "paused":
                self._check_can_start()
                if self.auto:
                    self.auto.step_aside()
            await self.robot.send_command("resume" if vac == "paused" else "start")
        elif payload == "pause":
            await self.robot.send_command("pause")
        elif payload == "stop":
            await self.robot.send_command("stop")
        elif payload == "return_to_base":
            m = reported.get("cleanMissionStatus") or {}
            on_dock = m.get("phase") in ("charge", "recharge", "evac")
            if on_dock and m.get("cycle") not in (None, "", "none"):
                # Already home with a job still open (e.g. recharging mid-job):
                # end the job where it is. `dock` here makes the robot leave and
                # come back -- seen 2026-10-18, it then stopped with "Bin full".
                await self.robot.send_command("stop")
            elif on_dock:
                log.info("return_to_base: already docked, nothing to do")
            else:
                # The robot ignores `dock` while cleaning; pause it first, as HA's
                # core integration does.
                if vac == "cleaning":
                    await self.robot.send_command("pause")
                    await asyncio.sleep(1)
                await self.robot.send_command("dock")
        elif payload == "locate":
            await self.robot.send_command("find")
        else:
            log.warning("Ignoring unsupported command %r", payload)

    async def handle_fan_speed(self, payload: str) -> None:
        flags = state.FAN_SPEEDS.get(payload)
        if flags is None:
            log.warning("Ignoring unknown fan speed %r", payload)
            return
        await self.robot.set_preferences(flags)

    async def handle_send_command(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {"command": payload}
        if isinstance(data, str):
            data = {"command": data}
        # HA's MQTT vacuum merges a call's params into the top level beside
        # "command"; accept a nested "params" object too, for hand-made publishes.
        command = data.pop("command", None)
        params = data.pop("params", None) if isinstance(data.get("params"), dict) else None
        params = (params or {}) | data or None
        if not command:
            log.warning("send_command without a command: %r", payload)
            return
        if command in ("auto_start", "auto_cancel"):
            await self.handle_auto(command)
            return
        if command == "clean_rooms":
            p = params or {}
            rooms = p.get("rooms")
            await self.clean_rooms(
                rooms if isinstance(rooms, list) else [rooms] if rooms else [],
                suction=p.get("suction"),
                passes=int(p["passes"]) if p.get("passes") else None,
            )
            return
        if command in ("start", "clean", "cleanRoom"):
            self._check_can_start()
        log.info("send_command %s %s", command, json.dumps(params))
        # roombapy refuses a start whose regions list is empty: the robot
        # would read that as "clean the whole house".
        await self.robot.send_command(command, params)

    def not_ready(self) -> int:
        """The robot's own "won't start" code, 0 when it will take a job.

        A start sent while this is non-zero is acknowledged and held: seen
        2026-10-18, starts tapped during a code-68 spell ran on their own
        minutes later (reported as `initiator: manual`). So fresh starts are
        refused here rather than left to surprise someone.
        """
        reported = self.robot.master_state.get("state", {}).get("reported", {})
        return int((reported.get("cleanMissionStatus") or {}).get("notReady") or 0)

    def _check_can_start(self) -> None:
        code = self.not_ready()
        if code:
            raise RuntimeError(f"Roomba is not ready (notReady {code}); not sending a start it would hold and run later")

    async def clean_rooms(self, rooms: list[str], suction: str | None = None,
                          passes: int | None = None, from_auto: bool = False) -> None:
        if self.cfg.robot_map is None:
            raise ValueError("ROOMBA_MAP is not configured; room cleaning is off")
        params = self.cfg.robot_map.start_params([str(r) for r in rooms], suction, passes)
        self._check_can_start()
        if self.auto and not from_auto:
            self.auto.step_aside()  # a job started by hand ends the auto run
        log.info("Room clean: %s", json.dumps(params))
        await self.robot.send_command("start", params)

    # -------------------------------------------------------------- auto
    def robot_view(self, reported: dict[str, Any]) -> RobotView:
        ran = self.tracker.has_run(reported)
        m = reported.get("cleanMissionStatus") or {}
        attrs = state.mission_attributes(reported, self.rooms, self.tracker.start_command, ran)
        return RobotView(
            mission=state.mission_state(reported, ran),
            on_dock=m.get("phase") in ("charge", "evac", "recharge"),
            not_ready=int(m.get("notReady") or 0),
            job_rooms=attrs["rooms"],
            reasons=tuple(state.attention(reported)),
            battery=reported.get("batPct") if isinstance(reported.get("batPct"), int) else None,
        )

    async def run_auto(self, actions: list[tuple]) -> None:
        assert self.auto is not None
        for action in actions:
            try:
                if action[0] == "start":
                    log.info("Auto run: starting %s (%s)", action[1], self.auto.snapshot()["step"])
                    rmap = self.cfg.robot_map
                    await self.clean_rooms([action[1]], passes=rmap.auto_passes if rmap else None, from_auto=True)
                elif action[0] == "dock":
                    log.info("Auto run cancelled: sending Roomba home")
                    await self.handle_command("return_to_base")
            except Exception:
                log.exception("Auto run action %s failed", action)
                if action[0] == "start":
                    self.auto.phase = "waiting"  # try again on a later update
        await self.publish_auto()

    async def handle_auto(self, command: str) -> None:
        if self.auto is None:
            log.warning("%s: no auto_order configured", command)
            return
        reported = self.robot.master_state.get("state", {}).get("reported", {})
        view = self.robot_view(reported)
        log.info("HA command %s (auto %s)", command, self.auto.phase)
        if command == "auto_start":
            self.auto.start()
            # Start straight away if Roomba is ready, rather than on the next update.
            actions = self.auto.on_update(view, None, time.monotonic()) if self._auto_restored else []
        else:
            actions = self.auto.cancel(view)
        await self.run_auto(actions)

    async def publish_auto(self) -> None:
        if self.auto is None or self.mqtt is None:
            return
        payload = json.dumps(self.auto.snapshot(), sort_keys=True)
        if payload != self._last_auto:
            self._last_auto = payload
            await self.mqtt.publish(self.t_auto, payload, qos=1, retain=True)

    def restore_auto(self, payload: str | None) -> None:
        if self.auto is None or self._auto_restored:
            return
        if payload:
            try:
                self.auto.restore(json.loads(payload))
                log.info("Auto run restored: %s", self.auto.snapshot())
            except (ValueError, TypeError):
                log.warning("Ignoring unreadable auto state %r", payload)
        self._auto_restored = True

    async def auto_restore_deadline(self) -> None:
        """No retained auto state within a few seconds means there is none."""
        await asyncio.sleep(5)
        if not self._auto_restored:
            self.restore_auto(None)
            await self.publish_auto()

    async def handle_segments(self, payload: str) -> None:
        ids = json.loads(payload)
        await self.clean_rooms(ids if isinstance(ids, list) else [ids])

    async def dispatch(self, message: aiomqtt.Message) -> None:
        payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
        topic = str(message.topic)
        try:
            if topic == self.t_ha_status:
                if payload == "online":
                    await self.publish_discovery()
            elif topic == self.t_command:
                await self.handle_command(payload)
            elif topic == self.t_fan:
                await self.handle_fan_speed(payload)
            elif topic == self.t_send:
                await self.handle_send_command(payload)
            elif topic == self.t_segments:
                await self.handle_segments(payload)
            elif topic == self.t_auto:
                # Our own retained state; read once at start-up, ignored after.
                if not self._auto_restored:
                    self.restore_auto(payload)
                    await self.publish_auto()
        except Exception:
            log.exception("Failed handling %s %r", topic, payload)

    # ---------------------------------------------------------------- run

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(30)
            idle = time.monotonic() - self._last_message
            if self.robot.connected and idle > self.cfg.stale_seconds:
                # Exit rather than patch it up in-process: a restart is cheap
                # and the pod's restart count makes a recurring stall visible.
                raise RuntimeError(f"No message from the robot for {idle:.0f}s")

    async def connect_robot(self) -> None:
        delay = 10.0
        while True:
            try:
                await self.robot.connect()
                self._last_message = time.monotonic()
                return
            except RoombaAuthError:
                raise
            except RoombaConnectionError as err:
                # Usually another client (the iRobot app, HA's old integration)
                # holds the robot's one local session.
                log.warning("Robot unreachable (%s); retrying in %.0fs", err, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def run(self) -> None:
        cfg = self.cfg
        self.robot.register_on_message_callback(self.on_robot_message)
        self.robot.register_on_connection_state_callback(self.on_robot_connection)
        will = aiomqtt.Will(self.t_bridge, "offline", qos=1, retain=True)
        async with aiomqtt.Client(
            hostname=cfg.mqtt_host, port=cfg.mqtt_port,
            username=cfg.mqtt_username, password=cfg.mqtt_password,
            identifier="roomba-mqtt-bridge", will=will,
        ) as mqtt:
            self.mqtt = mqtt
            await mqtt.publish(self.t_robot, "offline", qos=1, retain=True)
            await mqtt.publish(self.t_bridge, "online", qos=1, retain=True)
            await self.publish_discovery()
            for topic in (self.t_command, self.t_fan, self.t_send, self.t_segments, self.t_ha_status,
                          *((self.t_auto,) if self.auto else ())):
                await mqtt.subscribe(topic, qos=1)
            log.info("MQTT connected to %s:%s", cfg.mqtt_host, cfg.mqtt_port)
            await self.connect_robot()
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self.watchdog())
                    if self.auto:
                        tg.create_task(self.auto_restore_deadline())
                    async for message in mqtt.messages:
                        await self.dispatch(message)
            finally:
                await self.robot.disconnect()
                await mqtt.publish(self.t_robot, "offline", qos=1, retain=True)
