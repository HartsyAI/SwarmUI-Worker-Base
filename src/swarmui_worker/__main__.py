"""Standalone mode: a long-running worker with a fixed token (rented pods and instances).

Serverless providers do not use this entry point; their adapter images drive leases through
`BackgroundSupervisor` instead.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from . import logs
from .config import ConfigError, WorkerConfig
from .supervisor import Supervisor

log = logging.getLogger("swarmui_worker")


async def _run(config: WorkerConfig) -> int:
    supervisor = Supervisor(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    starting = asyncio.create_task(supervisor.start())
    stopped = asyncio.create_task(stop.wait())
    # Race startup against SIGTERM, so a stop during a long boot is not ignored.
    await asyncio.wait({starting, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if not starting.done():
        starting.cancel()
        await supervisor.stop()
        return 0
    if starting.exception() is not None:
        log.error("Worker failed to start", exc_info=starting.exception())
        await supervisor.stop()
        return 1
    log.info("Standalone worker running; send SIGTERM to stop")
    crashed = asyncio.create_task(supervisor.watch_process())
    await asyncio.wait({stopped, crashed}, return_when=asyncio.FIRST_COMPLETED)
    exited = crashed.done()
    crashed.cancel()
    await supervisor.stop()
    # A non-zero exit lets the provider's restart policy (or the extension) see the crash.
    return 1 if exited else 0


def main() -> int:
    try:
        config = WorkerConfig.from_env()
    except ConfigError as ex:
        logs.setup()
        log.error("Invalid configuration: %s", ex)
        return 2
    logs.setup(config.log_level, config.log_json)
    if not config.token:
        # A standalone worker has no provider channel to hand out a per-lease token, and running
        # without one would publish an unauthenticated admin SwarmUI.
        log.error("SWARMUI_WORKER_TOKEN is required in standalone mode")
        return 2
    return asyncio.run(_run(config))


if __name__ == "__main__":
    sys.exit(main())
