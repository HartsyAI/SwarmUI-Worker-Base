"""Writes SwarmUI's Data/Backends.fds for the image's backend.

Building an extension only registers its backend *type*; nothing runs SwarmUI's first-run wizard in a
container, so the image must ship a configured backend or the worker comes up unable to generate.

Every setting that could make a cold worker reach out to the network or change itself is pinned off:
auto-update, managed-node updates, and the ComfyUI web frontend download (a worker never serves it).
"""

from __future__ import annotations

import argparse
import os

COMFYUI = """0:
\ttype: comfyui_selfstart
\ttitle: ComfyUI
\tenabled: true
\tsettings:
\t\tStartScript: dlbackend/ComfyUI/main.py
\t\tExtraArgs: {extra_args}
\t\tDisableInternalArgs: false
\t\tAutoUpdate: false
\t\tUpdateManagedNodes: false
\t\tFrontendVersion: None
\t\tEnablePreviews: true
\t\tGPU_ID: 0
\t\tOverQueue: 1
\t\tAutoRestart: true
"""

HARTSYINFERENCE = """0:
\ttype: hartsyinference
\ttitle: HartsyInference
\tenabled: true
\tsettings:
\t\tComputeBackend: {compute}
\t\tGPU_ID: 0
\t\tLowVram: Auto
\t\tOverQueue: 1
\t\tPreviews: true
\t\tAutoUpdate: false
"""


def render(backend: str, prewarm: bool) -> str:
    """Returns the file contents. `prewarm` forces CPU, for the build-time boot with no GPU."""
    if backend == "comfyui":
        return COMFYUI.format(extra_args="--cpu" if prewarm else "")
    if backend == "hartsyinference":
        return HARTSYINFERENCE.format(compute="cpu" if prewarm else "auto")
    raise SystemExit(f"Unknown backend '{backend}'")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", choices=["comfyui", "hartsyinference"])
    parser.add_argument("--swarm-dir", default="/opt/swarmui")
    parser.add_argument("--prewarm", action="store_true")
    args = parser.parse_args()
    data_dir = os.path.join(args.swarm_dir, "Data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "Backends.fds"), "w", encoding="utf-8") as f:
        f.write(render(args.backend, args.prewarm))


if __name__ == "__main__":
    main()
