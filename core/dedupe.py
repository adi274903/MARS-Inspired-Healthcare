"""At-least-once delivery support: idempotency records + deadline watchdog."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any


class MemoryDedupe:
    """Bounded LRU of seen event ids (tests, monolith)."""

    def __init__(self, maxlen: int = 25_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.maxlen = maxlen

    async def seen(self, event_id: str) -> bool:
        if event_id in self._seen:
            return True
        self._seen[event_id] = None
        while len(self._seen) > self.maxlen:
            self._seen.popitem(last=False)
        return False


class SqliteDedupe:
    """Durable idempotency table shared by processes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS seen (event_id TEXT PRIMARY KEY, ts REAL)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def seen(self, event_id: str) -> bool:
        with self._conn() as c:
            try:
                c.execute("INSERT INTO seen (event_id, ts) VALUES (?, ?)", (event_id, time.time()))
            except sqlite3.IntegrityError:
                return True
        return False


class DeadlineWatchdog:
    """Fires ``on_timeout`` for correlations that produced no terminal event."""

    def __init__(self, on_timeout: Any, interval: float = 0.05) -> None:
        self.on_timeout = on_timeout
        self.interval = interval
        self._pending: dict[str, float] = {}
        self._done: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def expect(self, correlation_id: str, deadline: float) -> None:
        self._pending[correlation_id] = deadline

    def settle(self, correlation_id: str) -> None:
        self._pending.pop(correlation_id, None)
        self._done.add(correlation_id)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="watchdog")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            now = time.time()
            for cid, deadline in list(self._pending.items()):
                if now > deadline:
                    self._pending.pop(cid, None)
                    await self.on_timeout(cid)
            await asyncio.sleep(self.interval)
