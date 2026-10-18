"""Read-only capture of a Roomba's local traffic, for re-learning the room map.

Never sends anything to the robot. Prints each `start` command the iRobot app
sends, which carries the map ids and every selected room's id and settings.

The robot allows one local session: stop the bridge (scale it to 0) and close
the iRobot app first, then start an all-rooms job from the app and cancel it.

    ROOMBA_HOST=... ROOMBA_BLID=... ROOMBA_PASSWORD=... uv run python tools/capture.py 600
"""

import asyncio
import json
import os
import sys

from roombapy import RoombaClient


async def main(seconds: float) -> None:
    client = RoombaClient(os.environ["ROOMBA_HOST"], os.environ["ROOMBA_BLID"],
                          os.environ["ROOMBA_PASSWORD"])
    seen = set()

    def on_message(_msg):
        cmd = client.master_state.get("state", {}).get("reported", {}).get("lastCommand")
        if isinstance(cmd, dict) and cmd.get("command") == "start" and cmd.get("time") not in seen:
            seen.add(cmd.get("time"))
            print(json.dumps(cmd, indent=1), flush=True)

    client.register_on_message_callback(on_message)
    async with client:
        print(f"connected; listening {seconds:.0f}s", file=sys.stderr, flush=True)
        await asyncio.sleep(seconds)


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 300))
