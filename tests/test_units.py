"""Pure-logic tests: idle state machine, activity parsing, config, redaction, model tree."""

from __future__ import annotations

import logging
import os

import pytest

from swarmui_worker import logs
from swarmui_worker.config import ConfigError, WorkerConfig
from swarmui_worker.idle import (REASON_IDLE, REASON_MAX_LIFETIME, REASON_NEVER_USED, IdleMonitor,
                                 is_busy)
from swarmui_worker.models import build_shadow_tree


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def status(**counts: int) -> dict:
    base = {"live_gens": 0, "waiting_gens": 0, "loading_models": 0, "waiting_backends": 0}
    base.update(counts)
    return {"status": base}


# ── is_busy ──────────────────────────────────────────────────────────────────

def test_is_busy_counts_only_generation_work():
    assert not is_busy(status())
    for key in ("live_gens", "waiting_gens", "loading_models", "waiting_backends"):
        assert is_busy(status(**{key: 1}))


def test_is_busy_treats_unknown_shapes_as_busy():
    assert is_busy({})
    assert is_busy({"status": "weird"})
    assert is_busy({"status": {"live_gens": "1"}})


# ── IdleMonitor ──────────────────────────────────────────────────────────────

def test_startup_grace_holds_then_releases_unused_lease():
    clock = Clock()
    m = IdleMonitor(idle_seconds=120, startup_grace_seconds=600, max_seconds=0, clock=clock)
    clock.now += 599
    m.observe(False)
    assert m.release_reason() is None
    clock.now += 1
    assert m.release_reason() == REASON_NEVER_USED


def test_idle_counts_from_last_activity_not_from_start():
    clock = Clock()
    m = IdleMonitor(idle_seconds=120, startup_grace_seconds=600, max_seconds=0, clock=clock)
    clock.now += 500
    m.observe(True)
    clock.now += 119
    m.observe(False)
    assert m.release_reason() is None
    clock.now += 1
    assert m.release_reason() == REASON_IDLE


def test_never_releases_while_busy_even_past_cap():
    clock = Clock()
    m = IdleMonitor(idle_seconds=10, startup_grace_seconds=10, max_seconds=60, clock=clock)
    clock.now += 1000
    m.observe(True)
    assert m.release_reason() is None
    m.observe(False)
    assert m.release_reason() == REASON_MAX_LIFETIME


# ── WorkerConfig ─────────────────────────────────────────────────────────────

def test_config_defaults():
    c = WorkerConfig.from_env({})
    assert c.swarm_port == 7810 and c.public_port == 7801
    assert c.token is None and c.shadow_models and not c.allow_url_login


@pytest.mark.parametrize("env,fragment", [
    ({"SWARMUI_WORKER_TOKEN": "short"}, "at least 32"),
    ({"SWARMUI_INTERNAL_PORT": "7801"}, "must differ"),
    ({"SWARMUI_IDLE_SECONDS": "abc"}, "must be a number"),
    ({"SWARMUI_IDLE_SECONDS": "1"}, "at least"),
    ({"SWARMUI_SHADOW_MODELS": "maybe"}, "true or false"),
    ({"SWARMUI_PUBLIC_PORT": "70000"}, "between"),
])
def test_config_rejects_bad_values(env, fragment):
    with pytest.raises(ConfigError, match=fragment):
        WorkerConfig.from_env(env)


def test_config_error_never_echoes_the_token():
    with pytest.raises(ConfigError) as info:
        WorkerConfig.from_env({"SWARMUI_WORKER_TOKEN": "sekrit"})
    assert "sekrit" not in str(info.value)


# ── Redaction ────────────────────────────────────────────────────────────────

def test_redacting_filter_scrubs_messages_args_and_tracebacks():
    secret = "s" * 40
    logs.register_secret(secret)
    try:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "key=%s", (secret,), None)
        logs.RedactingFilter().filter(record)
        assert secret not in record.getMessage()
        try:
            raise RuntimeError(f"boom {secret}")
        except RuntimeError:
            import sys
            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
        logs.RedactingFilter().filter(record)
        assert secret not in record.exc_text
    finally:
        logs.forget_secret(secret)
    assert logs.redact(secret) == secret


# ── Shadow model tree ────────────────────────────────────────────────────────

def test_shadow_tree_links_files_and_keeps_metadata_dbs_private(tmp_path):
    src = tmp_path / "volume"
    (src / "Stable-Diffusion" / "sub").mkdir(parents=True)
    (src / "Stable-Diffusion" / "a.safetensors").write_bytes(b"x")
    (src / "Stable-Diffusion" / "sub" / "b.safetensors").write_bytes(b"y")
    (src / "Stable-Diffusion" / "model_metadata.ldb").write_bytes(b"db")
    (src / "Stable-Diffusion" / "model_metadata-log.ldb").write_bytes(b"db")
    shadow = tmp_path / "shadow"
    assert build_shadow_tree(str(src), str(shadow)) == 2
    link = shadow / "Stable-Diffusion" / "a.safetensors"
    assert link.is_symlink() and link.read_bytes() == b"x"
    assert (shadow / "Stable-Diffusion" / "sub" / "b.safetensors").is_symlink()
    assert not (shadow / "Stable-Diffusion" / "model_metadata.ldb").exists()
    # A worker writing its own db lands on local disk, not the shared volume.
    (shadow / "Stable-Diffusion" / "model_metadata.ldb").write_bytes(b"mine")
    assert (src / "Stable-Diffusion" / "model_metadata.ldb").read_bytes() == b"db"


def test_shadow_tree_rebuild_replaces_old_tree_and_survives_cycles(tmp_path):
    src = tmp_path / "volume"
    (src / "Lora").mkdir(parents=True)
    (src / "Lora" / "x.safetensors").write_bytes(b"1")
    os.symlink(src, src / "Lora" / "loop")
    shadow = tmp_path / "shadow"
    build_shadow_tree(str(src), str(shadow))
    (src / "Lora" / "x.safetensors").unlink()
    (src / "Lora" / "y.safetensors").write_bytes(b"2")
    build_shadow_tree(str(src), str(shadow))
    assert not (shadow / "Lora" / "x.safetensors").exists()
    assert (shadow / "Lora" / "y.safetensors").is_symlink()


def test_shadow_tree_missing_root_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        build_shadow_tree(str(tmp_path / "nope"), str(tmp_path / "shadow"))
