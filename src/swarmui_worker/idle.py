"""Decides when a lease is over, from SwarmUI's own activity counters.

Only real work counts as activity: generations running or waiting, and model loads. HTTP traffic does
not, because the Swarm backend that attaches to this worker polls it every few seconds forever, and
counting that would keep a paid worker alive with nothing to do.
"""

from __future__ import annotations

import time
from typing import Callable, Mapping, Optional

REASON_IDLE = "idle"
REASON_NEVER_USED = "startup_grace_expired"
REASON_MAX_LIFETIME = "max_lifetime"


def is_busy(global_status: Mapping[str, object]) -> bool:
    """True if SwarmUI's /API/GetGlobalStatus response shows any generation work in flight."""
    status = global_status.get("status")
    if not isinstance(status, Mapping):
        # Unknown shape: assume busy, so a format change can never release a worker mid-generation.
        return True
    for key in ("live_gens", "waiting_gens", "loading_models", "waiting_backends"):
        value = status.get(key, 0)
        if not isinstance(value, (int, float)) or value > 0:
            return True
    return False


class IdleMonitor:
    """State machine for one lease.

    - Until the first activity, the lease gets `startup_grace_seconds`. That covers the Swarm side
      attaching and the worker's backend loading, which is not generation activity but is not idle
      either.
    - After activity has been seen, the lease ends `idle_seconds` after the last activity.
    - `max_seconds` (if above 0) ends the lease once it is reached and nothing is running. It never
      interrupts a generation; the provider's own execution timeout is the hard stop.
    """

    def __init__(self, idle_seconds: float, startup_grace_seconds: float, max_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self._idle_seconds = idle_seconds
        self._grace_seconds = startup_grace_seconds
        self._max_seconds = max_seconds
        self._clock = clock
        self._started_at = clock()
        self._last_activity: Optional[float] = None
        self._busy = False

    @property
    def seen_activity(self) -> bool:
        """True once any generation activity has been observed during this lease."""
        return self._last_activity is not None

    def observe(self, busy: bool) -> None:
        """Records one activity sample."""
        self._busy = busy
        if busy:
            self._last_activity = self._clock()

    def release_reason(self) -> Optional[str]:
        """The reason this lease should end now, or None to keep it."""
        if self._busy:
            return None
        now = self._clock()
        if self._max_seconds > 0 and now - self._started_at >= self._max_seconds:
            return REASON_MAX_LIFETIME
        if self._last_activity is None:
            if now - self._started_at >= self._grace_seconds:
                return REASON_NEVER_USED
            return None
        if now - self._last_activity >= self._idle_seconds:
            return REASON_IDLE
        return None
