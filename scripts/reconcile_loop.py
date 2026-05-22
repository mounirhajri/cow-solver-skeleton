"""Run reconciliation in a loop. Invoked by docker-compose as a sidecar service."""

import asyncio

from src.config import settings
from src.log import configure_logging, get_logger
from src.shadow.cow_api import CowApiClient
from src.shadow.reconcile import reconcile_once

log = get_logger(__name__)


async def main() -> None:
    configure_logging(level=settings.log_level)
    cow_api = CowApiClient(network="arbitrum_one")
    log_path = settings.shadow_log_path

    while True:
        try:
            updated = await reconcile_once(log_path, cow_api)
            log.info("reconcile_cycle", updated=updated)
        except Exception as e:  # noqa: BLE001
            log.error("reconcile_failed", error=str(e))
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
