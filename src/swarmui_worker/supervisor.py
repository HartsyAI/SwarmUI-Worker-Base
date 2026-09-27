"""Ties the pieces together: SwarmUI process, gateway, and lease lifecycle.

Provider adapters (RunPod, Vast.ai) use this through `BackgroundSupervisor`, which runs everything on
its own thread and event loop, so it keeps serving between the provider SDK's jobs and never depends
on how that SDK runs its own loop.

Lease lifecycle, per provider job/session:
  1. `begin_lease()` issues a fresh token (unless one is fixed by the environment) and returns what
     the client needs to connect.
  2. `wait_for_release()` blocks until the worker has gone idle (or never got used, or hit its cap).
  3. `end_lease()` revokes the token, drops every open connection, and wipes outputs, so the next
     lease on this same warm worker starts clean and the previous holder is locked out.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import shutil
import threading
from dataclasses import dataclass

from .config import WorkerConfig, generate_token
from .gateway import Gateway
from .idle import IdleMonitor, is_busy
from .models import build_shadow_tree
from .swarm import SwarmApi, SwarmProcess, write_settings

log = logging.getLogger("swarmui_worker.supervisor")


@dataclass(frozen=True)
class LeaseInfo:
    """What a provider adapter hands back to the client that asked for a worker."""

    token: str
    """Bearer token the client must send on every request."""
    lease_number: int
    """Increments per lease on this worker, for logs and diagnostics."""


class Supervisor:
    """Async core. All methods must run on the supervisor's own event loop."""

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.gateway = Gateway(config.swarm_port, allow_url_login=config.allow_url_login)
        self.api = SwarmApi(config.swarm_port)
        self.process = SwarmProcess(config.swarm_dir, config.swarm_port)
        self._lease_number = 0
        self._lease_active = False

    async def start(self) -> None:
        """Prepares models and settings, then starts the gateway and SwarmUI, and waits for SwarmUI."""
        model_root = self.config.model_root
        if model_root and self.config.shadow_models:
            await asyncio.to_thread(build_shadow_tree, model_root, self.config.shadow_root)
            model_root = self.config.shadow_root
        write_settings(self.config.swarm_dir, model_root)
        if self.config.token:
            await self.gateway.set_token(self.config.token)
        await self.gateway.start(self.config.public_host, self.config.public_port)
        await self.process.start()
        await self.process.wait_until_up(self.api, self.config.boot_timeout)
        self.gateway.set_upstream_ready(True)
        log.info("SwarmUI is up; worker ready")

    async def begin_lease(self) -> LeaseInfo:
        """Opens a lease and returns its token."""
        if self._lease_active:
            raise RuntimeError("A lease is already active on this worker")
        token = self.config.token or generate_token()
        await self.gateway.set_token(token)
        self._lease_number += 1
        self._lease_active = True
        log.info("Lease %d started", self._lease_number)
        return LeaseInfo(token=token, lease_number=self._lease_number)

    async def wait_for_release(self) -> str:
        """Blocks until the lease should end, and returns why."""
        monitor = IdleMonitor(self.config.idle_seconds, self.config.startup_grace_seconds, self.config.max_seconds)
        failures = 0
        while True:
            if not self.process.running:
                return "swarmui_exited"
            try:
                status = await self.api.call("GetGlobalStatus")
                monitor.observe(is_busy(status))
                failures = 0
            except Exception as ex:  # A transient API hiccup must never end a lease by itself.
                failures += 1
                monitor.observe(True)
                if failures in (1, 10) or failures % 60 == 0:
                    log.warning("Activity check failed (%d in a row): %s", failures, ex)
            reason = monitor.release_reason()
            if reason is not None:
                log.info("Lease %d releasing: %s", self._lease_number, reason)
                return reason
            await asyncio.sleep(self.config.poll_interval)

    async def end_lease(self) -> None:
        """Revokes the lease's token, closes its connections, and wipes its outputs."""
        if self.config.token:
            # A fixed token (pods and instances) outlives any lease; only connections are dropped.
            await self.gateway.close_sockets()
        else:
            await self.gateway.set_token(None)
        await asyncio.to_thread(self._wipe_outputs)
        self._lease_active = False
        log.info("Lease %d ended", self._lease_number)

    def _wipe_outputs(self) -> None:
        # Swarm-to-Swarm requests already ask for no saving; this is the backstop against one lease's
        # images being readable by the next lease holder.
        out = self.config.output_dir
        if os.path.isdir(out):
            for name in os.listdir(out):
                path = os.path.join(out, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    async def stop(self) -> None:
        """Stops the gateway and SwarmUI."""
        await self.gateway.stop()
        await self.process.stop()
        await self.api.close()


class BackgroundSupervisor:
    """Runs a `Supervisor` on a dedicated thread; every method is safe to call from any thread."""

    def __init__(self, config: WorkerConfig):
        self._config = config
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="swarmui-worker", daemon=True)
        self._supervisor = Supervisor(config)
        self._thread.start()

    @property
    def config(self) -> WorkerConfig:
        return self._config

    def _submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _await(self, coro):
        return await asyncio.wrap_future(self._submit(coro))

    def start(self) -> None:
        """Blocking start (for use before a provider SDK takes over the main thread)."""
        self._submit(self._supervisor.start()).result()

    async def begin_lease(self) -> LeaseInfo:
        return await self._await(self._supervisor.begin_lease())

    async def wait_for_release(self) -> str:
        return await self._await(self._supervisor.wait_for_release())

    async def end_lease(self) -> None:
        await self._await(self._supervisor.end_lease())

    def stop(self) -> None:
        """Blocking stop."""
        try:
            self._submit(self._supervisor.stop()).result(timeout=60)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
