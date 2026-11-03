"""Event log: durable audit trail of every bus event + a read API.

Write path: one ``ServiceLoop`` subscribed to everything, inserting each event
into SQLite (idempotent on ``event_id``). A bad row must never kill the loop.
Read path: ``GET /api/events`` and ``GET /api/events/{correlation_id}/timeline``,
which is what the tests and the UI replay from.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from core.bus import Bus, ServiceLoop
from core.envelope import Event
from core.telemetry import log
from services.common import ServiceSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT UNIQUE,
    type           TEXT NOT NULL,
    correlation_id TEXT,
    causation_id   TEXT,
    producer       TEXT,
    ts             REAL,
    deadline       REAL,
    payload_json   TEXT,
    inserted_at    REAL
);
CREATE INDEX IF NOT EXISTS events_correlation ON events (correlation_id, ts);
CREATE INDEX IF NOT EXISTS events_type ON events (type, ts);
"""

COLUMNS = ("event_id", "type", "correlation_id", "causation_id", "producer", "ts", "deadline", "payload_json")


class EventStore:
    """Thin SQLite wrapper; every call opens a short-lived connection."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._write_lock = asyncio.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    async def insert(self, event: Event) -> bool:
        """Insert one event; ``False`` when it was already there."""
        row = (
            event.event_id,
            event.type,
            event.correlation_id,
            event.causation_id,
            event.producer,
            event.ts,
            event.deadline,
            json.dumps(event.payload, default=str),
            time.time(),
        )
        async with self._write_lock:
            conn = self.connect()
            try:
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO events ({','.join(COLUMNS)}, inserted_at) VALUES ({','.join('?' * (len(COLUMNS) + 1))})",
                    row,
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def rows(
        self,
        *,
        correlation_id: str | None = None,
        type: str | None = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if type:
            clauses.append("type = ?")
            params.append(type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ts DESC, rowid DESC" if newest_first else "ts ASC, rowid ASC"
        sql = f"SELECT * FROM events {where} ORDER BY {order} LIMIT ?"
        params.append(int(limit))
        conn = self.connect()
        try:
            return [_row_to_dict(raw) for raw in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


def _row_to_dict(raw: sqlite3.Row) -> dict[str, Any]:
    out = {key: raw[key] for key in raw.keys() if key != "payload_json"}
    try:
        out["payload"] = json.loads(raw["payload_json"] or "{}")
    except (TypeError, ValueError):
        out["payload"] = {}
    return out


def build(settings, bus: Bus) -> ServiceSpec:
    store = EventStore(Path(settings.events_db))
    try:  # endpoints answer immediately, even before the startup hook runs
        store.init()
    except Exception as exc:
        log("event_log", "init.failed", error=f"{type(exc).__name__}: {exc}")
    router = APIRouter()

    async def handle(event: Event) -> None:
        try:
            fresh = await store.insert(event)
            if not fresh:
                log("event_log", "duplicate", event_id=event.event_id, type=event.type)
        except Exception as exc:  # never break the loop on a storage hiccup
            log("event_log", "insert.failed", type=event.type, error=f"{type(exc).__name__}: {exc}")

    @router.get("/api/events")
    async def list_events(
        correlation_id: str | None = Query(default=None),
        type: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        try:
            return store.rows(correlation_id=correlation_id, type=type, limit=limit)
        except sqlite3.Error as exc:
            raise HTTPException(503, f"event store unavailable: {exc}") from exc

    @router.get("/api/events/{correlation_id}/timeline")
    async def timeline(correlation_id: str, limit: int = Query(default=500, ge=1, le=5000)) -> list[dict[str, Any]]:
        try:
            return store.rows(correlation_id=correlation_id, limit=limit, newest_first=False)
        except sqlite3.Error as exc:
            raise HTTPException(503, f"event store unavailable: {exc}") from exc

    loop = ServiceLoop("event_log", bus, handle, group="event_log")

    return ServiceSpec(
        name="event_log",
        router=router,
        port=settings.service_ports.get("event_log", 8087),
        loops=[loop],
        on_start=[_async(store.init)],
        meta={"store": store},
    )


def _async(fn):
    async def wrapper() -> None:
        fn()

    return wrapper


def main() -> None:  # ``python -m services.event_log.main``
    from services.common import run

    run("services.event_log.main")


if __name__ == "__main__":
    main()
