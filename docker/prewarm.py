"""Boots SwarmUI once at image build time, on CPU, then shuts it down.

Two jobs:
- Let SwarmUI itself install whatever its backend wants on first start (ComfyUI's Python packages
  pinned in ComfyUISelfStartBackend.RequiredPythonPackages, and so on). Doing it here, with SwarmUI's
  own logic, means the list never drifts from what SwarmUI expects, and no cold worker pays for it.
- Prove the image works: the backend must reach "running" with no attempt to rebuild an extension
  (the runtime image has no .NET SDK, so a rebuild attempt would fail at every cold start).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time

from swarmui_worker import logs
from swarmui_worker.swarm import SwarmApi, SwarmProcess, write_settings

log = logging.getLogger("prewarm")
PORT = 7899
TIMEOUT = float(os.environ.get("PREWARM_TIMEOUT", "2400"))
SWARM_DIR = os.environ.get("SWARMUI_DIR", "/opt/swarmui")


class OutputWatch(logging.Handler):
    """Collects SwarmUI's relayed log lines so the build can fail on a forbidden rebuild."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def run() -> int:
    watch = OutputWatch()
    logging.getLogger("swarmui").addHandler(watch)
    write_settings(SWARM_DIR, "")
    # Debug level: SwarmUI only logs "Building extension project" at debug.
    proc = SwarmProcess(SWARM_DIR, PORT, extra_args=["--loglevel", "debug"])
    api = SwarmApi(PORT)
    try:
        await proc.start()
        await proc.wait_until_up(api, 900)
        deadline = time.monotonic() + TIMEOUT
        status = "unknown"
        while time.monotonic() < deadline:
            data = await api.call("ListBackends", {"nonreal": False, "full_data": True})
            backends = [b for b in data.values() if isinstance(b, dict)]
            if not backends:
                log.error("SwarmUI has no backend configured: %s", data)
                return 1
            status = str(backends[0].get("status", "unknown")).lower()
            if status == "running":
                break
            if status == "errored":
                log.error("Backend errored during prewarm: %s", backends[0])
                return 1
            await asyncio.sleep(10)
        if status != "running":
            log.error("Backend did not reach running within %.0fs (last status %s)", TIMEOUT, status)
            return 1
        rebuilds = [line for line in watch.lines
                    if "Building extension project" in line or "Build of extension project" in line]
        if rebuilds:
            log.error("SwarmUI tried to rebuild an extension at startup: %s", rebuilds[:3])
            return 1
        log.info("Prewarm complete: backend running")
        return 0
    finally:
        await proc.stop()
        await api.close()


def clean_runtime_state() -> None:
    """Drops everything the prewarm boot wrote that must not ship in the image."""
    data = os.path.join(SWARM_DIR, "Data")
    for name in os.listdir(data):
        if name not in ("Backends.fds",):
            path = os.path.join(data, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    for folder in ("Output", "tmp"):
        shutil.rmtree(os.path.join(SWARM_DIR, folder), ignore_errors=True)


if __name__ == "__main__":
    logs.setup("INFO", as_json=False)
    code = asyncio.run(run())
    clean_runtime_state()
    sys.exit(code)
