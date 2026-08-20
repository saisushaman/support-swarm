"""Structured logging with redaction baked in.

Every record is emitted as a single JSON line (configurable) and every string
value passes through :func:`~support_swarm.core.redaction.redact` on the way
out, so an accidental ``log.info("reset for %s", email)`` cannot leak PII.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from ..config import get_settings
from .redaction import redact, redact_text

#: Correlation ids attached to every record inside a request / resolution run.
#: The default is None rather than {} so the sentinel cannot be mutated in place
#: and leak state between requests; readers go through :func:`_current`.
_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


def _current() -> dict[str, Any]:
    return _context.get() or {}


_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


def bind(**kwargs: Any) -> dict[str, Any]:
    """Attach correlation fields to the current context. Returns the old one."""
    previous = _current()
    _context.set({**previous, **{k: v for k, v in kwargs.items() if v is not None}})
    return previous


def restore(previous: dict[str, Any]) -> None:
    _context.set(previous)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        payload.update(redact(_current()))
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = _current()
        suffix = " ".join(f"{k}={v}" for k, v in ctx.items())
        base = f"{record.levelname:<7} {record.name} {redact_text(record.getMessage())}"
        return f"{base} {suffix}".rstrip()


_configured = False


def configure() -> None:
    """Idempotently install the root handler."""
    global _configured
    if _configured:
        return
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_json else TextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("uvicorn.access").propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(name)
