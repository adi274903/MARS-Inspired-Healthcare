"""Event envelope shared by every service.

One shape for all messages on the bus, so any service can log, dedupe and
correlate without knowing the producer.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

EVENT_VERSION = 1
EVENTS_STREAM = "evalbot.events"


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    causation_id: str | None = None
    producer: str = ""
    deadline: float | None = None
    event_id: str = ""
    ts: float = 0.0
    v: int = EVENT_VERSION

    def __post_init__(self) -> None:
        if not self.event_id:
            object.__setattr__(self, "event_id", uuid.uuid4().hex)
        if not self.ts:
            object.__setattr__(self, "ts", time.time())
        if not self.correlation_id:
            object.__setattr__(self, "correlation_id", uuid.uuid4().hex)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "producer": self.producer,
            "deadline": self.deadline,
            "event_id": self.event_id,
            "ts": self.ts,
            "v": self.v,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            type=raw["type"],
            payload=raw.get("payload") or {},
            correlation_id=raw.get("correlation_id") or "",
            causation_id=raw.get("causation_id"),
            producer=raw.get("producer") or "",
            deadline=raw.get("deadline"),
            event_id=raw.get("event_id") or "",
            ts=raw.get("ts") or 0.0,
            v=int(raw.get("v") or EVENT_VERSION),
        )

    # -- helpers ----------------------------------------------------------
    def child(self, type: str, payload: dict[str, Any] | None = None, producer: str = "") -> "Event":
        """A new event caused by this one: same correlation, same deadline."""
        return Event(
            type=type,
            payload=payload or {},
            correlation_id=self.correlation_id,
            causation_id=self.event_id,
            producer=producer or self.producer,
            deadline=self.deadline,
        )

    def expired(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.time()


def new_event(
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    producer: str = "",
    timeout_s: float | None = None,
    correlation_id: str = "",
) -> Event:
    deadline = time.time() + timeout_s if timeout_s else None
    return Event(
        type=type,
        payload=payload or {},
        producer=producer,
        deadline=deadline,
        correlation_id=correlation_id,
    )
