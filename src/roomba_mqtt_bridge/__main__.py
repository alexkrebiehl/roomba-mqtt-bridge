import asyncio
import logging
import os

from .bridge import Bridge, Config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(Bridge(Config.from_env()).run())


if __name__ == "__main__":
    main()
