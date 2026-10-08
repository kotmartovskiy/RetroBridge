"""Structured logging: JSON lines, redaction, bounded values (ARCHITECTURE §3.2)."""
from __future__ import annotations

import io
import json
import logging

import pytest

from core import logging as rb_logging


@pytest.fixture()
def logger():
    logger = rb_logging.setup("debug")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(rb_logging.JsonFormatter())
    logger.addHandler(handler)
    try:
        yield logger, stream
    finally:
        logger.removeHandler(handler)


def parse_events(stream: io.StringIO):
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_line_shape(logger):
    logger, stream = logger
    rb_logging.log_event(logger, logging.INFO, "startup", bind="127.0.0.1:8080")
    events = parse_events(stream)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "startup"
    assert event["level"] == "info"
    assert event["bind"] == "127.0.0.1:8080"
    assert event["ts"].endswith("Z")


def test_sensitive_keys_redacted():
    result = rb_logging.redact_mapping({
        "authorization": "Bearer abc",
        "cookie": "sid=1",
        "x-api-key": "k",
        "password": "p",
        "api_token": "t",
        "host": "example.com",
    })
    assert result["authorization"] == rb_logging.REDACTED
    assert result["cookie"] == rb_logging.REDACTED
    assert result["x-api-key"] == rb_logging.REDACTED
    assert result["password"] == rb_logging.REDACTED
    assert result["api_token"] == rb_logging.REDACTED
    assert result["host"] == "example.com"


def test_long_values_bounded():
    result = rb_logging.redact_mapping({"path": "/x" * 1000})
    assert len(result["path"]) <= rb_logging.MAX_FIELD_CHARS + 20
    assert result["path"].endswith("...<truncated>")


def test_log_event_redacts_secrets(logger):
    logger, stream = logger
    rb_logging.log_event(
        logger, logging.INFO, "request",
        authorization="Bearer topsecret", host="example.com",
    )
    payload = stream.getvalue()
    assert "topsecret" not in payload
    assert rb_logging.REDACTED in payload
    event = parse_events(stream)[0]
    assert event["authorization"] == rb_logging.REDACTED


def test_setup_rejects_unknown_level():
    with pytest.raises(ValueError):
        rb_logging.setup("chatty")


def test_setup_is_idempotent():
    first = rb_logging.setup("info")
    second = rb_logging.setup("warning")
    assert first is second
    assert len(first.handlers) == 1


def test_is_sensitive_rules():
    assert rb_logging.is_sensitive("Authorization")
    assert rb_logging.is_sensitive("X-Auth-Token")
    assert rb_logging.is_sensitive("set-cookie")
    assert rb_logging.is_sensitive("my-password")
    assert not rb_logging.is_sensitive("host")
    assert not rb_logging.is_sensitive("content-type")


def test_format_exception_is_truncated():
    record = logging.LogRecord("retrobridge", logging.ERROR, __file__, 1,
                               "boom", None, None)
    try:
        raise ValueError("x" * 5000)
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    text = rb_logging.JsonFormatter().format(record)
    payload = json.loads(text)
    assert len(payload.get("error", "")) <= 2100
