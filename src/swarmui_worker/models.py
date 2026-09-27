"""Builds a worker-private view of a shared model folder.

SwarmUI keeps a LiteDB metadata cache (`model_metadata.ldb`) inside every model folder it scans. When
several workers share one network volume, they would all open and write the same database files at
once. A private tree of directories whose files are symlinks to the shared models keeps those
databases on the worker's own disk, while the model files themselves are still read from the volume.
"""

from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger("swarmui_worker.models")

# SwarmUI's own per-folder metadata databases: never linked, each worker builds its own.
_PRIVATE_SUFFIXES = (".ldb",)


def build_shadow_tree(source_root: str, shadow_root: str) -> int:
    """Mirrors `source_root` into `shadow_root` as directories plus file symlinks.

    Replaces any previous shadow tree. Returns the number of files linked.
    Symlinked directories inside the source are followed once, but cycles are skipped.
    """
    source_root = os.path.realpath(source_root)
    if not os.path.isdir(source_root):
        raise FileNotFoundError(f"Model root '{source_root}' does not exist or is not a directory")
    if os.path.lexists(shadow_root):
        shutil.rmtree(shadow_root)
    os.makedirs(shadow_root)
    linked = 0
    seen_dirs: set[str] = set()
    for current, dirs, files in os.walk(source_root, followlinks=True):
        real_current = os.path.realpath(current)
        if real_current in seen_dirs:
            dirs[:] = []
            continue
        seen_dirs.add(real_current)
        rel = os.path.relpath(current, source_root)
        target_dir = shadow_root if rel == "." else os.path.join(shadow_root, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            if name.endswith(_PRIVATE_SUFFIXES):
                continue
            os.symlink(os.path.join(current, name), os.path.join(target_dir, name))
            linked += 1
    log.info("Built private model tree: %d files linked from %s", linked, source_root)
    return linked
