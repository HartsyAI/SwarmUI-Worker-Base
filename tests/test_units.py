"""Pure-logic tests: idle state machine, activity parsing, config, redaction, model tree."""

from __future__ import annotations

import base64
import json
import logging
import os
import traceback

import pytest

from swarmui_worker import logs
from swarmui_worker.config import ConfigError, WorkerConfig
from swarmui_worker.idle import (REASON_IDLE, REASON_MAX_LIFETIME, REASON_NEVER_USED, IdleMonitor,
                                 is_busy)
from swarmui_worker.models import build_shadow_tree
from swarmui_worker.startup import TRANSPORT_KEY, StartupConfig
from swarmui_worker.startup import main as startup_main
from swarmui_worker.swarm import child_env


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
    ({"SWARMUI_INTERNAL_PORT": "7801"}, "must differ"),
    ({"SWARMUI_IDLE_SECONDS": "abc"}, "must be a number"),
    ({"SWARMUI_IDLE_SECONDS": "1"}, "at least"),
    ({"SWARMUI_SHADOW_MODELS": "maybe"}, "true or false"),
    ({"SWARMUI_WORKER_TOKEN": "t" * 40}, "set worker_token in SWARMUI_WORKER_CONFIG"),
    ({"SWARMUI_PUBLIC_PORT": "7900"}, "set public_port in SWARMUI_WORKER_CONFIG"),
])
def test_config_rejects_bad_values(env, fragment):
    with pytest.raises(ConfigError, match=fragment):
        WorkerConfig.from_env(env)


def test_config_takes_the_token_and_port_from_the_startup_config():
    token = "t" * 40
    raw = encode({"worker_token": token, "public_port": 7900})
    try:
        c = WorkerConfig.from_env({TRANSPORT_KEY: raw})
        assert c.token == token and c.public_port == 7900
        assert token not in repr(c) and token not in repr(StartupConfig.parse(raw))
        # Neither the token nor the encoded value that holds it may reach a log line.
        assert logs.redact(f"{raw} {token}") == f"{logs.REDACTED} {logs.REDACTED}"
    finally:
        logs.forget_secret(token)
        logs.forget_secret(raw)
    with pytest.raises(ConfigError, match="must differ"):
        WorkerConfig.from_env({TRANSPORT_KEY: encode({"public_port": 7810})})


# ── StartupConfig ────────────────────────────────────────────────────────────

def encode(data) -> str:
    """The way the Cloud Backends extension sends a config: unpadded base64url JSON."""
    text = data if isinstance(data, str) else json.dumps(data)
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_startup_config_defaults_when_absent_or_blank():
    assert StartupConfig.from_env({}) == StartupConfig()
    assert StartupConfig.from_env({TRANSPORT_KEY: "  ", "SWARMUI_WORKER_TOKEN": " "}) == StartupConfig()
    assert StartupConfig.parse("{}") == StartupConfig(worker_token=None, public_port=7801, volume_path=None)


@pytest.mark.parametrize("length", range(32, 36))
def test_startup_config_reads_plain_and_base64url_json(length):
    # Four token lengths cover every base64 padding case.
    data = {"worker_token": "a" * length, "public_port": 7802, "volume_path": "/workspace"}
    expected = StartupConfig(worker_token="a" * length, public_port=7802, volume_path="/workspace")
    assert StartupConfig.parse(json.dumps(data)) == expected
    assert StartupConfig.parse(encode(data)) == expected


def test_startup_config_reads_what_the_extension_sends():
    # Produced by CloudWorkerConfig.Encode() in SwarmUI-CloudBackends, whose tests pin the same value.
    sent = ("eyJ3b3JrZXJfdG9rZW4iOiJhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0NSIsInB1YmxpY19wb3J0Ijo3ODAyLCJ2b2x1bWVfcGF0aCI6Ii93b3Jrc3BhY2UifQ")
    assert StartupConfig.parse(sent) == StartupConfig("abcdefghijklmnopqrstuvwxyz012345", 7802, "/workspace")


def test_startup_config_reads_blank_and_null_fields_as_unset():
    assert StartupConfig.parse('{"worker_token": " ", "public_port": null, "volume_path": ""}') == StartupConfig()


