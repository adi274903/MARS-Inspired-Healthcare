"""Structured JSON logging + tiny counters (no external deps)."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

_LOGGER = logging.getLogger("evalbot")
_configured = False
_COUNTERS: dict[str, int] = {}


def _configure() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(os.environ.get("LOG_LEVEL", "info").upper())
    _LOGGER.propagate = False
    _configured = True


def log(service: str, event: str, **fields: Any) -> None:
    """One line of JSON per event. Cheap enough to call on every token batch."""
    _configure()
    record = {"ts": round(time.time(), 6), "service": service, "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    _LOGGER.info(json.dumps(record, default=str, separators=(",", ":")))


def count(name: str, n: int = 1) -> None:
    _COUNTERS[name] = _COUNTERS.get(name, 0) + n


def counters() -> dict[str, int]:
    return dict(_COUNTERS)


def reset_counters() -> None:
    _COUNTERS.clear()
