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
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .config import WorkerConfig, generate_token
from .gateway import Gateway
from .idle import REASON_EXITED, REASON_UNRESPONSIVE, IdleMonitor, is_busy
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
        self._stopping = False
        self._retired = False

    async def start(self) -> None:
        """Prepares models and settings, then starts the gateway and SwarmUI, and waits for SwarmUI."""
        model_root = self.config.model_root
        if model_root and self.config.shadow_models:
            await asyncio.to_thread(build_shadow_tree, model_root, self.config.shadow_root)
            model_root = self.config.shadow_root
        write_settings(self.config.swarm_dir, model_root)
        if self.config.token:
            await self.gateway.set_token(self.config.token)
        await self.gateway.start(self.config.public_host, self.config.public_port,
                                 self.config.tls_cert, self.config.tls_key)
        await self.process.start()
        await self.process.wait_until_up(self.api, self.config.boot_timeout)
        self.gateway.set_upstream_ready(True)
        log.info("SwarmUI is up; worker ready")

    async def begin_lease(self) -> LeaseInfo:
        """Opens a lease and returns its token."""
        if self._retired:
            raise RuntimeError("This worker is retired: a previous lease's generations could not be confirmed stopped")
        if self._lease_active:
            raise RuntimeError("A lease is already active on this worker")
        token = self.config.token or generate_token()
        await self.gateway.set_token(token)
        self._lease_number += 1
        self._lease_active = True
        log.info("Lease %d started", self._lease_number)
        return LeaseInfo(token=token, lease_number=self._lease_number)

    def lease_limits(self, idle_seconds: Optional[float] = None, startup_grace_seconds: Optional[float] = None,
                     max_seconds: Optional[float] = None) -> tuple[float, float, float]:
        """The limits for one lease: the client's requested values where given, within safe bounds.

        The idle window and startup grace are the client's to choose (5s to 1h). The lease cap may only be
        lowered, never raised: it exists to stay under the provider's execution timeout.
        """
        def pick(value: Optional[float], default: float, low: float, high: float) -> float:
            if value is None or not math.isfinite(float(value)):
                return default
            return min(max(float(value), low), high)
        cap = self.config.max_seconds
        return (pick(idle_seconds, self.config.idle_seconds, 5, 3600),
                pick(startup_grace_seconds, self.config.startup_grace_seconds, 5, 3600),
                pick(max_seconds, cap, 60, cap) if cap > 0 else pick(max_seconds, 0, 60, 7 * 24 * 3600))

    async def wait_for_release(self, idle_seconds: Optional[float] = None, startup_grace_seconds: Optional[float] = None,
                               max_seconds: Optional[float] = None) -> str:
        """Blocks until the lease should end, and returns why."""
        limits = self.lease_limits(idle_seconds, startup_grace_seconds, max_seconds)
        monitor = IdleMonitor(*limits)
        # A failed check counts as busy, so a slow API never releases mid-generation; a dead one still ends the lease.
        unresponsive_after = max(300.0, limits[0])
        failing_since: Optional[float] = None
        failures = 0
        while True:
            if not self.process.running:
                return REASON_EXITED
            try:
                status = await self.api.call("GetGlobalStatus")
                if "error" in status:
                    raise RuntimeError(f"GetGlobalStatus returned an error: {status.get('error')}")
                monitor.observe(is_busy(status))
                failing_since = None
                failures = 0
            except Exception as ex:
                failures += 1
                failing_since = failing_since or time.monotonic()
                monitor.observe(True)
                if failures in (1, 10) or failures % 60 == 0:
                    log.warning("Activity check failed (%d in a row): %s", failures, ex)
                if time.monotonic() - failing_since >= unresponsive_after:
                    log.error("Lease %d releasing: SwarmUI has not answered for %.0fs", self._lease_number,
                              unresponsive_after)
                    return REASON_UNRESPONSIVE
            reason = monitor.release_reason()
            if reason is not None:
                log.info("Lease %d releasing: %s", self._lease_number, reason)
                return reason
            await asyncio.sleep(self.config.poll_interval)

    async def end_lease(self) -> None:
        """Revokes the lease's token, closes its connections, stops its generations, and wipes its outputs."""
        if self.config.token:
            # A fixed token (pods and instances) outlives any lease; only connections are dropped.
            await self.gateway.close_connections()
        else:
            await self.gateway.set_token(None)
        try:
            # Stop anything still generating, or a late image could land after the wipe for the next holder.
            result = await self.api.call("InterruptAll", {"other_sessions": True})
            if "error" in result:
                raise RuntimeError(result.get("error"))
        except Exception as ex:
            # Unconfirmed: never hand this worker to anyone else. The provider replaces it.
            log.error("Could not interrupt generations at lease end (%s); retiring this worker", ex)
            self._retired = True
        await asyncio.to_thread(self._wipe_outputs)
        self._lease_active = False
        log.info("Lease %d ended", self._lease_number)

    def _wipe_outputs(self) -> None:
        # Swarm-to-Swarm requests already ask for no saving; this is the backstop against one lease's
        # images being readable by the next lease holder.
        out = os.path.join(self.config.swarm_dir, "Output")
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

    @property
    def retired(self) -> bool:
        """True once a lease ended without its generations confirmed stopped; the worker takes no more leases."""
        return self._retired

    async def watch_process(self) -> None:
        """Returns once SwarmUI exits on its own (not through `stop()`) or the worker is retired. Call only after `start()` has finished."""
        while (self.process.running or self._stopping) and not self._retired:
            await asyncio.sleep(5)
        log.error("SwarmUI exited unexpectedly" if not self._retired else "Worker retired")

    async def stop(self) -> None:
        """Stops the gateway and SwarmUI."""
        self._stopping = True
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
        """Blocking start (for use before a provider SDK takes over the main thread).

        If SwarmUI later dies, or the worker is retired, the whole process exits non-zero so the provider replaces it.
        """
        self._submit(self._supervisor.start()).result()
        self._submit(self._exit_when_swarm_dies())

    async def _exit_when_swarm_dies(self) -> None:
        await self._supervisor.watch_process()
        os._exit(1)

    async def begin_lease(self) -> LeaseInfo:
        return await self._await(self._supervisor.begin_lease())

    def lease_limits(self, *args, **kwargs) -> tuple[float, float, float]:
        return self._supervisor.lease_limits(*args, **kwargs)

    async def wait_for_release(self, idle_seconds: Optional[float] = None, startup_grace_seconds: Optional[float] = None,
                               max_seconds: Optional[float] = None) -> str:
        return await self._await(self._supervisor.wait_for_release(idle_seconds, startup_grace_seconds, max_seconds))

    async def end_lease(self) -> None:
        await self._await(self._supervisor.end_lease())
        if self._supervisor.retired:
            # Exit right away rather than at the next watch tick, so no new lease can land in between.
            log.error("Exiting so the provider replaces this retired worker")
            os._exit(1)

    def stop(self) -> None:
        """Blocking stop."""
        try:
            self._submit(self._supervisor.stop()).result(timeout=60)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
