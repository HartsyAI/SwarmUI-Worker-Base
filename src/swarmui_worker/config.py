"""Environment-driven configuration for the worker supervisor."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Mapping, Optional

# Shorter tokens are refused: the gateway is the only thing between the public internet and a
# SwarmUI that trusts every loopback caller as its admin.
MIN_TOKEN_LENGTH = 32


class ConfigError(ValueError):
    """Raised when the worker environment is invalid. The message is safe to log."""


def _get_float(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as ex:
        raise ConfigError(f"{name} must be a number, got '{raw}'") from ex
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _get_int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as ex:
        raise ConfigError(f"{name} must be an integer, got '{raw}'") from ex
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got '{raw}'")


def generate_token() -> str:
    """Returns a new random worker token."""
    return secrets.token_urlsafe(48)


@dataclass(frozen=True)
class WorkerConfig:
    """Everything the supervisor needs, read once from the environment at startup."""

    swarm_dir: str
    """Root of the baked SwarmUI install (contains src/bin/live_release)."""
    swarm_port: int
    """Loopback-only port SwarmUI listens on. Never exposed."""
    public_host: str
    """Interface the gateway binds to."""
    public_port: int
    """Port the gateway listens on. This is the only port a provider should expose."""
    token: Optional[str]
    """Fixed worker token from the environment, or None to generate one per lease."""
    model_root: str
    """Where the models live (e.g. a network volume). Empty means the image's own Models folder."""
    shadow_models: bool
    """Give this worker a private model tree of symlinks, so SwarmUI's per-folder metadata databases
    are never written onto storage shared with other workers."""
    shadow_root: str
    """Where the private model tree is built."""
    output_dir: str
    """SwarmUI output folder, wiped between leases."""
    idle_seconds: float
    """How long the worker may sit with no generation activity before it is released."""
    startup_grace_seconds: float
    """How long a freshly started lease may wait for its first generation before it is released."""
    max_seconds: float
    """Longest a single lease may last (0 disables). Never cuts a running generation."""
    poll_interval: float
    """Seconds between activity checks."""
    boot_timeout: float
    """Longest SwarmUI may take to start answering."""
    allow_url_login: bool
    """Allow a browser to log in once with ?worker_token=..., which sets an HttpOnly cookie.
    Off by default because tokens in URLs can end up in proxy logs and browser history."""
    log_json: bool
    """Emit one JSON object per log line."""
    log_level: str
    """Python log level name."""

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "WorkerConfig":
        """Reads and validates the configuration. Raises ConfigError with a readable message."""
        env = os.environ if env is None else env
        swarm_port = _get_int(env, "SWARMUI_INTERNAL_PORT", 7810, 1, 65535)
        public_port = _get_int(env, "SWARMUI_PUBLIC_PORT", 7801, 1, 65535)
        if swarm_port == public_port:
            raise ConfigError("SWARMUI_INTERNAL_PORT and SWARMUI_PUBLIC_PORT must differ")
        token = env.get("SWARMUI_WORKER_TOKEN")
        if token is not None:
            token = token.strip()
            if token == "":
                token = None
            elif len(token) < MIN_TOKEN_LENGTH:
                raise ConfigError(f"SWARMUI_WORKER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
        swarm_dir = env.get("SWARMUI_DIR", "/opt/swarmui")
        return cls(
            swarm_dir=swarm_dir,
            swarm_port=swarm_port,
            public_host=env.get("SWARMUI_PUBLIC_HOST", "0.0.0.0"),
            public_port=public_port,
            token=token,
            model_root=env.get("SWARMUI_MODEL_ROOT", "").strip(),
            shadow_models=_get_bool(env, "SWARMUI_SHADOW_MODELS", True),
            shadow_root=env.get("SWARMUI_SHADOW_ROOT", "/tmp/swarmui-models"),
            output_dir=env.get("SWARMUI_OUTPUT_DIR", os.path.join(swarm_dir, "Output")),
            idle_seconds=_get_float(env, "SWARMUI_IDLE_SECONDS", 120.0, 5.0),
            startup_grace_seconds=_get_float(env, "SWARMUI_STARTUP_GRACE_SECONDS", 600.0, 5.0),
            max_seconds=_get_float(env, "SWARMUI_MAX_LEASE_SECONDS", 3600.0, 0.0),
            poll_interval=_get_float(env, "SWARMUI_POLL_INTERVAL", 5.0, 0.5),
            boot_timeout=_get_float(env, "SWARMUI_BOOT_TIMEOUT", 900.0, 10.0),
            allow_url_login=_get_bool(env, "SWARMUI_WORKER_ALLOW_URL_LOGIN", False),
            log_json=_get_bool(env, "SWARMUI_LOG_JSON", True),
            log_level=env.get("SWARMUI_LOG_LEVEL", "INFO").upper(),
        )
