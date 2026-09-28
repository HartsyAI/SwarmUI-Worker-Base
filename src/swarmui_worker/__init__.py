"""Provider-neutral supervisor for a SwarmUI cloud worker.

Runs SwarmUI bound to loopback, puts an authenticated gateway in front of it, and reports when the
worker has gone idle so a provider adapter (RunPod, Vast.ai, ...) can release it.
"""

__version__ = "0.1.0"
