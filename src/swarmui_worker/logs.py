"""Structured logging with secret redaction.

Every secret the worker handles (tokens, provider keys) is registered here, and every log record is
scrubbed before it is formatted, including records from third-party libraries and the SwarmUI
process output this worker relays.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Iterable

REDACTED = "[REDACTED]"

_secrets_lock = threading.Lock()
_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Marks a value as secret, so it never appears in any log line."""
    if value:
        with _secrets_lock:
            _secrets.add(value)


def forget_secret(value: str) -> None:
    """Stops redacting a value that is no longer in use (e.g. a rotated token)."""
    with _secrets_lock:
        _secrets.discard(value)


def redact(text: str) -> str:
    """Returns the text with every registered secret replaced."""
    with _secrets_lock:
        current: Iterable[str] = sorted(_secrets, key=len, reverse=True)
        for secret in current:
            if secret in text:
                text = text.replace(secret, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """Renders the record's message once, redacted, so formatters never see the raw values."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_text:
            data["exc"] = record.exc_text
        return json.dumps(data, ensure_ascii=False)


def setup(level: str = "INFO", as_json: bool = True) -> None:
    """Configures the root logger. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    if as_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    # aiohttp's access log would print full request lines, which can carry a login token.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
