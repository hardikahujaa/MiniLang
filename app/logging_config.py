"""Structured logging setup for the MiniLang backend.

Backend code never calls :func:`print`. It logs through the standard library
:mod:`logging` module, and this module decides how those records are rendered.

Two formats are supported, selected by the ``MINILANG_LOG_FORMAT`` environment
variable:

* ``json``  (default) -- one JSON object per line, suitable for log shipping
  and for grepping with ``jq`` during a demo.
* ``text``  -- human-friendly single line, handy when tailing locally.

The log level is read from ``MINILANG_LOG_LEVEL`` (default ``INFO``).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from typing import Any, Final

#: Attributes present on every :class:`logging.LogRecord`. Anything *not* in
#: this set was attached by the caller via ``extra={...}`` and is therefore
#: structured context worth emitting.
_STANDARD_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

DEFAULT_LEVEL: Final[str] = "INFO"
DEFAULT_FORMAT: Final[str] = "json"

_configured: bool = False


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single line of JSON.

    Any keyword passed through ``extra={...}`` at the call site is merged into
    the emitted object, which is what makes the logs *structured* rather than
    merely formatted. For example::

        logger.info("compile finished", extra={"phase": "lexer", "tokens": 42})

    produces a record carrying ``phase`` and ``tokens`` as first-class fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record`` to a JSON string.

        Args:
            record: The log record emitted by the logging framework.

        Returns:
            A JSON object encoded as a single line, with no trailing newline.
        """
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Install the root logging handler for the backend.

    Safe to call more than once; repeated calls after the first are ignored so
    that importing the app under pytest does not stack duplicate handlers.

    Args:
        level: Log level name such as ``"DEBUG"``. Defaults to the
            ``MINILANG_LOG_LEVEL`` environment variable, then ``"INFO"``.
        fmt: Either ``"json"`` or ``"text"``. Defaults to the
            ``MINILANG_LOG_FORMAT`` environment variable, then ``"json"``.
    """
    global _configured  # noqa: PLW0603 - module-level idempotency guard
    if _configured:
        return

    resolved_level = (level or os.getenv("MINILANG_LOG_LEVEL") or DEFAULT_LEVEL).upper()
    resolved_fmt = (fmt or os.getenv("MINILANG_LOG_FORMAT") or DEFAULT_FORMAT).lower()

    handler = logging.StreamHandler(stream=sys.stdout)
    if resolved_fmt == "text":
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger.

    Calling this rather than :func:`logging.getLogger` directly guarantees the
    handlers are installed even if the module is imported before app startup
    (which is what happens under pytest).

    Args:
        name: Logger name, conventionally the caller's ``__name__``.

    Returns:
        A :class:`logging.Logger` writing through the configured handler.
    """
    configure_logging()
    return logging.getLogger(name)
