"""Tests for :mod:`app.logging_config`.

The quality bar for this project bans ``print()`` in backend code, so logging is
load-bearing rather than incidental: if the formatter drops the structured
context attached via ``extra={...}``, the logs silently lose the very fields
that make them useful.
"""

from __future__ import annotations

import json
import logging

import pytest

from app import logging_config
from app.logging_config import JsonFormatter, configure_logging, get_logger


def make_record(message: str = "hello", **extra: object) -> logging.LogRecord:
    """Build a log record carrying arbitrary structured context.

    Args:
        message: The log message.
        **extra: Fields to attach as if passed via ``extra={...}``.

    Returns:
        A :class:`logging.LogRecord` ready to be formatted.
    """
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    """One JSON object per line, with caller context preserved."""

    def test_emits_a_single_line_of_valid_json(self):
        output = JsonFormatter().format(make_record())
        assert "\n" not in output
        assert json.loads(output)["message"] == "hello"

    def test_includes_standard_fields(self):
        payload = json.loads(JsonFormatter().format(make_record()))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.logger"
        assert "ts" in payload

    def test_preserves_structured_extras(self):
        """This is the whole point of structured logging over print()."""
        record = make_record("compile", phase="lexer", tokens=42)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["phase"] == "lexer"
        assert payload["tokens"] == 42

    def test_interpolates_message_args(self):
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="compiled %d lines",
            args=(7,),
            exc_info=None,
        )
        assert json.loads(JsonFormatter().format(record))["message"] == "compiled 7 lines"

    def test_serialises_non_json_values_rather_than_raising(self):
        """A stray object in `extra` must not take down the request."""
        payload = json.loads(JsonFormatter().format(make_record(obj=object())))
        assert isinstance(payload["obj"], str)

    def test_includes_exception_text(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = make_record("failed")
            record.exc_info = sys.exc_info()
            payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in payload["exception"]

    def test_unicode_is_not_escaped(self):
        payload = json.loads(JsonFormatter().format(make_record("中文 é")))
        assert payload["message"] == "中文 é"


class TestConfigureLogging:
    """Handler installation, level and format selection."""

    @pytest.fixture(autouse=True)
    def reset_logging_state(self):
        """Undo the module-level idempotency guard between tests."""
        original = logging_config._configured
        original_handlers = list(logging.getLogger().handlers)
        logging_config._configured = False
        yield
        logging_config._configured = original
        logging.getLogger().handlers = original_handlers

    def test_installs_exactly_one_handler(self):
        configure_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_is_idempotent(self):
        """Importing the app under pytest must not stack duplicate handlers."""
        configure_logging()
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_json_is_the_default_format(self):
        configure_logging()
        assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_text_format_is_selectable(self):
        configure_logging(fmt="text")
        assert not isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_level_is_applied(self):
        configure_logging(level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_environment_variables_are_honoured(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MINILANG_LOG_LEVEL", "warning")
        monkeypatch.setenv("MINILANG_LOG_FORMAT", "TEXT")
        configure_logging()
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_get_logger_configures_on_first_use(self):
        logger = get_logger("app.test")
        assert logger.name == "app.test"
        assert logging.getLogger().handlers
