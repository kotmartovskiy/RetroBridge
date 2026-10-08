"""Structured, redacted logging for the gateway (ARCHITECTURE §3.2, §6).

One JSON object per line on stderr. Keys whose names look sensitive are
redacted before serialization; values are length-bounded so a hostile device
cannot flood logs through long inputs. No secret material is ever logged.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, Mapping, Optional

LOGGER_NAME = "retrobridge"
REDACTED = "<redacted>"
MAX_FIELD_CHARS = 512

#: Header/field names whose values are never logged.
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "token",
        "access-token",
        "refresh-token",
        "secret",
        "password",
        "pass",
        "auth",
        "x-retrobridge-secret",
    }
)

#: Supported log levels (name → logging level constant).
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
_LEVELS = LEVELS


def is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    return any(part in lowered for part in ("secret", "password", "token"))


def redact_mapping(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``values`` with sensitive keys redacted and long values bounded."""
    safe: Dict[str, Any] = {}
    for key, value in values.items():
        if is_sensitive(str(key)):
            safe[str(key)] = REDACTED
            continue
        if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
            value = value[:MAX_FIELD_CHARS] + "...<truncated>"
        safe[str(key)] = value
    return safe


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON: {ts, level, event, ...fields}."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + ".%03dZ" % (int(record.msecs),),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(redact_mapping(fields))
        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            payload["error"] = exc_text[-2000:]
        try:
            return json.dumps(payload, ensure_ascii=True, sort_keys=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return json.dumps(
                {"ts": payload["ts"], "level": "error", "event": "log_serialization_failed"}
            )


def setup(level: str = "info") -> logging.Logger:
    """Configure and return the gateway logger (idempotent)."""
    logger = logging.getLogger(LOGGER_NAME)
    resolved = _LEVELS.get(level.lower())
    if resolved is None:
        raise ValueError("unknown log level: %s" % level)
    logger.setLevel(resolved)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    for handler in logger.handlers:
        handler.setLevel(resolved)
    return logger


def get_logger(child: Optional[str] = None) -> logging.Logger:
    name = LOGGER_NAME if not child else "%s.%s" % (LOGGER_NAME, child)
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger, level: int, event: str, exc_info: bool = False, **fields: Any
) -> None:
    """Emit one structured event; sensitive keys are redacted, values bounded."""
    logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)
