"""Small JSON logger with conservative secret redaction."""

import json
import logging
import re
from typing import Any

_SECRET = re.compile(r"(?i)(password|token|api[_-]?key|secret|signature|signed[_-]?url)")
_SECRET_VALUE = re.compile(r"(?i)(password|token|api[_-]?key|secret|signature)\s*[=:]|https?://[^\s?]+\?")
_LOGGER = logging.getLogger("photo_server")


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SECRET.search(str(key)) else _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return "[REDACTED]"
    if isinstance(value, bytes):
        return "[REDACTED]"
    return value


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    _LOGGER.log(level, json.dumps({"event": event, **_safe(fields)}, sort_keys=True, default=str))
