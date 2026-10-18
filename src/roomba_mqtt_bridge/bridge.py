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
            await self.robot.send_command("resume" if vac == "paused" else "start")
        elif payload == "pause":
            await self.robot.send_command("pause")
        elif payload == "stop":
            await self.robot.send_command("stop")
        elif payload == "return_to_base":
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
        if command == "clean_rooms":
            p = params or {}
            rooms = p.get("rooms")
            await self.clean_rooms(
                rooms if isinstance(rooms, list) else [rooms] if rooms else [],
                suction=p.get("suction"),
                passes=int(p["passes"]) if p.get("passes") else None,
            )
            return
        log.info("send_command %s %s", command, json.dumps(params))
        # roombapy refuses a start whose regions list is empty: the robot
        # would read that as "clean the whole house".
        await self.robot.send_command(command, params)

    async def clean_rooms(self, rooms: list[str], suction: str | None = None,
                          passes: int | None = None) -> None:
        if self.cfg.robot_map is None:
            raise ValueError("ROOMBA_MAP is not configured; room cleaning is off")
        params = self.cfg.robot_map.start_params([str(r) for r in rooms], suction, passes)
        log.info("Room clean: %s", json.dumps(params))
        await self.robot.send_command("start", params)

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
            for topic in (self.t_command, self.t_fan, self.t_send, self.t_segments, self.t_ha_status):
                await mqtt.subscribe(topic, qos=1)
            log.info("MQTT connected to %s:%s", cfg.mqtt_host, cfg.mqtt_port)
            await self.connect_robot()
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self.watchdog())
                    async for message in mqtt.messages:
                        await self.dispatch(message)
            finally:
                await self.robot.disconnect()
                await mqtt.publish(self.t_robot, "offline", qos=1, retain=True)
