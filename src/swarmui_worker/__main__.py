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
    try:
        await supervisor.start()
    except Exception:
        log.exception("Worker failed to start")
        await supervisor.stop()
        return 1
    log.info("Standalone worker running; send SIGTERM to stop")
    watcher = asyncio.create_task(_watch_process(supervisor, stop))
    await stop.wait()
    crashed = not supervisor.process.running
    watcher.cancel()
    await supervisor.stop()
    # A non-zero exit lets the provider's restart policy (or the extension) see the crash.
    return 1 if crashed else 0


async def _watch_process(supervisor: Supervisor, stop: asyncio.Event) -> None:
    while supervisor.process.running:
        await asyncio.sleep(5)
    log.error("SwarmUI exited unexpectedly; stopping the worker")
    stop.set()


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
