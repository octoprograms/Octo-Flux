"""Structured logging.

Every log line is a single JSON object. Callers pass an `event` name plus
arbitrary keyword fields. Never pass API key values, Authorization headers,
or full prompt/response bodies — only identifiers (key *names*, sizes).
"""
from __future__ import annotations

import json
import logging
import sys
import time

_LOGGER_NAME = "octoproxy"

_REDACT_KEYS = {"authorization", "api_key", "key_value", "value", "secret"}


def configure_logging(level: str = "INFO") -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


def _redact(fields: dict) -> dict:
    return {k: ("<redacted>" if k.lower() in _REDACT_KEYS else v) for k, v in fields.items()}


def log_event(event: str, level: str = "info", **fields) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    payload = {"ts": round(time.time(), 3), "event": event, **_redact(fields)}
    line = json.dumps(payload, default=str)
    getattr(logger, level, logger.info)(line)