@pytest.mark.parametrize("raw,fragment", [
    ("not base64!", "JSON object"),
    ("__4", "JSON object"),  # base64url of bytes that are not UTF-8
    (encode("nope"), "not valid JSON"),
    ("{oops", "not valid JSON"),
    (encode("[]"), "must be a JSON object"),
    ('{"worker_token": "short"}', "at least 32"),
    ('{"worker_token": 12345678901234567890123456789012345}', "must be a string"),
    ('{"public_port": "7801"}', "must be an integer"),
    ('{"public_port": true}', "must be an integer"),
    ('{"public_port": 7801.0}', "must be an integer"),
    ('{"public_port": 0}', "between"),
    ('{"public_port": 70000}', "between"),
    ('{"volume_path": "workspace"}', "absolute path"),
    ('{"volume_path": "/a\\n/b"}', "absolute path"),
    ('{"volume_path": "/a\\u0000b"}', "control characters"),
    ('{"volume_path": "/a\\ud800"}', "control characters"),
    (json.dumps({"worker_token": "a" * 40 + "\n" + "b"}), "printable ASCII"),
    (json.dumps({"worker_token": "a" * 40 + " b"}), "printable ASCII"),
    (json.dumps({"worker_token": "\u00e9" * 40}), "printable ASCII"),
    (json.dumps({"worker_token": "a" * 40 + "\ud800"}), "printable ASCII"),
    ('{"worker_tokn": "x"}', "unknown field"),
    ('{"public_port": 1, "public_port": 2}', "duplicate field"),
])
def test_startup_config_rejects_bad_values(raw, fragment):
    with pytest.raises(ConfigError, match=fragment):
        StartupConfig.parse(raw)


@pytest.mark.parametrize("raw", ['{"worker_token": "sekrit"}', '{"sekrit": 1}', '{"worker_token": "sekrit', "sekrit!", encode("sekrit")])
def test_config_error_never_echoes_the_value(raw):
    with pytest.raises(ConfigError) as info:
        WorkerConfig.from_env({TRANSPORT_KEY: raw})
    shown = "".join(traceback.format_exception(info.value))
    assert "sekrit" not in str(info.value) and raw not in str(info.value)
    # The chained cause is suppressed too, so a logged traceback cannot show it either.
    assert "JSONDecodeError" not in shown


def test_entrypoint_command_prints_only_the_volume_path(monkeypatch, capsys):
    monkeypatch.setenv(TRANSPORT_KEY, encode({"worker_token": "z" * 40, "volume_path": "/runpod-volume"}))
    assert startup_main(["volume_path"]) == 0
    assert capsys.readouterr().out == "/runpod-volume\n"
    assert startup_main(["worker_token"]) == 2
    assert "z" * 40 not in "".join(capsys.readouterr())
    monkeypatch.delenv(TRANSPORT_KEY)
    assert startup_main(["volume_path"]) == 0
    assert capsys.readouterr().out == "\n"
    monkeypatch.setenv(TRANSPORT_KEY, "{oops")
    assert startup_main(["volume_path"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "not valid JSON" in captured.err


def test_swarmui_never_receives_the_worker_config():
    env = child_env({TRANSPORT_KEY: "x", "RUNPOD_API_KEY": "y", "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


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


def test_tls_config_must_be_paired_and_exist(tmp_path):
    with pytest.raises(ConfigError, match="set together"):
        WorkerConfig.from_env({"SWARMUI_TLS_CERT": "/x"})
    with pytest.raises(ConfigError, match="does not exist"):
        WorkerConfig.from_env({"SWARMUI_TLS_CERT": "/nope.crt", "SWARMUI_TLS_KEY": "/nope.key"})
    (tmp_path / "c").write_text("x")
    (tmp_path / "k").write_text("x")
    c = WorkerConfig.from_env({"SWARMUI_TLS_CERT": str(tmp_path / "c"), "SWARMUI_TLS_KEY": str(tmp_path / "k")})
    assert c.tls_cert.endswith("c")


def test_lease_limits_honor_client_values_within_bounds():
    from swarmui_worker.supervisor import Supervisor
    sup = Supervisor(WorkerConfig.from_env({"SWARMUI_MAX_LEASE_SECONDS": "3600"}))
    assert sup.lease_limits() == (120.0, 600.0, 3600.0)
    assert sup.lease_limits(300, 900, 1800) == (300.0, 900.0, 1800.0)
    # Out-of-range requests are clamped; the cap can only be lowered.
    assert sup.lease_limits(1, 99999, 99999) == (5.0, 3600.0, 3600.0)
