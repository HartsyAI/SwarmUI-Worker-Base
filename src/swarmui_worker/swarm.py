"""Starts SwarmUI on loopback and talks to it through its own public API only."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

import aiohttp

log = logging.getLogger("swarmui_worker.swarm")
swarm_log = logging.getLogger("swarmui")


class SwarmStartError(RuntimeError):
    """SwarmUI failed to start or never answered."""


def write_settings(swarm_dir: str, model_root: str) -> None:
    """Writes the worker's Data/Settings.fds.

    Only the keys the worker owns are written. SwarmUI fills every other setting with its defaults on
    load, and rewrites the file itself. `IsInstalled` skips the first-run installer, which would
    otherwise block the API forever in a headless container.
    """
    data_dir = os.path.join(swarm_dir, "Data")
    os.makedirs(data_dir, exist_ok=True)
    lines = ["IsInstalled: true", "LaunchMode: none"]
    if model_root:
        lines += ["Paths:", f"\tModelRoot: {model_root}"]
    with open(os.path.join(data_dir, "Settings.fds"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class SwarmApi:
    """Minimal client for the local SwarmUI API (no auth needed on loopback)."""

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self._session_id: Optional[str] = None
        self._http: Optional[aiohttp.ClientSession] = None

    async def _client(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()

    async def call(self, route: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """POSTs to /API/<route>, attaching (and renewing if invalid) a session id."""
        http = await self._client()
        for _ in range(2):
            if self._session_id is None and route != "GetNewSession":
                self._session_id = (await self.call("GetNewSession"))["session_id"]
            payload = dict(body or {})
            if route != "GetNewSession":
                payload["session_id"] = self._session_id
            async with http.post(f"{self.base}/API/{route}", json=payload) as resp:
                data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("error_id") == "invalid_session_id":
                self._session_id = None
                continue
            if not isinstance(data, dict):
                raise RuntimeError(f"SwarmUI {route} returned a non-object response")
            return data
        raise RuntimeError(f"SwarmUI kept rejecting sessions for {route}")

    async def is_up(self) -> bool:
        """True if SwarmUI answers GetNewSession."""
        try:
            data = await self.call("GetNewSession")
            return bool(data.get("session_id"))
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            return False


class SwarmProcess:
    """Owns the SwarmUI child process."""

    def __init__(self, swarm_dir: str, port: int, extra_args: Optional[list[str]] = None):
        self._swarm_dir = swarm_dir
        self._port = port
        self._extra_args = list(extra_args or [])
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pump: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        binary = os.path.join(self._swarm_dir, "src", "bin", "live_release", "SwarmUI")
        if not os.path.isfile(binary):
            raise SwarmStartError(f"SwarmUI binary not found at {binary}")
        # Loopback only: the gateway is the sole public entry point.
        args = [binary, "--launch_mode", "none", "--host", "127.0.0.1", "--port", str(self._port), *self._extra_args]
        env = dict(os.environ)
        env.setdefault("HOME", os.path.join(self._swarm_dir, "home"))
        self._proc = await asyncio.create_subprocess_exec(
            *args, cwd=self._swarm_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._pump = asyncio.create_task(self._relay_output())
        log.info("Started SwarmUI (pid %d) on 127.0.0.1:%d", self._proc.pid, self._port)

    async def _relay_output(self) -> None:
        # Chunked reads rather than readline(): a single over-long line (a big stack trace) would
        # otherwise raise, stop this relay, fill the pipe, and freeze SwarmUI on its next write.
        assert self._proc is not None and self._proc.stdout is not None
        pending = b""
        while True:
            chunk = await self._proc.stdout.read(64 * 1024)
            if not chunk:
                break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            if len(pending) > 1024 * 1024:
                lines.append(pending)
                pending = b""
            for raw in lines:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    swarm_log.info("%s", line)
        if pending.strip():
            swarm_log.info("%s", pending.decode("utf-8", errors="replace").rstrip())

    async def wait_until_up(self, api: SwarmApi, timeout: float) -> None:
        """Blocks until SwarmUI answers, the process dies, or the timeout passes."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                raise SwarmStartError(f"SwarmUI exited during startup (code {self._proc.returncode if self._proc else '?'})")
            if await api.is_up():
                return
            await asyncio.sleep(2)
        raise SwarmStartError(f"SwarmUI did not answer within {timeout:.0f}s")

    async def stop(self, grace: float = 20) -> None:
        """SIGTERM, then SIGKILL after `grace` seconds."""
        if self._proc is None or self._proc.returncode is not None:
            return
        self._proc.terminate()
        try:
            await asyncio.wait_for(self._proc.wait(), grace)
        except asyncio.TimeoutError:
            log.warning("SwarmUI did not stop within %.0fs, killing it", grace)
            self._proc.kill()
            await self._proc.wait()
        if self._pump is not None:
            self._pump.cancel()
