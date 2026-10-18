# roomba-mqtt-bridge

Holds an iRobot Roomba's local MQTT session and mirrors it onto an MQTT broker
as Home Assistant entities (MQTT device discovery). It replaces HA's `roomba`
integration: the robot accepts **one** local client, so the two cannot run
side by side.

Built for an s9+ (`s955020`) on a Clean Base (`4500490`), using
[roombapy](https://pypi.org/project/roombapy/) 2.x.

## Why

HA's integration ignores the Clean Base's own state, so a clogged dock showed
as "docked, bin full" for days. It also went silent for 10 days without going
unavailable. This bridge publishes the dock state, a single headline status
built for dashboards, and drops to unavailable when the robot goes quiet.

## Entities

| Entity | Values |
|---|---|
| `vacuum.roomba` | HA vacuum: start/pause/stop/dock/locate, suction, `send_command`, room cleaning (`vacuum.clean_area`) |
| `sensor.roomba_status` | `error` `stuck` `cleaning` `returning` `dock_clogged` `dock_bag_full` `dock_problem` `bin_full` `emptying` `paused` `charging` `docked` `idle`; attributes `reasons`, `error` |
| `sensor.roomba_dock` | `ready` `emptying` `clogged` `bag_full` `bag_missing` `sealing_problem` `comms_problem` `problem` `none` `unknown`; attribute `code` |
| `binary_sensor.roomba_attention` | on when someone should look; attribute `reasons` |
| `sensor.roomba_mission` | `idle` `running` `returning` `paused` `emptying`; attributes `rooms`, `started`, `elapsed_min`, `initiator`, `phase` |
| `sensor.roomba_last_mission` | `ok` `failed` `cancelled`; attributes `ended`, `started`, `rooms`, `initiator` |
| `sensor.roomba_battery`, `binary_sensor.roomba_bin_full`, `binary_sensor.roomba_charging` | |

`status` picks the most urgent of several true conditions: a robot fault, then
an active job, then dock faults, then a full bin, then routine states. While a
job runs it reads `cleaning` even if the dock is clogged; `attention` still
turns on with `dock_clogged` among its reasons.

Every entity is unavailable unless both the bridge (MQTT last will) and the
robot session are up.

Dock codes 301/302/351 (ready/emptying/clogged) were observed on this robot.
The others come from the dorita980/rest980 community and are unverified.

## Room cleaning

Rooms are configured, not discovered: the robot stores only numeric region ids,
and the ids and map version have to match what the iRobot app sends.
`ROOMBA_MAP` holds them, in the app's room order, with each room's default
suction and passes:

```json
{"pmap_id": "...", "user_pmapv_id": "...",
 "rooms": [{"id": "4", "name": "Living Room", "suction": "Automatic", "passes": 2}]}
```

Two ways in, both built by the same code and both refusing an empty room list
(the robot reads an empty list as "the whole house"):

- **`vacuum.clean_area`**: map the rooms to HA areas in the vacuum's entity
  settings, then clean areas. Uses each room's defaults.
- **`vacuum.send_command`** with `command: clean_rooms` and
  `params: {rooms: [names or ids], suction: Automatic|Eco|Performance, passes: 1|2}`.
  `suction`/`passes` override every selected room for that job only.

Rooms always run in the app's order. Editing rooms in the app renumbers them
and changes both map ids: re-capture with `tools/capture.py`.

Suction set through the vacuum's fan speed is different: it changes the
robot's saved preference, which later jobs started from the app button also
use.

## Configuration

| Env | |
|---|---|
| `ROOMBA_HOST`, `ROOMBA_BLID`, `ROOMBA_PASSWORD` | robot address and local credentials |
| `MQTT_HOST`, `MQTT_PORT` (1883), `MQTT_USERNAME`, `MQTT_PASSWORD` | broker |
| `ROOMBA_MAP` | JSON above; omit to disable room cleaning |
| `STALE_SECONDS` (300) | exit when the robot sends nothing for this long; it reports Wi-Fi signal every few seconds even when idle |
| `BASE_TOPIC` (`roomba`), `DISCOVERY_PREFIX` (`homeassistant`), `DEVICE_NAME`, `LOG_LEVEL` | |

The process exits on a broker disconnect or a stale robot session and expects
to be restarted. Run exactly one instance.

## Development

```sh
uv run pytest
```

`tests/fixtures/capture-2026-10-18.jsonl` is real robot traffic (two app jobs
that failed on a clogged dock), reduced to the fields the bridge reads. The
tests replay it, and check that the room commands the bridge builds match the
app's exactly.

Images: `ghcr.io/alexkrebiehl/roomba-mqtt-bridge:sha-<commit>`, built by CI
on every push to `main`. Deployed by Flux from `flux-kustomizations-internal`.
