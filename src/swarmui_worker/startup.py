"""The startup config a client hands the worker: one typed JSON object in one provider field.

Cloud Backends serializes the same object (`CloudWorkerConfig`) when it creates a pod or instance. It is
sent as unpadded base64url JSON, because some providers turn the field into docker arguments, where
quotes and braces may not survive. Plain JSON is accepted too, for running a container by hand.

`python -m swarmui_worker.startup volume_path` prints the volume path, for the provider entrypoints.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Optional

TRANSPORT_KEY = "SWARMUI_WORKER_CONFIG"
"""Name of the provider field that carries the config."""

DEFAULT_PUBLIC_PORT = 7801

# Shorter tokens are refused: the gateway is the only thing between the public internet and a
# SwarmUI that trusts every loopback caller as its admin.
MIN_TOKEN_LENGTH = 32


class ConfigError(ValueError):
    """Raised when the worker's configuration is invalid. The message is safe to log."""


@dataclass(frozen=True)
class StartupConfig:
    """What the client chose for this worker. Every field is optional."""

    worker_token: Optional[str] = None
    """Fixed gateway token, or None to generate one per lease."""
    public_port: int = DEFAULT_PUBLIC_PORT
    """Port the gateway listens on. This is the only port a provider should expose."""
    volume_path: Optional[str] = None
    """Where the provider mounted the volume with the models, or None to look in the usual places."""

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "StartupConfig":
        """Reads the config from its provider field. An absent or blank field means all defaults."""
        raw = (os.environ if env is None else env).get(TRANSPORT_KEY, "").strip()
        return cls.parse(raw) if raw else cls()

    @classmethod
    def parse(cls, raw: str) -> "StartupConfig":
        """Parses and validates the config. Raises ConfigError, which never repeats the raw value (it holds the token)."""
        data = _decode(raw)
        unknown = sorted(set(data) - {"worker_token", "public_port", "volume_path"})
        if unknown:
            raise ConfigError(f"{TRANSPORT_KEY} has {len(unknown)} unknown field(s); it takes worker_token, public_port and volume_path")
        token = _string(data, "worker_token")
        if token is not None and len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"{TRANSPORT_KEY} worker_token must be at least {MIN_TOKEN_LENGTH} characters")
        port = data.get("public_port")
        if port is None:
            port = DEFAULT_PUBLIC_PORT
        # bool is an int in Python, so `true` would otherwise pass as port 1.
        elif isinstance(port, bool) or not isinstance(port, int):
            raise ConfigError(f"{TRANSPORT_KEY} public_port must be an integer")
        if port < 1 or port > 65535:
            raise ConfigError(f"{TRANSPORT_KEY} public_port must be between 1 and 65535")
        volume = _string(data, "volume_path")
        if volume is not None and (not volume.startswith("/") or "\n" in volume):
            raise ConfigError(f"{TRANSPORT_KEY} volume_path must be an absolute path")
        return cls(worker_token=token, public_port=port, volume_path=volume)


def _decode(raw: str) -> dict[str, Any]:
    """The JSON object in a config value, given as plain JSON or base64url JSON."""
    # `from None` throughout: the original errors quote the value, which holds the token.
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            raise ConfigError(f"{TRANSPORT_KEY} must be a JSON object, as plain text or base64url") from None
    try:
        data = json.loads(raw, object_pairs_hook=_unique_fields)
    except ConfigError:
        raise
    except (ValueError, RecursionError):
        raise ConfigError(f"{TRANSPORT_KEY} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{TRANSPORT_KEY} must be a JSON object")
    return data


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rejects an object that names a field twice, where JSON would silently keep the last."""
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ConfigError(f"{TRANSPORT_KEY} has a duplicate field")
    return data


def _string(data: Mapping[str, Any], name: str) -> Optional[str]:
    """A string field, or None if it is absent, null or blank."""
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{TRANSPORT_KEY} {name} must be a string")
    return value.strip() or None


def main(args: list[str]) -> int:
    """Prints one non-secret field for a shell entrypoint. The token cannot be printed."""
    if args != ["volume_path"]:
        print("usage: python -m swarmui_worker.startup volume_path", file=sys.stderr)
        return 2
    try:
        print(StartupConfig.from_env().volume_path or "")
    except ConfigError as ex:
        print(f"Invalid configuration: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
