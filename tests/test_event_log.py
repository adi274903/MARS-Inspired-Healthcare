"""Event log tests: idempotent writes + the read API, all on a MemoryBus."""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
import pytest

from core.bus import MemoryBus
from core.config import get_settings
from core.envelope import Event
from services.common import make_app
from services.event_log.main import build as build_event_log
from tests.support import build_fixture_db


def make_settings(tmp_path: Path, monkeypatch):
    db = build_fixture_db(tmp_path)
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("MODELS_YAML", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events" / "events.db"))
    return get_settings()


async def wait_until(predicate: Callable[[], Awaitable[bool]], *, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


def db_rows(path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM events").fetchall()]
    finally:
        conn.close()


@contextlib.asynccontextmanager
async def event_log_harness(settings) -> AsyncIterator[tuple[MemoryBus, httpx.AsyncClient]]:
    bus = MemoryBus()
    await bus.start()
    spec = build_event_log(settings, bus)
    app = make_app("event_log", settings, spec.router)
    for hook in spec.on_start:
        await hook()
    for loop in spec.loops:
        await loop.start()
    await asyncio.sleep(0.02)  # let the subscriber register
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://event-log") as client:
            yield bus, client
    finally:
        for loop in spec.loops:
            await loop.stop()
        await bus.stop()


@pytest.mark.asyncio
async def test_events_are_stored_once_and_queryable(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    async with event_log_harness(settings) as (bus, client):
        events = [
            Event(type=f"test.{i}", payload={"i": i}, correlation_id="cid-1", producer="tester") for i in range(3)
        ]
        for event in events:
            await bus.publish(event)
            await asyncio.sleep(0.01)

        db_path = Path(settings.events_db)
        assert db_path.exists()

        async def stored() -> bool:
            return len(db_rows(db_path)) == 3

        await wait_until(stored)

        rows = db_rows(db_path)
        assert sorted(row["event_id"] for row in rows) == sorted(e.event_id for e in events)
        assert {row["producer"] for row in rows} == {"tester"}

        # duplicate delivery of the same event id must not add a second row
        await bus.publish(events[0])
        await asyncio.sleep(0.2)
        assert len(db_rows(db_path)) == 3

        response = await client.get("/api/events")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 3
        assert {row["event_id"] for row in body} == {e.event_id for e in events}
        # newest first
        assert [row["ts"] for row in body] == sorted((row["ts"] for row in body), reverse=True)
        assert body[0]["payload"].get("i") in {0, 1, 2}
        assert "payload_json" not in body[0]

        filtered = (await client.get("/api/events", params={"type": "test.1"})).json()
        assert [row["type"] for row in filtered] == ["test.1"]

        limited = (await client.get("/api/events", params={"limit": 2})).json()
        assert len(limited) == 2

        timeline = (await client.get("/api/events/cid-1/timeline")).json()
        assert len(timeline) == 3
        assert [row["ts"] for row in timeline] == sorted(row["ts"] for row in timeline)
        assert [row["type"] for row in timeline] == ["test.0", "test.1", "test.2"]
        assert (await client.get("/api/events/nope/timeline")).json() == []


@pytest.mark.asyncio
async def test_storage_failure_does_not_kill_the_loop(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    async with event_log_harness(settings) as (bus, client):
        # an un-json-able payload must still be logged (default=str) and the loop survive
        await bus.publish(Event(type="test.weird", payload={"when": object()}, correlation_id="cid-2"))
        await bus.publish(Event(type="test.ok", payload={"ok": True}, correlation_id="cid-2"))

        db_path = Path(settings.events_db)

        async def stored() -> bool:
            return len(db_rows(db_path)) == 2

        await wait_until(stored)
        timeline = (await client.get("/api/events/cid-2/timeline")).json()
        assert [row["type"] for row in timeline] == ["test.weird", "test.ok"]
